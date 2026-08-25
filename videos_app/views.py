from datetime import timedelta

from django.db.models import Avg, Q, Sum
from django.db.models import F
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response

from hamromentor.permissions import IsAdminRoleOrAbove, IsStaffOrReadOnly

from .models import Video, VideoCategory, VideoNote, VideoProgress, VideoResource
from .serializers import (
    VideoAdminSerializer, VideoCategorySerializer, VideoDetailSerializer,
    VideoListSerializer, VideoNoteSerializer, VideoProgressSerializer, VideoResourceSerializer,
)


class VideoCategoryViewSet(viewsets.ModelViewSet):
    queryset = VideoCategory.objects.all()
    serializer_class = VideoCategorySerializer
    permission_classes = [IsStaffOrReadOnly]


def _course_gated_video_scoped(qs, user):
    """billing.access.has_video_access() already correctly gates the actual
    stream/progress endpoints (videos_app/views.py's stream/mark-progress
    actions) against real Enrollment for access_level='course' videos. But
    VideoViewSet.get_queryset() — the LIST endpoint — had no equivalent
    check at all: a course-gated video scoped to an unrelated course was
    still fully listed (title/description/thumbnail) to any authenticated
    user, only its actual playback was blocked. Only touches
    access_level='course' rows; every other access_level's visibility
    (public/registered/premium/teacher_only) is unaffected."""
    if user and user.is_authenticated and user.is_staff:
        return qs
    from django.db.models import Q as Q_gate

    from courses.access import eligible_course_ids

    course_ids = eligible_course_ids(user)
    allowed = ~Q_gate(access_level='course') | Q_gate(courses__isnull=True) | Q_gate(courses__id__in=course_ids)
    return qs.filter(allowed).distinct()


class VideoViewSet(viewsets.ModelViewSet):
    queryset = (
        Video.objects.select_related('subject', 'chapter', 'topic', 'category', 'created_by')
        .prefetch_related('courses', 'linked_tests', 'resources')
    )
    permission_classes = [IsStaffOrReadOnly]

    def get_serializer_class(self):
        user = self.request.user
        if user.is_authenticated and user.is_staff:
            return VideoAdminSerializer
        if self.action == 'retrieve':
            return VideoDetailSerializer
        return VideoListSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        course = params.get('course')
        subject = params.get('subject')
        chapter = params.get('chapter')
        topic = params.get('topic')
        category = params.get('category')
        instructor = params.get('instructor')
        search = params.get('search')
        access = params.get('access')  # 'free' | 'premium'
        sort = params.get('sort')  # 'recent' | 'popular'
        bookmarked = params.get('bookmarked')

        if course:
            qs = qs.filter(courses__id=course)
        if subject:
            qs = qs.filter(subject__slug=subject)
        if chapter:
            qs = qs.filter(chapter_id=chapter)
        if topic:
            qs = qs.filter(topic_id=topic)
        if category:
            qs = qs.filter(category_id=category)
        if instructor:
            qs = qs.filter(created_by_id=instructor)
        if search:
            qs = qs.filter(
                Q(title__icontains=search) | Q(description__icontains=search)
                | Q(instructor_name__icontains=search) | Q(subject__name__icontains=search)
            )
        if access == 'free':
            qs = qs.filter(access_level__in=['public', 'registered'])
        elif access == 'premium':
            qs = qs.filter(access_level__in=['premium', 'course'])

        user = self.request.user
        if bookmarked and user.is_authenticated:
            qs = qs.filter(progress__user=user, progress__is_bookmarked=True)

        is_staff_user = user.is_authenticated and user.is_staff
        if not is_staff_user:
            qs = qs.filter(is_active=True, is_archived=False)
            qs = _course_gated_video_scoped(qs, user)
        else:
            if params.get('include_archived') != 'true':
                qs = qs.exclude(is_archived=True)
            if getattr(user, 'admin_role', None) == 'teacher' and not user.can_manage_all_content:
                qs = qs.filter(created_by=user)

        if sort == 'recent':
            qs = qs.order_by('-created_at')
        elif sort == 'popular':
            qs = qs.order_by('-views_count')

        return qs.distinct()

    @action(detail=True, methods=['patch'], permission_classes=[IsAdminUser])
    def upload_media(self, request, pk=None):
        """One multipart call for a video's file + thumbnail, mirroring Question's upload_images."""
        video = self.get_object()
        if 'video_file' in request.FILES:
            video.video_file = request.FILES['video_file']
        if 'thumbnail' in request.FILES:
            video.thumbnail = request.FILES['thumbnail']
        video.save()
        return Response(VideoAdminSerializer(video, context=self.get_serializer_context()).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def progress(self, request, pk=None):
        from billing.access import has_video_access

        video = self.get_object()
        if not has_video_access(request.user, video):
            return Response({'detail': 'You do not have access to this video.'}, status=403)

        position = max(int(request.data.get('position_seconds', 0) or 0), 0)
        mark_complete = bool(request.data.get('mark_complete', False))

        record, created = VideoProgress.objects.get_or_create(user=request.user, video=video)
        record.last_position_seconds = position
        record.max_position_seconds = max(record.max_position_seconds, position)
        if not record.is_completed and (mark_complete or (video.duration_seconds and position >= video.duration_seconds * 0.9)):
            record.is_completed = True
            record.completed_at = timezone.now()
        record.save()

        if created:
            Video.objects.filter(pk=video.pk).update(views_count=F('views_count') + 1)

        return Response(VideoProgressSerializer(record).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def bookmark(self, request, pk=None):
        from billing.access import has_video_access

        video = self.get_object()
        if not has_video_access(request.user, video):
            return Response({'detail': 'You do not have access to this video.'}, status=403)

        record, _ = VideoProgress.objects.get_or_create(user=request.user, video=video)
        record.is_bookmarked = not record.is_bookmarked
        record.save(update_fields=['is_bookmarked'])
        return Response({'is_bookmarked': record.is_bookmarked})

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def continue_watching(self, request):
        records = (
            VideoProgress.objects.filter(
                user=request.user, is_completed=False,
                video__is_active=True, video__is_archived=False,
            )
            .select_related('video', 'video__subject')
            .order_by('-last_watched_at')[:10]
        )
        context = self.get_serializer_context()
        return Response([
            {
                'video': VideoListSerializer(r.video, context=context).data,
                'last_position_seconds': r.last_position_seconds,
                'duration_seconds': r.video.duration_seconds,
            }
            for r in records
        ])

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def recommended(self, request):
        """MVP heuristic: same subject as the most recently watched video → bookmarked
        subjects not yet started → most-viewed in the student's active course. Weak-quiz-
        performance and upcoming-exam-schedule correlation are deferred (no existing
        per-subject performance aggregation or exam-schedule model to key off yet)."""
        user = request.user
        base = Video.objects.filter(is_active=True, is_archived=False)

        candidates = Video.objects.none()
        last = (
            VideoProgress.objects.filter(user=user).select_related('video')
            .order_by('-last_watched_at').first()
        )
        if last and last.video.subject_id:
            candidates = (
                base.filter(subject_id=last.video.subject_id)
                .exclude(pk=last.video_id)
                .exclude(progress__user=user, progress__is_completed=True)
            )
        if not candidates.exists():
            bookmarked_subjects = list(
                Video.objects.filter(progress__user=user, progress__is_bookmarked=True)
                .values_list('subject_id', flat=True)
            )
            if bookmarked_subjects:
                candidates = base.filter(subject_id__in=bookmarked_subjects).exclude(progress__user=user)
        if not candidates.exists():
            active_course_id = getattr(user, 'active_course_id', None)
            candidates = base.filter(courses__id=active_course_id) if active_course_id else base
            candidates = candidates.order_by('-views_count')

        serializer = VideoListSerializer(candidates.distinct()[:10], many=True, context=self.get_serializer_context())
        return Response(serializer.data)

    @action(detail=True, methods=['get'], permission_classes=[IsAdminUser])
    def analytics(self, request, pk=None):
        video = self.get_object()
        records = video.progress.all()
        total = records.count()
        completed = records.filter(is_completed=True).count()
        avg_percent = 0
        if video.duration_seconds and total:
            avg_position = records.aggregate(avg=Avg('max_position_seconds'))['avg'] or 0
            avg_percent = round(min(avg_position / video.duration_seconds, 1) * 100, 1)
        return Response({
            'views_count': video.views_count,
            'watchers': total,
            'completed': completed,
            'avg_watch_percent': avg_percent,
        })

    @action(detail=False, methods=['get'], permission_classes=[IsAdminUser])
    def analytics_mine(self, request):
        videos = Video.objects.filter(created_by=request.user)
        total_views = videos.aggregate(total=Sum('views_count'))['total'] or 0
        return Response({
            'videos_uploaded': videos.count(),
            'total_views': total_views,
            'videos': VideoAdminSerializer(
                videos.order_by('-views_count')[:20], many=True, context=self.get_serializer_context(),
            ).data,
        })

    @action(detail=False, methods=['get'], permission_classes=[IsAdminRoleOrAbove])
    def analytics_platform(self, request):
        most_viewed = Video.objects.filter(is_archived=False).order_by('-views_count')[:10]
        least_viewed = Video.objects.filter(is_archived=False, views_count__gt=0).order_by('views_count')[:10]
        popular_subjects = (
            Video.objects.filter(subject__isnull=False).values('subject__name')
            .annotate(total_views=Sum('views_count')).order_by('-total_views')[:10]
        )
        total_progress = VideoProgress.objects.count()
        completed_progress = VideoProgress.objects.filter(is_completed=True).count()
        completion_rate = round(completed_progress / total_progress * 100, 1) if total_progress else 0
        context = self.get_serializer_context()
        return Response({
            'most_viewed': VideoListSerializer(most_viewed, many=True, context=context).data,
            'least_viewed': VideoListSerializer(least_viewed, many=True, context=context).data,
            'popular_subjects': list(popular_subjects),
            'completion_rate': completion_rate,
        })

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def student_summary(self, request):
        records = VideoProgress.objects.filter(user=request.user)
        completed = records.filter(is_completed=True).count()
        total_watch_seconds = records.aggregate(total=Sum('max_position_seconds'))['total'] or 0

        watched_dates = {r.last_watched_at.date() for r in records}
        streak = 0
        cursor = timezone.now().date()
        while cursor in watched_dates:
            streak += 1
            cursor -= timedelta(days=1)

        return Response({
            'videos_completed': completed,
            'total_watch_seconds': total_watch_seconds,
            'streak_days': streak,
        })


class VideoResourceViewSet(viewsets.ModelViewSet):
    queryset = VideoResource.objects.select_related('video')
    serializer_class = VideoResourceSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        video = self.request.query_params.get('video')
        if video:
            qs = qs.filter(video_id=video)
        user = self.request.user
        if not (user.is_authenticated and user.is_staff):
            qs = qs.filter(video__is_active=True, video__is_archived=False)
        return qs

    def _check_can_write(self, video):
        user = self.request.user
        if not (user.is_authenticated and user.is_staff):
            raise PermissionDenied
        if getattr(user, 'admin_role', None) == 'teacher' and not user.can_manage_all_content and video.created_by_id != user.id:
            raise PermissionDenied('You can only manage resources on your own videos.')

    def perform_create(self, serializer):
        self._check_can_write(serializer.validated_data['video'])
        serializer.save()

    def perform_update(self, serializer):
        self._check_can_write(serializer.instance.video)
        serializer.save()

    def perform_destroy(self, instance):
        self._check_can_write(instance.video)
        instance.delete()


class VideoNoteViewSet(viewsets.ModelViewSet):
    queryset = VideoNote.objects.all()
    serializer_class = VideoNoteSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset().filter(user=self.request.user)
        video = self.request.query_params.get('video')
        if video:
            qs = qs.filter(video_id=video)
        return qs

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
