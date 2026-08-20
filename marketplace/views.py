from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from hamromentor.permissions import IsAdminRoleOrAbove, IsAdminRoleOrAboveOrReadOnly, IsCourseOwnerOrAdmin

from .models import CourseCategory, CourseEnrollment, CourseLesson, CourseSection, TeacherCourse, TeacherProfile
from .serializers import (
    CourseCategorySerializer,
    CourseEnrollmentSerializer,
    CourseLessonSerializer,
    CourseSectionSerializer,
    TeacherCourseSerializer,
    TeacherProfileSerializer,
)

ACCESS_DURATION_DAYS = {'30_days': 30, '90_days': 90, '180_days': 180, '1_year': 365}


def _visible_course_filter(user, prefix=''):
    """Which TeacherCourse-scoped rows a user may LIST: staff see everything;
    an authenticated user sees their own courses (any status) plus everyone
    else's approved ones; anonymous sees only approved. `prefix` lets this be
    reused for CourseSection/CourseLesson querysets (e.g. 'course__' /
    'section__course__') — list filtering isn't covered by
    has_object_permission, which only guards retrieve/update/destroy of a
    single already-fetched object."""
    status_key = f'{prefix}status' if prefix else 'status'
    teacher_key = f'{prefix}teacher' if prefix else 'teacher'
    if user.is_authenticated and user.is_staff:
        return Q()
    if user.is_authenticated:
        return Q(**{teacher_key: user}) | Q(**{status_key: 'approved'})
    return Q(**{status_key: 'approved'})


class TeacherApplicationViewSet(viewsets.ModelViewSet):
    """Self-serve teacher application: any authenticated user can create their
    own (create=self, one per account); listing/approve/reject is admin-only.
    Deliberately separate from the internal admin_role='teacher' staff concept
    — see marketplace/models.py:TeacherProfile docstring."""
    queryset = TeacherProfile.objects.select_related('user').order_by('-submitted_at')
    serializer_class = TeacherProfileSerializer

    def get_permissions(self):
        if self.action == 'create':
            return [IsAuthenticated()]
        return [IsAdminRoleOrAbove()]

    def get_queryset(self):
        qs = super().get_queryset()
        status_filter = self.request.query_params.get('status')
        if status_filter and status_filter != 'all':
            qs = qs.filter(status=status_filter)
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(Q(user__first_name__icontains=search) | Q(user__email__icontains=search))
        return qs

    def create(self, request, *args, **kwargs):
        if TeacherProfile.objects.filter(user=request.user).exists():
            return Response({'detail': 'You already have a teacher application.'}, status=400)
        return super().create(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        profile = self.get_object()
        profile.status = 'approved'
        profile.decided_at = timezone.now()
        profile.decided_by = request.user
        profile.rejection_reason = ''
        profile.save()
        return Response(TeacherProfileSerializer(profile).data)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        profile = self.get_object()
        profile.status = 'rejected'
        profile.decided_at = timezone.now()
        profile.decided_by = request.user
        profile.rejection_reason = request.data.get('reason', '')
        profile.save()
        return Response(TeacherProfileSerializer(profile).data)


class CourseCategoryViewSet(viewsets.ModelViewSet):
    queryset = CourseCategory.objects.all()
    serializer_class = CourseCategorySerializer
    permission_classes = [IsAdminRoleOrAboveOrReadOnly]


class TeacherCourseViewSet(viewsets.ModelViewSet):
    queryset = TeacherCourse.objects.select_related('teacher', 'category').prefetch_related('sections__lessons')
    serializer_class = TeacherCourseSerializer
    permission_classes = [IsCourseOwnerOrAdmin]

    def get_queryset(self):
        user = self.request.user
        mine = self.request.query_params.get('mine')
        if mine and user.is_authenticated:
            qs = super().get_queryset().filter(teacher=user)
        else:
            qs = super().get_queryset().filter(_visible_course_filter(user))
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs

    def destroy(self, request, *args, **kwargs):
        """Permanent delete — blocked if any student is enrolled, since
        CourseEnrollment.course is CASCADE and would otherwise silently
        destroy paying students' access grants. Set status to a rejected/
        archived state instead of deleting a course students have access to."""
        from core.deletion_audit import record_deletion

        course = self.get_object()
        label = course.title

        if course.enrollments.exists():
            msg = 'Students are enrolled in this course and it cannot be deleted — reject or archive it instead.'
            record_deletion(request, 'TeacherCourse', course.id, label, result='failure', failure_reason=msg)
            return Response({'detail': msg}, status=400)

        try:
            response = super().destroy(request, *args, **kwargs)
        except Exception as exc:
            record_deletion(request, 'TeacherCourse', course.id, label, result='failure', failure_reason=str(exc)[:500])
            return Response({'detail': 'Deletion failed. No partial deletion should remain.'}, status=500)

        record_deletion(request, 'TeacherCourse', course.id, label, result='success')
        return response

    @action(detail=True, methods=['post'])
    def submit(self, request, pk=None):
        course = self.get_object()
        if course.teacher_id != request.user.id and not request.user.is_staff:
            raise PermissionDenied("You don't own this course.")
        if course.status not in ('draft', 'rejected'):
            return Response({'detail': f'Course is currently "{course.status}" — cannot submit.'}, status=400)
        if not course.sections.exists():
            return Response({'detail': 'Add at least one section before submitting.'}, status=400)
        course.status = 'pending_review'
        course.submitted_at = timezone.now()
        course.save(update_fields=['status', 'submitted_at'])
        return Response(TeacherCourseSerializer(course).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAdminRoleOrAbove])
    def approve(self, request, pk=None):
        course = self.get_object()
        course.status = 'approved'
        course.reviewed_at = timezone.now()
        course.reviewed_by = request.user
        course.rejection_reason = ''
        course.save()
        return Response(TeacherCourseSerializer(course).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAdminRoleOrAbove])
    def reject(self, request, pk=None):
        course = self.get_object()
        course.status = 'rejected'
        course.reviewed_at = timezone.now()
        course.reviewed_by = request.user
        course.rejection_reason = request.data.get('reason', '')
        course.save()
        return Response(TeacherCourseSerializer(course).data)


class CourseSectionViewSet(viewsets.ModelViewSet):
    queryset = CourseSection.objects.select_related('course').prefetch_related('lessons')
    serializer_class = CourseSectionSerializer
    permission_classes = [IsCourseOwnerOrAdmin]

    def get_course(self, obj):
        return obj.course

    def get_queryset(self):
        qs = super().get_queryset().filter(_visible_course_filter(self.request.user, prefix='course__'))
        course_id = self.request.query_params.get('course')
        if course_id:
            qs = qs.filter(course_id=course_id)
        return qs

    def perform_create(self, serializer):
        course = serializer.validated_data['course']
        user = self.request.user
        if course.teacher_id != user.id and not user.is_staff:
            raise PermissionDenied("You don't own this course.")
        serializer.save()


class CourseLessonViewSet(viewsets.ModelViewSet):
    queryset = CourseLesson.objects.select_related('section__course', 'video', 'test')
    serializer_class = CourseLessonSerializer
    permission_classes = [IsCourseOwnerOrAdmin]

    def get_course(self, obj):
        return obj.section.course

    def get_queryset(self):
        qs = super().get_queryset().filter(_visible_course_filter(self.request.user, prefix='section__course__'))
        section_id = self.request.query_params.get('section')
        if section_id:
            qs = qs.filter(section_id=section_id)
        return qs

    def perform_create(self, serializer):
        section = serializer.validated_data['section']
        user = self.request.user
        if section.course.teacher_id != user.id and not user.is_staff:
            raise PermissionDenied("You don't own this course.")
        serializer.save()


class CourseEnrollmentViewSet(viewsets.ModelViewSet):
    """Admin-only for now — Phase 1 has no purchase flow yet, so enrollments
    are manually granted the same way billing.Purchase.approve() already
    grants courses.Enrollment access elsewhere in this codebase."""
    queryset = CourseEnrollment.objects.select_related('user', 'course')
    serializer_class = CourseEnrollmentSerializer
    permission_classes = [IsAdminRoleOrAbove]

    def get_queryset(self):
        qs = super().get_queryset()
        course_id = self.request.query_params.get('course')
        if course_id:
            qs = qs.filter(course_id=course_id)
        return qs

    def perform_create(self, serializer):
        course = serializer.validated_data['course']
        expires_at = None
        if course.access_duration_type != 'lifetime':
            days = ACCESS_DURATION_DAYS.get(course.access_duration_type) or course.access_duration_days or 0
            if days:
                expires_at = timezone.now() + timedelta(days=days)
        serializer.save(source='admin_grant', expires_at=expires_at)


class MyCourseEnrollmentsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = CourseEnrollment.objects.filter(user=request.user, is_active=True).select_related('course')
        return Response(CourseEnrollmentSerializer(qs, many=True).data)
