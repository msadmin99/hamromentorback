from datetime import datetime

from django.db import OperationalError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from academics.models import Option
from academics.services import record_question_result
from billing.access import (
    consume_quota,
    get_grand_test_access,
    has_daily_test_access,
    has_mock_test_access,
    has_pyq_access,
    is_preview_only,
)
from hamromentor.permissions import IsStaffOrReadOnly

from . import performance
from .exam_versioning import RescheduleError, clone_test_as_new_version, create_reschedule_session
from .models import Answer, ExamSession, ExamTemplate, Test, TestAttempt
from .serializers import (
    ExamSessionSerializer,
    ExamTemplateSerializer,
    RescheduleSerializer,
    SessionAttemptSerializer,
    StartTestSerializer,
    SubmitAnswerSerializer,
    TestAdminSerializer,
    TestAttemptSerializer,
    TestAttemptSummarySerializer,
    TestDetailSerializer,
    TestListSerializer,
    TestResultSerializer,
)


def _start_attempt(request, test, session=None):
    """Shared by TestViewSet.start (legacy route, session=None — behavior is
    byte-for-byte what it was before Exam Sessions existed) and
    ExamSessionViewSet.start (new route — additionally enforces the
    session's time window/status/access)."""
    serializer = StartTestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    submitted_password = serializer.validated_data.get('access_password')

    if session:
        session.refresh_status()
        if session.status == 'cancelled':
            return Response({'detail': 'This session has been cancelled.'}, status=status.HTTP_403_FORBIDDEN)
        if session.status == 'draft':
            return Response({'detail': 'This session is not yet open.'}, status=status.HTTP_403_FORBIDDEN)
        now = timezone.now()
        if now < session.start_datetime:
            return Response({'detail': 'This session has not started yet.'}, status=status.HTTP_403_FORBIDDEN)
        if now > session.end_datetime:
            return Response({'detail': 'This session has ended.'}, status=status.HTTP_403_FORBIDDEN)
        if session.access_type == 'private' and submitted_password != session.password:
            return Response({'detail': 'Incorrect session password.'}, status=status.HTTP_403_FORBIDDEN)

    if test.exam_type == 'grand' and test.is_pro:
        access = get_grand_test_access(request.user, test)
        if not access:
            return Response(
                {'detail': 'This Grand Test requires purchase.', 'code': 'purchase_required'},
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )
        if submitted_password != access.password:
            return Response({'detail': 'Incorrect exam password.'}, status=status.HTTP_403_FORBIDDEN)
    elif test.access_password and submitted_password != test.access_password:
        return Response({'detail': 'Incorrect test password.'}, status=status.HTTP_403_FORBIDDEN)
    elif test.exam_type in ('mock', 'qbank') and test.is_pro and not has_mock_test_access(request.user, test):
        return Response(
            {'detail': 'This Mock Test requires an active subscription.', 'code': 'purchase_required'},
            status=status.HTTP_402_PAYMENT_REQUIRED,
        )
    elif (
        test.exam_type == 'daily' and test.is_pro
        and not has_daily_test_access(request.user, test) and test.free_preview_questions <= 0
    ):
        return Response(
            {'detail': 'This Daily Test requires an active subscription.', 'code': 'purchase_required'},
            status=status.HTTP_402_PAYMENT_REQUIRED,
        )
    elif test.exam_type == 'pyq' and test.is_pro and not has_pyq_access(request.user, test):
        return Response(
            {'detail': 'This Past Year Questions test requires an active membership.', 'code': 'purchase_required'},
            status=status.HTTP_402_PAYMENT_REQUIRED,
        )

    attempt_qs = test.attempts.filter(user=request.user, session=session)
    existing = attempt_qs.filter(status='in_progress').first()
    if existing:
        return Response(TestAttemptSerializer(existing, context={'request': request}).data)

    attempt_count = attempt_qs.count()
    max_attempts = session.max_attempts if session else test.max_attempts
    if attempt_count >= max_attempts:
        return Response({'detail': 'Maximum attempts reached for this test.'}, status=status.HTTP_403_FORBIDDEN)

    attempt = TestAttempt.objects.create(
        user=request.user, test=test, session=session, attempt_number=attempt_count + 1,
    )

    if test.exam_type in ('mock', 'qbank') and test.is_pro:
        consume_quota(request.user, 'mock_test')

    return Response(
        TestAttemptSerializer(attempt, context={'request': request}).data,
        status=status.HTTP_201_CREATED,
    )


class TestViewSet(viewsets.ModelViewSet):
    queryset = Test.objects.all()
    permission_classes = [IsStaffOrReadOnly]

    def destroy(self, request, *args, **kwargs):
        from core.deletion_audit import record_deletion

        test = self.get_object()
        label = test.title

        if test.attempts.exists():
            msg = 'This exam has student attempts and cannot be deleted — archive it instead.'
            record_deletion(request, 'Test', test.id, label, result='failure', failure_reason=msg)
            return Response({'detail': msg}, status=status.HTTP_400_BAD_REQUEST)
        # ExamSession.exam_version is on_delete=PROTECT — ANY session pointing at
        # this Test row blocks the delete at the DB level regardless of how many
        # other versions its exam_template has (a prior version of this check only
        # blocked when this was the *sole* version, which let a ProtectedError
        # reach super().destroy() unhandled — a 500 instead of this clean 400).
        if test.sessions.exists():
            msg = 'This exam version has scheduled sessions and cannot be deleted — cancel its sessions first.'
            record_deletion(request, 'Test', test.id, label, result='failure', failure_reason=msg)
            return Response({'detail': msg}, status=status.HTTP_400_BAD_REQUEST)

        try:
            response = super().destroy(request, *args, **kwargs)
        except Exception as exc:
            record_deletion(request, 'Test', test.id, label, result='failure', failure_reason=str(exc)[:500])
            return Response({'detail': 'Deletion failed. No partial deletion should remain.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        record_deletion(request, 'Test', test.id, label, result='success')
        return response

    def get_serializer_class(self):
        user = self.request.user
        if user.is_authenticated and user.is_staff:
            if self.request.method != 'GET' or self.action == 'retrieve':
                return TestAdminSerializer
            return TestListSerializer
        if self.action == 'retrieve':
            return TestDetailSerializer
        return TestListSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        exam_type = self.request.query_params.get('exam_type') or self.request.query_params.get('group')
        subject = self.request.query_params.get('subject')
        year = self.request.query_params.get('year')
        university = self.request.query_params.get('university')
        course_id = self.request.query_params.get('course')
        if exam_type:
            qs = qs.filter(exam_type__in=exam_type.split(','))
        if subject:
            qs = qs.filter(subject__slug=subject)
        if year:
            qs = qs.filter(academic_year=year)
        if university:
            qs = qs.filter(university=university)

        user = self.request.user
        if not (user.is_authenticated and user.is_staff):
            qs = qs.filter(is_draft=False)
        if course_id and not (user.is_authenticated and user.is_staff):
            from django.db.models import Q
            qs = qs.filter(Q(courses__id=course_id) | Q(courses__isnull=True))
        if user.is_authenticated and getattr(user, 'admin_role', None) == 'teacher' and not user.can_manage_all_content:
            qs = qs.filter(created_by=user)
        return qs.distinct()

    @action(detail=False, methods=['get'])
    def universities(self, request):
        """Distinct conducting institutions (IOM, MOE, BPKIHS, KU, ...) available for Past Year
        Question sets — the top-level grouping on the student Past Year Questions page,
        optionally scoped to a course."""
        # .order_by() clears Test's default ordering (['-scheduled_start', '-created_at']) —
        # without it, Django has to include those fields in the SELECT to satisfy the implicit
        # ORDER BY on a .distinct() query, so DISTINCT ends up operating over
        # (university, scheduled_start, created_at) instead of just university, and silently
        # stops deduplicating the moment two rows share a university but differ in timestamp.
        qs = self.get_queryset().filter(exam_type='pyq').exclude(university='').order_by()
        universities = sorted(qs.values_list('university', flat=True).distinct())
        return Response(universities)

    @action(detail=False, methods=['get'])
    def years(self, request):
        """Distinct academic years available for Past Year Question sets, optionally scoped to a
        course and/or a university (?university=IOM) — the level below University on the student
        Past Year Questions page."""
        qs = self.get_queryset().filter(exam_type='pyq').exclude(academic_year='').order_by()
        years = sorted(qs.values_list('academic_year', flat=True).distinct(), reverse=True)
        return Response(years)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def start(self, request, pk=None):
        return _start_attempt(request, self.get_object())

    @action(detail=True, methods=['post'])
    def reschedule(self, request, pk=None):
        """Reschedule / Schedule Again — creates a new ExamSession reusing
        this Test's question set and configuration by default. See
        exam_versioning.create_reschedule_session for the full flow
        (lazy template/session-1 adoption, double-click-safe locking,
        optional Create New Version when questions are being changed)."""
        test = self.get_object()
        serializer = RescheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        v = serializer.validated_data
        try:
            session = create_reschedule_session(
                test.id, request.user,
                session_name=v.get('session_name', ''),
                start_datetime=v['start_datetime'], end_datetime=v['end_datetime'],
                registration_deadline=v.get('registration_deadline'),
                tz_name=v.get('timezone', 'Asia/Kathmandu'),
                access_type=v.get('access_type', 'all'),
                access_course_ids=v.get('access_course_ids'),
                password=v.get('password', ''),
                max_attempts=v.get('max_attempts', 1),
                new_version=v.get('new_version', False),
                new_version_question_ids=v.get('new_version_question_ids'),
            )
        except RescheduleError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except OperationalError:
            # select_for_update() lock contention from a near-simultaneous duplicate
            # request (e.g. a double-click that got through client-side debouncing) —
            # the winning request already created the session; tell the loser to
            # refresh rather than show a raw 500.
            return Response(
                {'detail': 'This exam is already being rescheduled — please refresh and check Schedule History.'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(ExamSessionSerializer(session, context={'request': request}).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def duplicate(self, request, pk=None):
        """Duplicate Exam — an independent copy (new Test, new question-list
        copy, no exam_template link) as opposed to Reschedule, which reuses
        the same Test/question set under the same template."""
        test = self.get_object()
        new_test = clone_test_as_new_version(test, request.user, exam_template=None, title=f'{test.title} (Copy)')
        return Response(TestAdminSerializer(new_test, context={'request': request}).data, status=status.HTTP_201_CREATED)


def _teacher_scope(qs, user, field='created_by'):
    if user.is_authenticated and getattr(user, 'admin_role', None) == 'teacher' and not user.can_manage_all_content:
        qs = qs.filter(**{field: user})
    return qs


class ExamTemplateViewSet(viewsets.ModelViewSet):
    """Read-only from the API's point of view — a template is only ever
    created as a side effect of TestViewSet.reschedule (lazy adoption), never
    directly. Powers the Admin Exam List (one row per template) and the
    Schedule History view (its `sessions` action)."""
    queryset = ExamTemplate.objects.all()
    serializer_class = ExamTemplateSerializer
    permission_classes = [IsStaffOrReadOnly]
    http_method_names = ['get', 'head', 'options']

    def get_queryset(self):
        qs = super().get_queryset().prefetch_related('versions', 'sessions')
        return _teacher_scope(qs, self.request.user)

    @action(detail=True, methods=['get'])
    def sessions(self, request, pk=None):
        template = self.get_object()
        sessions = template.sessions.select_related('exam_version').all()
        return Response(ExamSessionSerializer(sessions, many=True, context={'request': request}).data)


class ExamSessionViewSet(viewsets.ModelViewSet):
    queryset = ExamSession.objects.all()
    serializer_class = ExamSessionSerializer
    permission_classes = [IsStaffOrReadOnly]
    http_method_names = ['get', 'put', 'patch', 'delete', 'post', 'head', 'options']

    def get_queryset(self):
        qs = super().get_queryset().select_related('exam_template', 'exam_version')
        user = self.request.user
        if not (user.is_authenticated and user.is_staff):
            qs = qs.exclude(status='draft')
        qs = _teacher_scope(qs, user, field='exam_template__created_by')
        template_id = self.request.query_params.get('exam_template')
        if template_id:
            qs = qs.filter(exam_template_id=template_id)
        if self.request.query_params.get('upcoming') == 'true':
            qs = qs.filter(status__in=['scheduled', 'registration_open', 'live'], end_datetime__gte=timezone.now())
        return qs

    def perform_update(self, serializer):
        session = self.get_object()
        if session.status == 'completed' or session.attempts.filter(status='submitted').exists():
            raise ValidationError('This session has already been conducted and can no longer be edited.')
        serializer.save()

    def destroy(self, request, *args, **kwargs):
        from core.deletion_audit import record_deletion

        session = self.get_object()
        label = session.session_name or f'session #{session.id}'

        if session.attempts.exists():
            msg = 'This session has attempts and cannot be deleted — cancel it instead.'
            record_deletion(request, 'ExamSession', session.id, label, result='failure', failure_reason=msg)
            return Response({'detail': msg}, status=status.HTTP_400_BAD_REQUEST)

        try:
            response = super().destroy(request, *args, **kwargs)
        except Exception as exc:
            record_deletion(request, 'ExamSession', session.id, label, result='failure', failure_reason=str(exc)[:500])
            return Response({'detail': 'Deletion failed. No partial deletion should remain.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        record_deletion(request, 'ExamSession', session.id, label, result='success')
        return response

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def start(self, request, pk=None):
        session = self.get_object()
        return _start_attempt(request, session.exam_version, session=session)

    @action(detail=True, methods=['get'])
    def attempts(self, request, pk=None):
        """Participants/Results for this session (Admin-only, staff already
        enforced by the ViewSet's default IsStaffOrReadOnly for GET-as-staff
        vs this being a detail action requiring the same permission)."""
        session = self.get_object()
        qs = session.attempts.select_related('user', 'test').order_by('-score')
        return Response(SessionAttemptSerializer(qs, many=True, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        session = self.get_object()
        if session.status == 'completed':
            return Response({'detail': 'A completed session cannot be cancelled.'}, status=status.HTTP_400_BAD_REQUEST)
        session.status = 'cancelled'
        session.save(update_fields=['status'])
        return Response(ExamSessionSerializer(session, context={'request': request}).data)


class AttemptDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, attempt_id):
        attempt = get_object_or_404(TestAttempt, pk=attempt_id, user=request.user)
        if attempt.status == 'submitted':
            return Response(TestResultSerializer(attempt, context={'request': request}).data)
        return Response(TestAttemptSerializer(attempt, context={'request': request}).data)


class SubmitAnswerView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, attempt_id):
        attempt = get_object_or_404(TestAttempt, pk=attempt_id, user=request.user, status='in_progress')
        serializer = SubmitAnswerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if is_preview_only(request.user, attempt.test):
            allowed_ids = set(
                attempt.test.questions.all()[:attempt.test.free_preview_questions].values_list('id', flat=True)
            )
            if data['question_id'] not in allowed_ids:
                return Response(
                    {'detail': 'Subscribe to unlock this question.', 'code': 'purchase_required'},
                    status=status.HTTP_402_PAYMENT_REQUIRED,
                )

        selected_option = None
        is_correct = False
        if data.get('option_id'):
            selected_option = get_object_or_404(Option, pk=data['option_id'], question_id=data['question_id'])
            is_correct = selected_option.is_correct

        Answer.objects.update_or_create(
            attempt=attempt, question_id=data['question_id'],
            defaults={
                'selected_option': selected_option,
                'is_correct': is_correct,
                'is_marked_for_review': data.get('mark_for_review', False),
            },
        )
        return Response({'saved': True})


class SubmitTestView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, attempt_id):
        attempt = get_object_or_404(TestAttempt, pk=attempt_id, user=request.user, status='in_progress')

        if is_preview_only(request.user, attempt.test):
            return Response(
                {'detail': 'Subscribe to submit this test.', 'code': 'purchase_required'},
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )

        score = 0.0
        correct = 0
        answered = attempt.answers.count()
        for answer in attempt.answers.select_related('question'):
            q = answer.question
            if answer.selected_option_id:
                if answer.is_correct:
                    score += float(q.marks)
                    correct += 1
                elif attempt.test.negative_marking:
                    score -= float(q.negative_marks)
                # Once per submitted attempt, not per answer-change (SubmitAnswerView
                # is update_or_create and can be hit many times while the student is
                # still deciding) — this is the platform-wide feed into Weak/Mastered/
                # Mistake Bank alongside QBank practice. Additive only: doesn't touch
                # Answer/TestAttempt/scoring above.
                record_question_result(request.user, q, answer.is_correct, source='test', selected_option=answer.selected_option)

        attempt.score = round(score, 2)
        attempt.accuracy = round((correct / answered) * 100, 2) if answered else 0
        attempt.end_time = timezone.now()
        attempt.status = 'submitted'
        attempt.save()

        # Ranking pool is scoped to the session when this attempt was made through
        # one, so rescheduled occurrences of the same exam never merge rankings —
        # every legacy attempt (session=None) keeps ranking exactly as it always
        # has, against every other session=None attempt on the same Test.
        ranking_pool = TestAttempt.objects.filter(test=attempt.test, session=attempt.session, status='submitted')
        submitted = list(ranking_pool.order_by('-score').values_list('id', flat=True))
        if attempt.id in submitted:
            attempt.rank = submitted.index(attempt.id) + 1
            total = len(submitted)
            attempt.percentile = round((total - attempt.rank) / total * 100, 2) if total > 1 else 100
            attempt.save(update_fields=['rank', 'percentile'])

        return Response(TestResultSerializer(attempt, context={'request': request}).data)


class TestResultView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, attempt_id):
        attempt = get_object_or_404(TestAttempt, pk=attempt_id, user=request.user)
        filter_type = request.query_params.get('filter', 'all')
        return Response(
            TestResultSerializer(attempt, context={'request': request, 'filter': filter_type}).data
        )


class MyAttemptsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        attempts = TestAttempt.objects.filter(user=request.user).select_related('test').order_by('-start_time')
        return Response(TestAttemptSummarySerializer(attempts, many=True, context={'request': request}).data)


def _parse_course(request):
    course = request.query_params.get('course')
    return int(course) if course else None


def _parse_date_range(request):
    """Accepts either ?days=7|30|90|all or explicit ?from=&to= (ISO datetimes).
    Explicit from/to wins if both are given."""
    date_from = parse_datetime(request.query_params.get('from', '') or '')
    date_to = parse_datetime(request.query_params.get('to', '') or '')
    if date_from or date_to:
        return date_from, date_to

    days = request.query_params.get('days', '30')
    if days == 'all':
        return None, None
    try:
        days_int = int(days)
    except ValueError:
        days_int = 30
    return timezone.now() - timezone.timedelta(days=days_int), None


class StudentPerformanceOverviewView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        course = _parse_course(request)
        date_from, date_to = _parse_date_range(request)
        granularity = request.query_params.get('granularity', 'day')

        return Response({
            'kpis': performance.kpi_overview(request.user, course, date_from, date_to),
            'trend': performance.trend_series(request.user, course, date_from, date_to, granularity),
            'subjects': performance.subject_breakdown(request.user, course),
            'mock_tests': performance.mock_test_analytics(request.user, course),
            'questions': performance.question_analytics(request.user, course),
            'strengths_weaknesses': performance.strengths_and_weaknesses(request.user, course),
            'recommendations': performance.recommendations(request.user, course),
        })


class SubjectPerformanceDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):
        return Response(performance.chapter_breakdown(request.user, subject_id))


class ExamTypeStatsView(APIView):
    """Powers the 'Your <Exam Type> Stats' sidebar panel on each test-listing
    page (Mock/Daily/Grand/PYQ/QBank)."""
    permission_classes = [IsAuthenticated]

    def get(self, request, exam_type):
        if exam_type not in dict(Test.EXAM_TYPE_CHOICES):
            return Response({'detail': 'Unknown exam_type.'}, status=400)
        course = _parse_course(request)
        return Response(performance.exam_type_stats(request.user, exam_type, course))


class PerformanceCalendarView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        course = _parse_course(request)
        month = request.query_params.get('month') or timezone.now().strftime('%Y-%m')
        try:
            datetime.strptime(month, '%Y-%m')
        except ValueError:
            return Response({'detail': 'month must be in YYYY-MM format.'}, status=400)
        return Response(performance.activity_calendar(request.user, course, month))


class AttemptComparativeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, attempt_id):
        if not TestAttempt.objects.filter(pk=attempt_id, user=request.user).exists():
            raise NotFound('Attempt not found.')
        return Response(performance.comparative(request.user, attempt_id))
