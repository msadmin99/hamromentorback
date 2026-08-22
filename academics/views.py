from django.db.models import Exists, OuterRef
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from hamromentor.permissions import IsStaffOrReadOnly, IsStaffOrReadOnlyExcludingTeacherWrites

from .excel import import_workbook, template_response
from .models import Chapter, Option, Question, QuestionAttempt, Subject, Topic
from .serializers import (
    AnswerSubmitSerializer,
    ChapterSerializer,
    QuestionAdminSerializer,
    QuestionSerializer,
    SubjectDetailSerializer,
    SubjectListSerializer,
    TopicSerializer,
)


class SubjectViewSet(viewsets.ModelViewSet):
    queryset = Subject.objects.all().prefetch_related('courses')
    permission_classes = [IsStaffOrReadOnlyExcludingTeacherWrites]
    lookup_field = 'slug'

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return SubjectDetailSerializer
        return SubjectListSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        course_id = self.request.query_params.get('course')
        if course_id:
            qs = qs.filter(courses__id=course_id)
        return qs.distinct()


class ChapterViewSet(viewsets.ModelViewSet):
    queryset = Chapter.objects.all()
    serializer_class = ChapterSerializer
    permission_classes = [IsStaffOrReadOnlyExcludingTeacherWrites]

    def get_queryset(self):
        qs = super().get_queryset()
        subject_slug = self.request.query_params.get('subject')
        if subject_slug:
            qs = qs.filter(subject__slug=subject_slug)
        return qs


class TopicViewSet(viewsets.ModelViewSet):
    queryset = Topic.objects.all()
    serializer_class = TopicSerializer
    permission_classes = [IsStaffOrReadOnlyExcludingTeacherWrites]

    def get_queryset(self):
        qs = super().get_queryset()
        chapter_id = self.request.query_params.get('chapter')
        if chapter_id:
            qs = qs.filter(chapter_id=chapter_id)
        return qs


class QuestionViewSet(viewsets.ModelViewSet):
    queryset = Question.objects.all().select_related('subject', 'chapter').prefetch_related('options')
    permission_classes = [IsStaffOrReadOnly]

    def get_serializer_class(self):
        if self.request.user.is_authenticated and self.request.user.is_staff:
            return QuestionAdminSerializer
        return QuestionSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        subject = self.request.query_params.get('subject')
        chapter = self.request.query_params.get('chapter')  # Chapter model — "Unit" in the UI
        topic = self.request.query_params.get('topic')  # Topic model — "Chapter" in the UI
        year = self.request.query_params.get('year')
        course = self.request.query_params.get('course')
        teacher = self.request.query_params.get('teacher')
        search = self.request.query_params.get('search')
        bookmarked = self.request.query_params.get('bookmarked')
        if subject:
            qs = qs.filter(subject__slug=subject)
        if chapter:
            qs = qs.filter(chapter_id=chapter)
        if topic:
            qs = qs.filter(topic_id=topic)
        if year:
            qs = qs.filter(year=year)
        if course:
            qs = qs.filter(courses__id=course)
        if teacher:
            qs = qs.filter(created_by_id=teacher)
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(public_id__icontains=search) | Q(text__icontains=search)
                | Q(subject__name__icontains=search) | Q(chapter__name__icontains=search)
                | Q(topic__name__icontains=search)
            )
        if bookmarked in ('true', '1'):
            if self.request.user.is_authenticated:
                qs = qs.filter(attempts__user=self.request.user, attempts__is_bookmarked=True)
            else:
                qs = qs.none()

        user = self.request.user
        if not (user.is_authenticated and user.is_staff):
            from billing.access import has_qbank_access
            locked_subject_ids = [s.id for s in Subject.objects.all() if not has_qbank_access(user, s)]
            if locked_subject_ids:
                qs = qs.exclude(subject_id__in=locked_subject_ids)
        elif getattr(user, 'admin_role', None) == 'teacher' and not user.can_manage_all_content:
            qs = qs.filter(created_by=user)

        if user.is_authenticated:
            # One EXISTS subquery joined into the main SELECT, not a query
            # per row — QuestionSerializer.get_is_bookmarked just reads this
            # annotation, so fetching a whole chapter's worth of questions
            # for a solve session stays a single query.
            qs = qs.annotate(
                is_bookmarked_by_user=Exists(
                    QuestionAttempt.objects.filter(user=user, question=OuterRef('pk'), is_bookmarked=True)
                )
            )
        return qs.distinct()

    def destroy(self, request, *args, **kwargs):
        """Permanent delete — blocked if the question has practice-attempt
        history or is used in an exam students have already attempted
        (mirrors TestViewSet's own attempt-guard). On success, also cleans
        up every associated image (both the newer MediaAsset pipeline and
        any legacy ImageField) and writes a DeletionAuditLog entry either
        way."""
        from core.deletion_audit import delete_file_field, record_deletion
        from media_library.service import delete_media_asset

        question = self.get_object()
        label = question.public_id

        if question.attempts.exists():
            msg = 'This question has practice-attempt history and cannot be deleted — consider unpublishing it instead.'
            record_deletion(request, 'Question', question.id, label, result='failure', failure_reason=msg)
            return Response({'detail': msg}, status=status.HTTP_400_BAD_REQUEST)

        tests_in_use = question.testquestion_set.filter(test__attempts__isnull=False).select_related('test').distinct()
        if tests_in_use.exists():
            titles = ', '.join(tq.test.title for tq in tests_in_use[:3])
            msg = f'This question is used in an exam with student attempts ({titles}) and cannot be deleted.'
            record_deletion(request, 'Question', question.id, label, result='failure', failure_reason=msg)
            return Response({'detail': msg}, status=status.HTTP_400_BAD_REQUEST)

        options = list(question.options.all())
        media_assets = [question.image_asset, question.explanation_image_asset] + [o.image_asset for o in options]

        try:
            for asset in media_assets:
                if asset:
                    delete_media_asset(asset)
            delete_file_field(question.image)
            delete_file_field(question.explanation_image)
            for opt in options:
                delete_file_field(opt.image)
            response = super().destroy(request, *args, **kwargs)
        except Exception as exc:
            record_deletion(request, 'Question', question.id, label, result='failure', failure_reason=str(exc)[:500])
            return Response({'detail': 'Deletion failed. No partial deletion should remain.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        record_deletion(request, 'Question', question.id, label, result='success')
        return response

    @action(detail=False, methods=['get'], permission_classes=[IsAdminUser])
    def summary(self, request):
        """Question Bank Summary page: total count + a by-course breakdown.
        Subject/Unit/Chapter breakdowns are served by the existing /subjects/
        (and /subjects/{slug}/) endpoints' question_count/mcq_count fields."""
        from django.db.models import Count

        from courses.models import Course

        courses = Course.objects.annotate(question_count=Count('questions')).order_by('-question_count')
        return Response({
            'total_questions': Question.objects.count(),
            'by_course': [
                {'id': c.id, 'name': c.name, 'question_count': c.question_count} for c in courses
            ],
        })

    @action(detail=True, methods=['patch'], permission_classes=[IsAdminUser])
    def upload_images(self, request, pk=None):
        """One call for every image a question can carry: its own image, the
        explanation image, and up to 4 option images (option_image_0..3).

        Two ways to set an image, both supported here:
        - Legacy: multipart file under `image`/`explanation_image`/`option_image_{i}`
          — stored directly on the plain ImageField, unoptimized (kept for
          back-compat with any existing caller).
        - New (preferred): `image_asset_id`/`explanation_image_asset_id`/
          `option_image_asset_id_{i}` — the id of an already-processed
          MediaAsset from POST /api/media/upload/ (validated, optimized,
          responsive variants). The Admin question form uploads via that
          endpoint first, polls until ready, then attaches the id here.
        - Clearing: `clear_image`/`clear_explanation_image`/`clear_option_image_{i}`
          (any truthy value) removes both the legacy field and the asset FK —
          used when an admin removes a previously-set image without replacing it.
        """
        from media_library.models import MediaAsset

        def truthy(value):
            return str(value).lower() in ('1', 'true', 'yes')

        question = self.get_object()
        if truthy(request.data.get('clear_image')):
            question.image = None
            question.image_asset = None
        elif 'image' in request.FILES:
            question.image = request.FILES['image']
        elif request.data.get('image_asset_id'):
            question.image_asset = MediaAsset.objects.filter(id=request.data['image_asset_id']).first()

        if truthy(request.data.get('clear_explanation_image')):
            question.explanation_image = None
            question.explanation_image_asset = None
        elif 'explanation_image' in request.FILES:
            question.explanation_image = request.FILES['explanation_image']
        elif request.data.get('explanation_image_asset_id'):
            question.explanation_image_asset = MediaAsset.objects.filter(id=request.data['explanation_image_asset_id']).first()
        question.save()

        options = list(question.options.order_by('order'))
        for i, opt in enumerate(options):
            file_key = f'option_image_{i}'
            asset_key = f'option_image_asset_id_{i}'
            clear_key = f'clear_option_image_{i}'
            changed = False
            if truthy(request.data.get(clear_key)):
                opt.image = None
                opt.image_asset = None
                changed = True
            elif file_key in request.FILES:
                opt.image = request.FILES[file_key]
                changed = True
            elif request.data.get(asset_key):
                opt.image_asset = MediaAsset.objects.filter(id=request.data[asset_key]).first()
                changed = True
            if changed:
                opt.save()

        return Response(QuestionAdminSerializer(question, context={'request': request}).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def answer(self, request, pk=None):
        question = self.get_object()
        serializer = AnswerSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        option_id = serializer.validated_data.get('option_id')
        bookmark = serializer.validated_data.get('bookmark', False)

        selected_option = None
        is_correct = False
        if option_id:
            selected_option = get_object_or_404(Option, pk=option_id, question=question)
            is_correct = selected_option.is_correct

        attempt, _ = QuestionAttempt.objects.update_or_create(
            user=request.user, question=question,
            defaults={
                'selected_option': selected_option,
                'is_correct': is_correct,
                'is_bookmarked': bookmark,
            },
        )

        correct_option = question.options.filter(is_correct=True).first()
        return Response({
            'is_correct': is_correct,
            'correct_option_id': correct_option.id if correct_option else None,
            'explanation': question.explanation,
            'explanation_image': request.build_absolute_uri(question.explanation_image.url) if question.explanation_image else None,
            'explanation_latex': question.explanation_latex,
            'explanation_video_url': question.explanation_video_url,
            'references': question.references,
        })

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def bookmark(self, request, pk=None):
        """Toggles a bookmark independently of answer() — deliberately a
        separate action, not `answer` called with only `bookmark`, because
        answer()'s update_or_create always writes selected_option/is_correct
        from the request (None/False when option_id is omitted), which would
        silently blank out a previously-recorded answer. Bookmarking must be
        safe to do before, during, or after answering, so this only ever
        touches is_bookmarked."""
        question = self.get_object()
        # bool("False") is True — request.data.get() can come back as a
        # form-encoded string as well as a real JSON boolean, so a plain
        # bool() cast would make "turn bookmark off" silently turn it on.
        is_bookmarked = request.data.get('bookmark') in (True, 'true', 'True', '1', 1)
        QuestionAttempt.objects.update_or_create(
            user=request.user, question=question,
            defaults={'is_bookmarked': is_bookmarked},
        )
        return Response({'is_bookmarked': is_bookmarked})


class QuestionExcelTemplateView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        return template_response()


class QuestionExcelImportView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request):
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({'detail': 'No file uploaded.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            summary = import_workbook(file_obj)
        except Exception as exc:  # noqa: BLE001
            return Response({'detail': f'Could not read that file: {exc}'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(summary)
