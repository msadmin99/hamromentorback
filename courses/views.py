from django.conf import settings
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from hamromentor.permissions import IsAdminRoleOrAbove, IsAdminRoleOrAboveOrReadOnly, IsSuperAdmin

from .models import (
    Course,
    CoursePackage,
    Enrollment,
    EnrollmentRequest,
)
from .serializers import (
    CoursePackageSerializer,
    CourseSerializer,
    EnrollmentRequestSerializer,
    EnrollmentSerializer,
)


class CourseViewSet(viewsets.ModelViewSet):
    queryset = Course.objects.all()
    serializer_class = CourseSerializer
    permission_classes = [IsAdminRoleOrAboveOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if not (user.is_authenticated and user.is_staff):
            qs = qs.filter(is_active=True)
        return qs


class CoursePackageViewSet(viewsets.ModelViewSet):
    queryset = CoursePackage.objects.all()
    serializer_class = CoursePackageSerializer
    permission_classes = [IsSuperAdmin]

    def get_queryset(self):
        qs = super().get_queryset()
        course_id = self.request.query_params.get('course')
        if course_id:
            qs = qs.filter(course_id=course_id)
        return qs


class EnrollmentViewSet(viewsets.ModelViewSet):
    queryset = Enrollment.objects.select_related('user', 'course').all()
    serializer_class = EnrollmentSerializer
    permission_classes = [IsAdminRoleOrAbove]

    def get_queryset(self):
        qs = super().get_queryset()
        search = self.request.query_params.get('search')
        course_id = self.request.query_params.get('course')
        date_from = self.request.query_params.get('from')
        date_to = self.request.query_params.get('to')
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(user__first_name__icontains=search) | Q(user__last_name__icontains=search)
                | Q(user__email__icontains=search) | Q(user__phone__icontains=search)
                | Q(student_code__icontains=search)
            )
        if course_id:
            qs = qs.filter(course_id=course_id)
        if date_from:
            qs = qs.filter(enrolled_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(enrolled_at__date__lte=date_to)
        return qs


class MyEnrollmentsView(APIView):
    """A student's own active enrollments — powers the 'Your Course' and
    'Your Plan Validity' rows in the profile menu."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Enrollment.objects.filter(user=request.user, is_active=True).select_related('user', 'course', 'package')
        return Response(EnrollmentSerializer(qs, many=True).data)


class EnrollmentRequestViewSet(viewsets.ModelViewSet):
    queryset = EnrollmentRequest.objects.select_related('user', 'course', 'package').all()
    serializer_class = EnrollmentRequestSerializer
    permission_classes = [IsAdminRoleOrAbove]

    def get_queryset(self):
        qs = super().get_queryset()
        status_filter = self.request.query_params.get('status')
        course_id = self.request.query_params.get('course')
        search = self.request.query_params.get('search')
        if status_filter and status_filter != 'all':
            qs = qs.filter(status=status_filter)
        if course_id:
            qs = qs.filter(course_id=course_id)
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(user__first_name__icontains=search) | Q(user__email__icontains=search)
                | Q(student_code__icontains=search)
            )
        return qs

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        req = self.get_object()
        req.status = 'approved'
        req.decided_at = timezone.now()
        req.save()
        Enrollment.objects.update_or_create(
            user=req.user, course=req.course,
            defaults={
                'package': req.package,
                'access_type': 'package' if req.package else 'free',
                'is_active': True,
            },
        )
        return Response(EnrollmentRequestSerializer(req).data)

    @action(detail=True, methods=['post'])
    def decline(self, request, pk=None):
        req = self.get_object()
        req.status = 'declined'
        req.decided_at = timezone.now()
        req.save()
        return Response(EnrollmentRequestSerializer(req).data)


class DashboardStatsView(APIView):
    permission_classes = [IsAdminRoleOrAbove]

    def get(self, request):
        from django.contrib.auth import get_user_model

        from academics.models import Question
        from tests_app.models import Test

        User = get_user_model()

        now = timezone.now()
        live_daily_tests = Test.objects.filter(
            exam_type='daily', scheduled_start__lte=now, scheduled_end__gte=now,
        ).prefetch_related('courses')

        questions_by_course = []
        for course in Course.objects.all():
            questions_by_course.append({
                'course': course.name,
                'count': course.questions.count(),
            })

        return Response({
            'students_registered': User.objects.filter(is_staff=False).count(),
            'total_courses': Course.objects.count(),
            'course_enrollments': Enrollment.objects.filter(is_active=True).count(),
            'package_enrollments': Enrollment.objects.filter(access_type='package', is_active=True).count(),
            'live_daily_exams_now': live_daily_tests.count(),
            'active_mock_tests': Test.objects.filter(exam_type='mock').count(),
            'total_questions': Question.objects.count(),
            'live_right_now': [
                {
                    'course': ', '.join(c.name for c in t.courses.all()) or 'All courses',
                    'label': t.title,
                    'exam_date': t.scheduled_start.date() if t.scheduled_start else None,
                }
                for t in live_daily_tests
            ],
            'questions_by_course': questions_by_course,
        })


def _check_cron_secret(request):
    provided = request.headers.get('X-Cron-Secret') or request.query_params.get('secret')
    return provided == settings.CRON_SECRET


class PruneExpiredPackagesView(APIView):
    """POST /api/cron/prune-expired-packages/ — housekeeping only: a package's access
    already stops the instant it expires or is revoked, this just deletes long-stale
    grant records so admin lists stay clean."""
    permission_classes = [AllowAny]

    def post(self, request):
        if not _check_cron_secret(request):
            return Response({'detail': 'Invalid or missing cron secret.'}, status=status.HTTP_401_UNAUTHORIZED)
        stale = Enrollment.objects.filter(expires_at__lt=timezone.now(), access_type='package')
        count = stale.count()
        stale.delete()
        return Response({'deleted': count})
