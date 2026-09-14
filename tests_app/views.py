import logging
from datetime import datetime

from django.db import OperationalError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from academics.models import Option
from billing.access import (
    consume_quota,
    get_grand_test_access,
    has_daily_test_access,
    has_mock_test_access,
    has_pyq_access,
)
from entitlements.provisioning import ensure_and_consume_free_starter, has_free_starter_available
from entitlements.services import can_review_attempt
from hamromentor.errors import error_response
from hamromentor.permissions import HasFeature, IsStaffOrReadOnly

from . import performance
from .access import can_access_test, visible_test_queryset
from .grand_test_admin import (
    grand_test_monitor as _grand_test_monitor_data,
    grand_test_participants as _grand_test_participants_data,
    grand_test_report as _grand_test_report_data,
)
from .exam_versioning import RescheduleError, clone_test_as_new_version, create_reschedule_session
from .lifecycle import (
    attempt_is_preview_only,
    attempt_preview_question_ids,
    effective_attempt_end,
    ensure_finalized_if_expired,
    finalize_attempt,
    freeze_attempt_questions,
    grand_test_participation_status,
    grand_test_review_window,
    is_attempt_expired,
    resolve_test_schedule_session,
)
from .models import Answer, ExamSession, ExamTemplate, SavedExamView, Test, TestAttempt, TestQuestion
from .policy import get_all_exam_type_defaults
from .preview import resolve_preview_user
from .stats_tasks import process_question_stats

logger = logging.getLogger(__name__)
from .serializers import (
    ExamSessionSerializer,
    ExamTemplateSerializer,
    MissedReviewQuestionSerializer,
    RescheduleSerializer,
    SavedExamViewSerializer,
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


class _ExamBrowsePagination(PageNumberPagination):
    """Same opt-in-only precedent as academics.views._BrowsePagination —
    GET /tests/ and GET /exam-templates/ stay bare-array-unpaginated for
    their existing callers (student test listings, the Admin videos page,
    the teacher course editor); the new Exam Management dashboard uses
    these dedicated `browse` actions instead."""
    page_size = 20
    max_page_size = 50
    page_size_query_param = 'page_size'


class _BoundedListPagination(PageNumberPagination):
    """Caps GET /tests/ at a real DB-level LIMIT without changing its
    response shape — every one of its 7 confirmed callers (student test
    listings across Daily/Mock/Grand/PYQ/home, the Admin videos page,
    the teacher course editor) reads the response as a bare array, and
    none of them is the "browse the whole catalog" use case (the Admin
    Exam Management dashboard already uses the paginated `browse` action
    above for that). This still issues LIMIT 200 at the query level —
    every one of these callers is already filtered by exam_type/course/
    university/etc. and realistically returns far fewer rows than that;
    the cap exists purely so a request with no filters at all can never
    return the entire table as it scales toward 100k+ questions' worth
    of exams."""
    page_size = 200
    max_page_size = 200

    def get_paginated_response(self, data):
        return Response(data)


def _free_starter_denied_payload():
    """Phase 3, Step 19 — a machine-readable denial shape the frontend can
    use to render an upgrade prompt, additive alongside the existing
    `detail`/`code` fields these 402 responses already return. Not a
    breaking response-shape change."""
    return {'reason': 'free_limit_reached', 'source': 'free_starter', 'upgrade_available': True}


def _free_starter_eligibility(user, has_prior_attempt, resource_type):
    """Returns (eligible, should_consume) for a free-starter fallback
    check inside _start_attempt.

    A student who already has ANY prior attempt of this specific test
    (any status) necessarily reached it via free-starter — this branch is
    only ever reached when the student LACKS real commercial access
    (has_mock_test_access/has_daily_test_access/has_pyq_access/
    GrandTestAccess all already failed above), so a prior attempt without
    real access can only mean a previous free-starter grant. Such a
    student is always allowed through again (resume, or a second attempt
    if the test's own max_attempts permits it) without a second
    consumption — matches Step 11's "do not consume a Mock Test quota
    repeatedly" rule, generalized to every resource type gated here.

    A genuinely first-time attempt is gated on, and (by the caller, once
    every other check has also passed) consumes, real remaining
    free-starter quota.

    Staff/admin accounts are never eligible at all (Step 31: must never
    be treated as normal free users, must never accidentally consume free
    quota) — preserves the exact pre-Phase-3 behavior for staff.
    """
    if user.is_staff:
        return False, False
    if has_prior_attempt:
        return True, False
    return has_free_starter_available(user, resource_type), True


def _start_attempt(request, test, session=None):
    """Shared by TestViewSet.start (legacy route — behavior is byte-for-byte
    what it was before Exam Sessions existed, FOR A TEST THAT HAS NEVER
    BEEN SCHEDULED) and ExamSessionViewSet.start (explicitly enforces the
    session's time window/status/access).

    GT3-2 fix: if the caller didn't pass a session but this Test has one
    anyway (scheduled via TestViewSet.reschedule / exam_versioning), it is
    auto-resolved here rather than silently skipped — closing a real
    bypass where a student could call the legacy /tests/{id}/start/ route
    directly to start (or, more importantly for Grand Test 3.0, to appear
    to have 'attempted') an exam whose real, scheduled window had already
    closed, entirely ignoring that window's own start/end/status checks
    below (which only ever ran `if session:`). This is what makes MISSED
    (tests_app.lifecycle.grand_test_participation_status) impossible to
    dodge merely by choosing a different URL — the server, not the route
    the client happened to call, decides. A genuinely unscheduled Test
    (test.sessions is empty) is completely unaffected — resolve_test_
    schedule_session() returns None for it, exactly as before."""
    if session is None:
        session = resolve_test_schedule_session(test)

    serializer = StartTestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    submitted_password = serializer.validated_data.get('access_password')

    # Academic-eligibility gate — the actual enforcement point for "a student
    # must not be able to access an unauthorized exam by changing the URL,
    # changing the exam ID, or calling the API directly." Listing (get_queryset)
    # already filters correctly, but this is what stops a leaked/guessed Test
    # ID from bypassing that filter entirely.
    if not can_access_test(request.user, test):
        return error_response(status.HTTP_403_FORBIDDEN, 'not_authorized', 'You do not have access to this exam.')

    # Phase 3 Free Starter: whether this student has EVER attempted this
    # specific Test before (any status) — computed once, up front. Fed
    # into _free_starter_eligibility() below, which distinguishes "first
    # attempt, gate and consume" from "already unlocked this test before,
    # always allow, never re-consume." See
    # docs/FREE_STARTER_USAGE_RULES.md §3/4/5.
    has_prior_attempt = test.attempts.filter(user=request.user).exists()

    if session:
        session.refresh_status()
        if session.status == 'cancelled':
            return error_response(status.HTTP_403_FORBIDDEN, 'session_cancelled', 'This session has been cancelled.')
        if session.status == 'draft':
            return error_response(status.HTTP_403_FORBIDDEN, 'session_not_open', 'This session is not yet open.')
        now = timezone.now()
        if now < session.start_datetime:
            return error_response(
                status.HTTP_403_FORBIDDEN, 'session_not_started', 'This session has not started yet.',
            )
        if now > session.end_datetime:
            # GT3-2: a Grand Test student who never started at all gets the
            # specific, approved MISSED message/code rather than the
            # generic 'session_ended' — distinct wording, distinct `code`,
            # so the Frontend can render the dedicated missed-state screen
            # (§12/§32 of the GT3-2 spec) instead of a plain error toast.
            # Every other exam type, and a Grand Test where an attempt
            # already exists (an unusual client action — see this
            # function's own resume-handling below, unreachable from here
            # since this check runs first), keep the existing generic
            # message unchanged.
            if test.exam_type == 'grand' and not test.attempts.filter(user=request.user, session=session).exists():
                return error_response(
                    status.HTTP_403_FORBIDDEN, 'grand_test_missed',
                    'Sorry, you missed this Grand Test during the scheduled examination time. '
                    'The examination cannot be attempted after the official closing time.',
                )
            return error_response(status.HTTP_403_FORBIDDEN, 'session_ended', 'This session has ended.')
        if session.access_type == 'private' and submitted_password != session.password:
            return error_response(
                status.HTTP_403_FORBIDDEN, 'invalid_session_password', 'Incorrect session password.',
            )
    elif test.exam_type in ('grand', 'daily') and test.scheduled_start and test.scheduled_end:
        # GT3-2: no real ExamSession exists for this Grand Test, but it
        # does have the simpler Test.scheduled_start/scheduled_end pair
        # (set directly on the exam builder, without using Reschedule/Exam
        # Sessions) — this is the more basic, likely more commonly used
        # scheduling mechanism, and until now it was purely a display
        # field (TestListSerializer.get_status's 'upcoming'/'ended'/'live'
        # badge) with NO actual enforcement here. That gap is exactly what
        # let a determined student start a "closed" Grand Test at any
        # time, defeating the entire "one official scheduled window"
        # premise. See grand_test_participation_status() — this mirrors
        # it exactly so enforcement and reporting can never disagree.
        #
        # Daily Test schedule audit: the identical gap existed for Daily
        # Test (its scheduled_start/end were equally display-only). Daily
        # Test's own requirement is a hard 24h window boundary — "at/after
        # scheduled_end, no new starting" — one instant stricter than
        # Grand Test's existing `now > scheduled_end` (which still allows
        # a start landing exactly ON the boundary). Branching the
        # comparison operator here, rather than changing it outright,
        # keeps Grand Test's own long-established behavior byte-for-byte
        # unchanged (see tests_gt3_2_missed_exam.py) while giving Daily
        # Test the stricter boundary its own spec calls for.
        now = timezone.now()
        if now < test.scheduled_start:
            if test.exam_type == 'grand':
                return error_response(
                    status.HTTP_403_FORBIDDEN, 'grand_test_not_started', 'This Grand Test has not started yet.',
                )
            return error_response(
                status.HTTP_403_FORBIDDEN, 'daily_test_not_started', 'This Daily Test has not opened yet.',
            )
        window_closed = now >= test.scheduled_end if test.exam_type == 'daily' else now > test.scheduled_end
        if window_closed and not test.attempts.filter(user=request.user, session__isnull=True).exists():
            if test.exam_type == 'grand':
                return error_response(
                    status.HTTP_403_FORBIDDEN, 'grand_test_missed',
                    'Sorry, you missed this Grand Test during the scheduled examination time. '
                    'The examination cannot be attempted after the official closing time.',
                )
            return error_response(
                status.HTTP_403_FORBIDDEN, 'daily_test_missed',
                "Sorry, this Daily Test's 24-hour window has closed. It can no longer be started.",
            )

    # Phase 3 Free Starter: which resource_type (if any) this attempt is
    # being granted through free-starter fallback — checked (never
    # consumed) here, so a later gate in this same function (a wrong
    # password, an exhausted attempt count) can still deny the request
    # without having already burned the student's quota. Actually consumed
    # only once, right before the attempt is created below — see
    # docs/FREE_STARTER_USAGE_RULES.md §3-6 and the has_free_starter_
    # available() docstring for exactly why consumption is deferred this
    # way (a real bug caught by this phase's own tests: consuming
    # immediately on "no Grand Test purchase found" let a wrong-password
    # guess burn the student's one free grant before they got to enter the
    # real password).
    free_starter_resource = None

    if test.exam_type == 'grand' and test.is_pro:
        access = get_grand_test_access(request.user, test)
        if not access:
            eligible, should_consume = _free_starter_eligibility(request.user, has_prior_attempt, 'grand_test')
            if not eligible:
                return error_response(
                    status.HTTP_402_PAYMENT_REQUIRED, 'purchase_required', 'This Grand Test requires purchase.',
                    access_denied=_free_starter_denied_payload(),
                )
            # Free-starter-eligible entry still respects the exam's own
            # optional password (an additional layer, never a substitute
            # for entitlement — GrandTestAccess.password doesn't apply here
            # since no GrandTestAccess grant exists on this path). GT3-3
            # deliberately leaves this branch untouched — it gates a
            # DIFFERENT population (a student with no paid entitlement at
            # all) through a DIFFERENT, admin-set, Test-level password,
            # not the per-student GrandTestAccess.password this phase
            # removes below.
            if test.access_password and submitted_password != test.access_password:
                return error_response(status.HTTP_403_FORBIDDEN, 'invalid_test_password', 'Incorrect test password.')
            if should_consume:
                free_starter_resource = 'grand_test'
        # GT3-3: an entitled student (a real, non-revoked GrandTestAccess
        # row exists) no longer needs to submit access.password at all —
        # authorization for this population is now exactly
        # `authenticated user + valid GrandTestAccess + valid schedule`
        # (the schedule half already enforced above/below, unchanged from
        # GT3-2). The password CHECK is removed; the password FIELD,
        # generator, and confirmation email are all deliberately left in
        # place (no destructive migration, easy rollback) — see this
        # phase's own completion report for why. A student who submits a
        # password anyway (stale frontend build, or one who still has the
        # emailed value) is simply ignored here, not rejected — the
        # field is no longer consulted for this population, in either
        # direction.
    elif test.access_password and submitted_password != test.access_password:
        return error_response(status.HTTP_403_FORBIDDEN, 'invalid_test_password', 'Incorrect test password.')
    elif test.exam_type in ('mock', 'qbank') and test.is_pro and not has_mock_test_access(request.user, test):
        eligible, should_consume = _free_starter_eligibility(request.user, has_prior_attempt, 'mock_test')
        if not eligible:
            return error_response(
                status.HTTP_402_PAYMENT_REQUIRED, 'purchase_required',
                'This Mock Test requires an active subscription.', access_denied=_free_starter_denied_payload(),
            )
        if should_consume:
            free_starter_resource = 'mock_test'
    elif (
        test.exam_type == 'daily' and test.is_pro
        and not has_daily_test_access(request.user, test) and test.free_preview_questions <= 0
    ):
        eligible, should_consume = _free_starter_eligibility(request.user, has_prior_attempt, 'daily_test')
        if not eligible:
            return error_response(
                status.HTTP_402_PAYMENT_REQUIRED, 'purchase_required',
                'This Daily Test requires an active subscription.', access_denied=_free_starter_denied_payload(),
            )
        if should_consume:
            free_starter_resource = 'daily_test'
    elif test.exam_type == 'pyq' and test.is_pro and not has_pyq_access(request.user, test):
        eligible, should_consume = _free_starter_eligibility(request.user, has_prior_attempt, 'pyq')
        if not eligible:
            return error_response(
                status.HTTP_402_PAYMENT_REQUIRED, 'purchase_required',
                'This Past Year Questions test requires an active membership.',
                access_denied=_free_starter_denied_payload(),
            )
        if should_consume:
            free_starter_resource = 'pyq'

    # GT3-7 §42 — start concurrency: everything from here through the
    # actual TestAttempt.objects.create() below used to be a plain
    # read-then-write with no lock and no DB constraint, so two
    # simultaneous start requests from the SAME user (a double-tap, two
    # open tabs, a retried request) could both observe "no in_progress
    # attempt yet" and both create one — two 'official' attempts for one
    # exam, defeating "one official attempt" (a real, reproduced race,
    # not theoretical). A DB-level partial/conditional unique constraint
    # (UniqueConstraint(condition=...)) would be the more standard fix,
    # but this project's production database is MySQL, which Django's ORM
    # does not support conditional unique constraints against
    # (supports_partial_indexes=False) — so the guard instead locks this
    # one user's own row for the duration of the check-then-create,
    # serializing only this user's own concurrent requests against each
    # other (select_for_update on a row that already exists, unlike the
    # not-yet-created TestAttempt). Every other student's start request
    # locks a completely different row and proceeds independently, so
    # this adds no contention for the real target scenario (many
    # different students starting at the same instant — GT3-7 §41
    # Scenario A).
    try:
        with transaction.atomic():
            request.user.__class__.objects.select_for_update().get(pk=request.user.pk)

            attempt_qs = test.attempts.filter(user=request.user, session=session)
            existing = attempt_qs.filter(status='in_progress').first()
            if existing:
                # Phase 6: a "resume" that lands after the effective deadline has
                # passed finalizes the stale attempt instead of handing back a
                # dead attempt for the frontend to render as if still answerable
                # — then falls through to the ordinary attempt-count/new-attempt
                # logic below, exactly as if this had never been in progress.
                existing = ensure_finalized_if_expired(existing)
                if existing.status == 'in_progress':
                    return Response(TestAttemptSerializer(existing, context={'request': request}).data)

            attempt_count = attempt_qs.count()
            max_attempts = session.max_attempts if session else test.max_attempts
            if attempt_count >= max_attempts:
                return error_response(
                    status.HTTP_403_FORBIDDEN, 'max_attempts_reached', 'Maximum attempts reached for this test.',
                )

            attempt = TestAttempt.objects.create(
                user=request.user, test=test, session=session, attempt_number=attempt_count + 1,
            )
            # Phase 9: freeze this attempt's question set/order exactly once, right
            # here — the single point every subsequent GET/answer/submit reads
            # from instead of recomputing. See freeze_attempt_questions' own
            # docstring for the incident this fixes.
            freeze_attempt_questions(attempt)
    except OperationalError:
        # Same defensive pattern as TestViewSet.reschedule's own
        # select_for_update() lock-contention handling — on a backend
        # without true blocking row locks (SQLite, used locally/in tests),
        # a near-simultaneous duplicate request can surface as "database is
        # locked" instead of waiting; tell the loser to retry rather than
        # show a raw 500. On production MySQL, the second request instead
        # blocks and proceeds normally once the first commits (InnoDB row
        # locking), so this branch is expected to be effectively unreachable
        # there.
        return Response(
            {'detail': 'Please try again — a duplicate start request was in progress.'},
            status=status.HTTP_409_CONFLICT,
        )

    if free_starter_resource:
        ensure_and_consume_free_starter(request.user, free_starter_resource)
    elif test.exam_type in ('mock', 'qbank') and test.is_pro:
        consume_quota(request.user, 'mock_test')

    return Response(
        TestAttemptSerializer(attempt, context={'request': request}).data,
        status=status.HTTP_201_CREATED,
    )


def _exam_stats(program=None):
    """'Exam' means one exam identity: either an ExamTemplate (grouping its
    versions/sessions) or a standalone Test that's never been rescheduled —
    matching how the list UI counts rows. Shared by TestViewSet.stats (one
    program, or overall) and .stats_by_program (looped per program)."""
    from django.db.models import OuterRef, Subquery

    from academics.models import Question

    template_qs = ExamTemplate.objects.all()
    standalone_qs = Test.objects.filter(exam_template__isnull=True)
    attempt_qs = TestAttempt.objects.all()
    question_qs = Question.objects.all()
    if program:
        template_qs = template_qs.filter(versions__courses__program_group=program).distinct()
        standalone_qs = standalone_qs.filter(courses__program_group=program).distinct()
        attempt_qs = attempt_qs.filter(test__courses__program_group=program).distinct()
        question_qs = question_qs.filter(courses__program_group=program).distinct()

    latest_version_draft = Test.objects.filter(
        exam_template=OuterRef('pk')
    ).order_by('-version_number', '-created_at').values('is_draft')[:1]
    template_qs = template_qs.annotate(latest_is_draft=Subquery(latest_version_draft))

    published_exams = template_qs.filter(latest_is_draft=False).count() + standalone_qs.filter(is_draft=False).count()
    draft_exams = template_qs.filter(latest_is_draft=True).count() + standalone_qs.filter(is_draft=True).count()
    scheduled_exams = template_qs.filter(sessions__status__in=['scheduled', 'registration_open']).distinct().count()

    return {
        'total_exams': template_qs.count() + standalone_qs.count(),
        'published_exams': published_exams,
        'draft_exams': draft_exams,
        'scheduled_exams': scheduled_exams,
        'total_questions': question_qs.count(),
        'total_attempts': attempt_qs.count(),
    }


class TestViewSet(viewsets.ModelViewSet):
    queryset = Test.objects.all()
    permission_classes = [IsStaffOrReadOnly]
    pagination_class = _BoundedListPagination

    def get_permissions(self):
        # FIX (P0 security audit): plain IsStaffOrReadOnly meant ANY staff
        # account — including Editor/Teacher roles the product explicitly
        # scopes via the 'exam_delete' feature key everywhere else
        # (RolePermission, EXAM_MANAGEMENT_FEATURES) — could delete any
        # exam. Only 'destroy' is gated here; every other action keeps its
        # existing IsStaffOrReadOnly behavior unchanged (see the note on
        # .reschedule below for the second gated action, and the note in
        # MASTER_PLATFORM_AUDIT_REPORT.md §36 for why publish/archive
        # (a generic PATCH of is_draft, not a distinct action) and
        # .duplicate are deliberately NOT gated here — no EXAM_MANAGEMENT_
        # FEATURES key claims to cover them, and inferring one from a PATCH
        # payload is a bigger, separate change).
        if self.action == 'destroy':
            return [HasFeature('exam_delete')()]
        return super().get_permissions()

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

    def perform_update(self, serializer):
        """Phase 2 notification hook: publishing an exam (the plain
        `is_draft: True -> False` PATCH this ViewSet's own comment above
        already documents as "generic... not a distinct action") is the
        real moment a Daily/Grand Test actually becomes visible to
        students — tests_app.access.can_access_test denies everyone but
        staff/the creator while is_draft=True, so any ExamSession created
        for this Test before publish produced zero notifications (correct
        — no student could see it yet). This re-runs the same scheduling
        function for every not-yet-finished session under this Test the
        moment it's published, so the "available" event actually reaches
        its audience instead of silently never firing. dedupe_key makes
        this safe to call unconditionally for every still-open session —
        a session already notified (e.g. created after publish) is a
        no-op; only genuinely new recipients are created."""
        # serializer.instance still holds the pre-save field values here —
        # DRF sets it from get_object() before validation/save ever touch
        # it, so this is the old is_draft with no extra query.
        was_draft = serializer.instance.is_draft
        test = serializer.save()
        if was_draft and not test.is_draft and test.exam_type in ('grand', 'daily'):
            from notifications.exam_integration import schedule_session_reminders

            sessions = list(test.sessions.exclude(status__in=['completed', 'cancelled']))

            def _schedule_all():
                for session in sessions:
                    schedule_session_reminders(session)

            transaction.on_commit(_schedule_all)

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
        # Admin Exam Management dashboard filters — additive, safe for every
        # caller (student-facing pages simply never send them).
        program = self.request.query_params.get('program')
        search = self.request.query_params.get('search')
        status_param = self.request.query_params.get('status')
        standalone = self.request.query_params.get('standalone')
        access = self.request.query_params.get('access')
        difficulty = self.request.query_params.get('difficulty')
        if exam_type:
            qs = qs.filter(exam_type__in=exam_type.split(','))
        if subject:
            qs = qs.filter(subject__slug=subject)
        if year:
            qs = qs.filter(academic_year=year)
        if university:
            qs = qs.filter(university=university)
        if difficulty:
            qs = qs.filter(difficulty=difficulty)
        if program:
            qs = qs.filter(courses__program_group=program)
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(title__icontains=search)
                | Q(exam_template__exam_code__icontains=search)
                | Q(exam_template__title__icontains=search)
            )
        if status_param == 'draft':
            qs = qs.filter(is_draft=True)
        elif status_param == 'published':
            qs = qs.filter(is_draft=False)
        elif status_param == 'scheduled':
            qs = qs.filter(sessions__status__in=['scheduled', 'registration_open'])
        if standalone == 'true':
            qs = qs.filter(exam_template__isnull=True)
        if access == 'pro':
            qs = qs.filter(is_pro=True)
        elif access == 'free':
            qs = qs.filter(is_pro=False)

        user = self.request.user
        # Phase 10 "preview as student" (plan bullet 3): an admin can ask
        # for the catalog as one specific student sees it. Read-only and
        # GET-only by construction — see tests_app/preview.py. Returns None
        # (i.e. no change at all) for everyone else.
        preview_user = resolve_preview_user(self.request)
        if preview_user is not None:
            user = preview_user
        # Always-applied, server-derived eligibility filter — never opt-in on
        # whether the client happened to send ?course=, and never widened by
        # a client-supplied course id the student isn't actually enrolled in.
        qs = visible_test_queryset(user, qs)
        if course_id and not (user.is_authenticated and user.is_staff):
            # Narrows within the already-eligible set above (e.g. a student
            # enrolled in multiple courses picking one) — never a substitute
            # for it.
            qs = qs.filter(courses__id=course_id)
        if user.is_authenticated and getattr(user, 'admin_role', None) == 'teacher' and not user.can_manage_all_content:
            qs = qs.filter(created_by=user)

        # TestListSerializer needs subject.name, created_by.first_name/email,
        # and the full courses list per row — select_related/prefetch_related
        # these instead of one query per field per row (scalability audit:
        # ~8 queries/row -> ~163 queries for a 20-row page). annotated_*
        # replace the question_count/total_marks @property lookups (each of
        # which re-queried every related Question) with two DB-side
        # aggregates computed in the same query as the row fetch; distinct=True
        # on both guards against the classic Django multi-aggregate fan-out
        # since they aggregate over the same `questions` join.
        from django.db.models import Count, Prefetch, Sum

        qs = qs.select_related('subject', 'created_by').prefetch_related('courses').annotate(
            annotated_question_count=Count('questions', distinct=True),
            annotated_total_marks=Sum('questions__marks', distinct=True),
        )
        if user.is_authenticated:
            # One query for this user's attempts across every Test in the
            # page, instead of the 3 separate live queries per row
            # (best-score, in-progress, attempts-used) TestListSerializer
            # used to run. answered_count is annotated here too, so the
            # in-progress card's "answered_count" field needs zero further
            # queries even in the rare case a row does have one.
            # Explicit order, not a reliance on Meta.ordering combining
            # predictably with the annotate()-induced GROUP BY (it doesn't,
            # confirmed by a failing test — the "most recent among tied
            # scores" tiebreak in TestListSerializer._best_attempt needs a
            # genuinely deterministic input order to be stable, not an
            # assumed one).
            user_attempts_qs = TestAttempt.objects.filter(user=user).annotate(
                answered_count=Count('answers', distinct=True),
            ).order_by('-start_time')
            qs = qs.prefetch_related(Prefetch('attempts', queryset=user_attempts_qs, to_attr='_prefetched_user_attempts'))
        # Explicit order (matching Test.Meta.ordering) rather than relying on
        # the model default combining predictably with .distinct() — DRF's
        # paginator warns ("may yield inconsistent results") when it can't
        # positively confirm the queryset is deterministically ordered.
        return qs.distinct().order_by('-scheduled_start', '-created_at', '-id')

    @action(detail=False, methods=['get'])
    def universities(self, request):
        """Conducting institutions (IOM, MOE, BPKIHS, KU, ...) available for Past Year
        Question sets — the top-level grouping on the student Past Year Questions page,
        optionally scoped to a course. Each entry carries real, computed years-available
        and paper-count numbers (the "Choose a University" cards need these) — sole
        caller is Frontend/src/app/past-year-questions/page.js, confirmed via repo-wide
        grep, so this shape change is safe (no other consumer expects the old flat
        array of strings)."""
        # .order_by() clears Test's default ordering (['-scheduled_start', '-created_at']) —
        # without it, Django has to include those fields in the SELECT to satisfy the implicit
        # ORDER BY on a .distinct() query, so DISTINCT ends up operating over
        # (university, scheduled_start, created_at) instead of just university, and silently
        # stops deduplicating the moment two rows share a university but differ in timestamp.
        qs = self.get_queryset().filter(exam_type='pyq').exclude(university='').order_by()
        names = sorted(qs.values_list('university', flat=True).distinct())
        return Response([
            {
                'name': name,
                'years_available': qs.filter(university=name).exclude(academic_year='').values('academic_year').distinct().count(),
                'paper_count': qs.filter(university=name).count(),
            }
            for name in names
        ])

    @action(detail=False, methods=['get'])
    def years(self, request):
        """Distinct academic years available for Past Year Question sets, optionally scoped to a
        course and/or a university (?university=IOM) — the level below University on the student
        Past Year Questions page."""
        qs = self.get_queryset().filter(exam_type='pyq').exclude(academic_year='').order_by()
        years = sorted(qs.values_list('academic_year', flat=True).distinct(), reverse=True)
        return Response(years)

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def recommended(self, request):
        """Which single Daily/Mock/Grand Test to feature on that exam type's
        'Recommended For You' hero — real, computed picks only, no
        editorial/admin flag. Frontend follows up with GET /tests/{id}/ for
        full detail (price/access/etc.); this only decides *which* test."""
        from django.db.models import Count, Q

        exam_type = request.query_params.get('exam_type')
        if exam_type not in ('daily', 'mock', 'grand'):
            return Response({'detail': 'exam_type must be one of: daily, mock, grand.'}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        course = request.query_params.get('course')
        course_id = int(course) if course else None

        now = timezone.now()
        base_qs = self.get_queryset().filter(exam_type=exam_type)
        available_qs = base_qs.filter(
            Q(scheduled_start__isnull=True) | Q(scheduled_start__lte=now)
        ).filter(
            Q(scheduled_end__isnull=True) | Q(scheduled_end__gte=now)
        )
        attempted_test_ids = set(TestAttempt.objects.filter(user=user, test__exam_type=exam_type).values_list('test_id', flat=True))

        if exam_type == 'daily':
            not_attempted = available_qs.exclude(id__in=attempted_test_ids)
            for row in performance.subject_breakdown(user, course_id):
                if row['attempted'] < 3:
                    continue
                match = not_attempted.filter(subject_id=row['subject_id']).first()
                if match:
                    return Response({
                        'test_id': match.id, 'reason': 'weak_subject', 'weak_area': row['subject_name'],
                        'accuracy_pct': row['accuracy'], 'question_count': match.question_count, 'attempted_count': None,
                    })
            fallback = not_attempted.order_by('created_at').first()
            if not fallback:
                return Response({'test_id': None})
            return Response({
                'test_id': fallback.id, 'reason': 'new', 'weak_area': None,
                'accuracy_pct': None, 'question_count': fallback.question_count, 'attempted_count': None,
            })

        if exam_type == 'mock':
            top = available_qs.annotate(qc=Count('questions', distinct=True)).order_by('-qc', 'created_at').first()
            if not top:
                return Response({'test_id': None})
            attempted_count = TestAttempt.objects.filter(test=top).values('user').distinct().count()
            return Response({
                'test_id': top.id, 'reason': 'most_comprehensive', 'weak_area': None,
                'accuracy_pct': None, 'question_count': top.question_count, 'attempted_count': attempted_count,
            })

        # grand — most-attempted available Grand Test, oldest-first tiebreak
        # (also the natural fallback when nothing has been attempted yet).
        top = (
            available_qs.annotate(attempt_count=Count('attempts__user', distinct=True))
            .order_by('-attempt_count', 'created_at')
            .first()
        )
        if not top:
            return Response({'test_id': None})
        return Response({
            'test_id': top.id, 'reason': 'most_attempted', 'weak_area': None,
            'accuracy_pct': None, 'question_count': top.question_count,
            'attempted_count': TestAttempt.objects.filter(test=top).values('user').distinct().count(),
        })

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def start(self, request, pk=None):
        # Deliberately Test.objects (not self.get_object(), which is scoped
        # by get_queryset()'s eligibility filter) — a test outside that
        # filter must still resolve here so can_access_test() inside
        # _start_attempt() is the actual, explicit 403 enforcement point,
        # not an incidental 404 from the object lookup failing first.
        test = get_object_or_404(Test, pk=pk)
        return _start_attempt(request, test)

    @action(detail=True, methods=['post'], permission_classes=[HasFeature('exam_schedule')])
    def reschedule(self, request, pk=None):
        """Reschedule / Schedule Again — creates a new ExamSession reusing
        this Test's question set and configuration by default. See
        exam_versioning.create_reschedule_session for the full flow
        (lazy template/session-1 adoption, double-click-safe locking,
        optional Create New Version when questions are being changed).

        Gated on 'exam_schedule' (P0 security audit fix) — previously any
        staff account could reschedule any exam via plain IsStaffOrReadOnly.
        """
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

    @action(detail=False, methods=['post'])
    def access_preview(self, request):
        """'Who can see this exam?' preview (spec item 16) — computed from
        the NOT-YET-SAVED selection currently in the create/edit form (sent
        in the request body), not the saved Test row, so an admin can review
        it before ever hitting Confirm & Publish. POST-for-read is
        deliberate: the selection can be a large, arbitrary list of course/
        student/batch ids, awkward as query params. Inherits this ViewSet's
        IsStaffOrReadOnly, which already requires staff for non-GET methods."""
        from courses.models import Course, Enrollment

        course_ids = request.data.get('courses') or []
        student_ids = request.data.get('assigned_students') or []
        batch_ids = request.data.get('assigned_batches') or []

        eligible_user_ids = set()
        if course_ids:
            eligible_user_ids |= set(
                Enrollment.objects.filter(course_id__in=course_ids, is_active=True).values_list('user_id', flat=True)
            )
        if batch_ids:
            eligible_user_ids |= set(
                Enrollment.objects.filter(batch_id__in=batch_ids, is_active=True).values_list('user_id', flat=True)
            )
        eligible_user_ids |= {int(i) for i in student_ids}

        return Response({
            'eligible_count': len(eligible_user_ids),
            'courses': list(Course.objects.filter(id__in=course_ids).values('id', 'name')),
            'batch_count': len(batch_ids),
            'individual_student_count': len(student_ids),
        })

    @action(detail=True, methods=['post'])
    def duplicate(self, request, pk=None):
        """Duplicate Exam — an independent copy (new Test, new question-list
        copy, no exam_template link) as opposed to Reschedule, which reuses
        the same Test/question set under the same template."""
        test = self.get_object()
        new_test = clone_test_as_new_version(test, request.user, exam_template=None, title=f'{test.title} (Copy)')
        return Response(TestAdminSerializer(new_test, context={'request': request}).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'], permission_classes=[IsAdminUser])
    def browse(self, request):
        """Server-paginated, server-filtered variant of GET /tests/ for the
        Admin Exam Management dashboard (standalone, never-rescheduled exams
        — pass ?standalone=true; template-grouped exams come from
        ExamTemplateViewSet.browse instead). See _ExamBrowsePagination for
        why this is a separate opt-in action rather than pagination on list()."""
        qs = self.filter_queryset(self.get_queryset())
        paginator = _ExamBrowsePagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        # TestListSerializer (not TestAdminSerializer) — this is a display
        # listing; Edit already re-fetches full TestAdminSerializer detail
        # via GET /tests/{id}/, matching the existing openEdit() pattern.
        serializer = TestListSerializer(page, many=True, context={'request': request})
        return paginator.get_paginated_response(serializer.data)

    @action(detail=False, methods=['get'], permission_classes=[IsAdminUser])
    def stats(self, request):
        """Real, non-hardcoded numbers for the Exam Stats card — aggregate
        queries only, no per-row Python loop over exams/questions/attempts."""
        return Response(_exam_stats(request.query_params.get('program')))

    @action(detail=False, methods=['get'], permission_classes=[IsAdminUser])
    def stats_by_program(self, request):
        """Same numbers as stats(), grouped by Course.program_group in one
        response — powers the per-program cards without the frontend firing
        N sequential ?program= requests (one per card). Still N aggregate
        queries server-side, but N = number of distinct programs (a
        handful), never N = number of exams/questions."""
        from courses.models import Course

        programs = list(
            Course.objects.exclude(program_group='').values_list('program_group', flat=True).distinct().order_by('program_group')
        )
        return Response([{'program': p, **_exam_stats(p)} for p in programs])

    @action(detail=False, methods=['get'], permission_classes=[IsAdminUser])
    def exam_type_policies(self, request):
        """Phase 5 — the canonical per-exam-category default-config
        template, read-only. Sole intended callers: the Create Exam Wizard
        (Admin/src/app/exam-management/page.js) and the Import & Create Test
        UI (Admin/src/components/import/TestConfigStep.js), both of which
        used to hardcode their own independent, drifted default objects.
        Writing a policy is Django-admin-only (see tests_app/admin.py) —
        no write endpoint exists here, matching the entitlements.
        FreeStarterPolicy precedent from Phase 2."""
        return Response(get_all_exam_type_defaults())

    @action(detail=True, methods=['post'], permission_classes=[HasFeature('exam_release_solutions')])
    def release_solutions(self, request, pk=None):
        """Phase 7 — the manual-release action solutions_visibility='manual'
        requires. Releases this Test's own session-less (anytime) attempts
        only — a scheduled attempt releases per-session instead (see
        ExamSessionViewSet.release_solutions), deliberately not cascaded
        from here, so releasing one occurrence never leaks into another
        still-open one. Idempotent: calling this again after release is a
        no-op (returns the already-released state), never re-stamps
        released_by/released_at."""
        test = self.get_object()
        if test.solutions_visibility != 'manual':
            return Response(
                {'detail': "This exam's solutions visibility is not set to manual release — there is nothing to release."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not test.solutions_released_at:
            from core.edit_audit import record_admin_edit

            test.solutions_released_at = timezone.now()
            test.solutions_released_by = request.user
            test.save(update_fields=['solutions_released_at', 'solutions_released_by'])
            record_admin_edit(
                request, resource_type='Test', resource_id=test.id, resource_label=test.title,
                changed_fields={'solutions_released_at': {'old': None, 'new': test.solutions_released_at.isoformat()}},
            )
        return Response(TestAdminSerializer(test, context={'request': request}).data)

    @action(detail=True, methods=['get'], permission_classes=[IsAdminUser])
    def grand_test_monitor(self, request, pk=None):
        """GT3-7 §9-13 — the Live Grand Test Monitor's aggregate header
        counts (entitled/started/active/submitted/auto-submitted/
        not-started). Extends the existing admin monitoring surface
        (ExamSessionViewSet.attempts already covers per-attempt Participants/
        Results) rather than duplicating it — see tests_app/
        grand_test_admin.py's own module docstring."""
        test = self.get_object()
        if test.exam_type != 'grand':
            return Response({'detail': 'This view is only meaningful for a Grand Test.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_grand_test_monitor_data(test))

    @action(detail=True, methods=['get'], permission_classes=[IsAdminUser])
    def grand_test_participants(self, request, pk=None):
        """GT3-7 §13-14 — student participation table, including entitled
        students with no attempt at all (not_started/missed) — the gap
        ExamSessionViewSet.attempts structurally cannot fill, since it only
        lists rows that already have an attempt."""
        test = self.get_object()
        if test.exam_type != 'grand':
            return Response({'detail': 'This view is only meaningful for a Grand Test.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_grand_test_participants_data(test))

    @action(detail=True, methods=['get'], permission_classes=[IsAdminUser])
    def grand_test_report(self, request, pk=None):
        """GT3-7 §15-18 — post-exam participation/results/question
        analytics, computed only from submitted attempts."""
        test = self.get_object()
        if test.exam_type != 'grand':
            return Response({'detail': 'This view is only meaningful for a Grand Test.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_grand_test_report_data(test))


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
        program = self.request.query_params.get('program')
        search = self.request.query_params.get('search')
        exam_type = self.request.query_params.get('exam_type')
        status_param = self.request.query_params.get('status')
        access = self.request.query_params.get('access')
        if program:
            qs = qs.filter(versions__courses__program_group=program).distinct()
        if search:
            from django.db.models import Q
            qs = qs.filter(Q(title__icontains=search) | Q(exam_code__icontains=search))
        if exam_type:
            qs = qs.filter(exam_type__in=exam_type.split(','))
        if status_param == 'scheduled':
            qs = qs.filter(sessions__status__in=['scheduled', 'registration_open']).distinct()
        elif status_param in ('draft', 'published'):
            from django.db.models import OuterRef, Subquery
            latest_version_draft = Test.objects.filter(
                exam_template=OuterRef('pk')
            ).order_by('-version_number', '-created_at').values('is_draft')[:1]
            qs = qs.annotate(latest_is_draft=Subquery(latest_version_draft))
            qs = qs.filter(latest_is_draft=(status_param == 'draft'))
        if access == 'pro':
            qs = qs.filter(versions__is_pro=True).distinct()
        elif access == 'free':
            qs = qs.filter(versions__is_pro=False).distinct()
        return _teacher_scope(qs, self.request.user)

    @action(detail=True, methods=['get'], permission_classes=[IsAdminUser])
    def sessions(self, request, pk=None):
        template = self.get_object()
        sessions = template.sessions.select_related('exam_version').all()
        return Response(ExamSessionSerializer(sessions, many=True, context={'request': request}).data)

    @action(detail=False, methods=['get'], permission_classes=[IsAdminUser])
    def browse(self, request):
        """Server-paginated, server-filtered variant of GET /exam-templates/
        for the Admin Exam Management dashboard — the primary listing for
        exams that have been scheduled/rescheduled at least once. See
        _ExamBrowsePagination for why list() itself stays unpaginated."""
        qs = self.filter_queryset(self.get_queryset())
        paginator = _ExamBrowsePagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        serializer = ExamTemplateSerializer(page, many=True, context={'request': request})
        return paginator.get_paginated_response(serializer.data)


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

    def perform_create(self, serializer):
        """Phase 2 notification hook: the plain admin 'create a session'
        flow is one of exactly two real code paths that produce an
        ExamSession (the other is exam_versioning.create_reschedule_session,
        hooked separately — see that function). Additive only: scheduling
        notifications never blocks or alters session creation itself, and
        schedule_session_reminders() is itself a no-op for any exam_type
        other than grand/daily."""
        from notifications.exam_integration import schedule_session_reminders

        session = serializer.save()
        # transaction.on_commit: runs immediately here today (this action
        # isn't wrapped in an atomic block), and stays correct unchanged if
        # that ever changes — never notify before the session row is
        # actually durable (architecture prompt's transaction-safety rule).
        transaction.on_commit(lambda: schedule_session_reminders(session))

    def perform_update(self, serializer):
        session = self.get_object()
        if session.status == 'completed' or session.attempts.filter(status='submitted').exists():
            raise ValidationError('This session has already been conducted and can no longer be edited.')
        old_start, old_end = session.start_datetime, session.end_datetime
        updated = serializer.save()
        if updated.start_datetime != old_start or updated.end_datetime != old_end:
            # Real 'Edit Session' time change (Admin's EditSessionModal) —
            # see notifications.services.reschedule_notifications_for_session's
            # own docstring for why this is a distinct case from
            # 'Reschedule / Schedule Again' and needs a hard-delete-then-
            # recreate, not a soft cancel.
            def _resync_reminders():
                from notifications.exam_integration import schedule_session_reminders
                from notifications.services import reschedule_notifications_for_session

                reschedule_notifications_for_session(updated)
                schedule_session_reminders(updated)

            transaction.on_commit(_resync_reminders)

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

    @action(detail=True, methods=['get'], permission_classes=[IsAdminUser])
    def attempts(self, request, pk=None):
        """Participants/Results for this session — admin-only.

        FIX (P0 security audit): the ViewSet's default IsStaffOrReadOnly
        does NOT enforce staff-only here — GET is a SAFE_METHOD, so that
        permission class returns True unconditionally for any caller,
        authenticated or not. This action returns participant names,
        emails, scores, and ranks (see SessionAttemptSerializer), so an
        explicit IsAdminUser override is required, not optional. Do not
        remove this override without replacing it with an equally strict
        one — the previous state was a real unauthenticated PII leak.
        """
        session = self.get_object()
        qs = session.attempts.select_related('user', 'test').order_by('-score')
        return Response(SessionAttemptSerializer(qs, many=True, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        from notifications.services import cancel_notifications_for_session

        session = self.get_object()
        if session.status == 'completed':
            return Response({'detail': 'A completed session cannot be cancelled.'}, status=status.HTTP_400_BAD_REQUEST)
        session.status = 'cancelled'
        session.save(update_fields=['status'])
        # Phase 2: this session's own pending/scheduled reminders (and only
        # this session's — see cancel_notifications_for_session's own
        # docstring on why it scopes by metadata['session_id'], not test_id,
        # given a single Test can have multiple independent sessions).
        transaction.on_commit(lambda: cancel_notifications_for_session(session))
        return Response(ExamSessionSerializer(session, context={'request': request}).data)

    @action(detail=True, methods=['post'], permission_classes=[HasFeature('exam_release_solutions')])
    def release_solutions(self, request, pk=None):
        """Phase 7 — session-scoped counterpart of TestViewSet.
        release_solutions, for solutions_visibility='manual' attempts made
        through this specific session. Deliberately independent of every
        other session under the same exam_template/Test — releasing
        Session #1's solutions must never affect a still-open Session #2
        (see ExamSession.solutions_released_at's own comment). Idempotent."""
        session = self.get_object()
        if session.exam_version.solutions_visibility != 'manual':
            return Response(
                {'detail': "This exam's solutions visibility is not set to manual release — there is nothing to release."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not session.solutions_released_at:
            from core.edit_audit import record_admin_edit

            session.solutions_released_at = timezone.now()
            session.solutions_released_by = request.user
            session.save(update_fields=['solutions_released_at', 'solutions_released_by'])
            record_admin_edit(
                request, resource_type='ExamSession', resource_id=session.id, resource_label=session.session_name,
                changed_fields={'solutions_released_at': {'old': None, 'new': session.solutions_released_at.isoformat()}},
            )
        return Response(ExamSessionSerializer(session, context={'request': request}).data)


class SavedExamViewViewSet(viewsets.ModelViewSet):
    """Per-admin saved Exam Management filter combos. Scoped strictly to the
    requesting user — one admin's saved views are never visible to another."""
    serializer_class = SavedExamViewSerializer
    permission_classes = [IsAdminUser]

    def get_queryset(self):
        return SavedExamView.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class AttemptDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, attempt_id):
        attempt = get_object_or_404(TestAttempt, pk=attempt_id, user=request.user)
        # Phase 6: the resume/continue entry point — lazily finalizes a
        # still-'in_progress' attempt whose effective deadline (session
        # window or personal duration, whichever is sooner — see
        # lifecycle.effective_attempt_end) has already passed. Mirrors the
        # ExamSession.refresh_status() lazy-reactive pattern already
        # established in this codebase: a student (or admin) revisiting an
        # expired attempt is what actually finalizes it if no one has
        # touched it since the deadline passed — see
        # tests_app/lifecycle.py's module docstring for the full picture
        # (the finalize_expired_attempts management command is the
        # complementary backstop for an attempt no one ever revisits).
        attempt = ensure_finalized_if_expired(attempt)
        # Phase 4: routed through the shared can_review_attempt() capability
        # instead of an inline status check duplicated a second, slightly
        # different way in TestResultView (see that view's own comment and
        # docs/ACCESS_DECISION_MATRIX.md discrepancy #1 for the real bug
        # this was found alongside) — same condition as before
        # (attempt.status == 'submitted'), now centralized.
        if can_review_attempt(request.user, attempt).allowed:
            return Response(TestResultSerializer(attempt, context={'request': request}).data)
        return Response(TestAttemptSerializer(attempt, context={'request': request}).data)


class SubmitAnswerView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, attempt_id):
        attempt = get_object_or_404(TestAttempt, pk=attempt_id, user=request.user, status='in_progress')
        # Phase 6: server-authoritative deadline enforcement — never trust
        # that the browser timer stopped the student from sending this
        # request. An attempt whose effective deadline has passed is
        # finalized right here (auto-submitted, using whatever was legally
        # answered before now) and this specific late answer is rejected —
        # "student must no longer be allowed to answer" past the deadline,
        # per the Phase 6 spec.
        if is_attempt_expired(attempt):
            finalize_attempt(attempt, auto_submitted=True)
            return error_response(
                status.HTTP_403_FORBIDDEN, 'exam_closed',
                'This exam has ended and your attempt was automatically submitted.',
            )
        serializer = SubmitAnswerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Phase 9: both the preview verdict and, if preview, the exact set
        # of allowed question IDs now come from this attempt's own frozen
        # AttemptQuestion rows — not a fresh, independently-derived
        # is_preview_only()/questions.all()[:N] computation. Fixes the
        # proven case where a question the student was actually shown as
        # preview (per a differently-shuffled get_questions() response)
        # could be rejected here as "not preview" by a second, disagreeing
        # derivation.
        if attempt_is_preview_only(attempt):
            allowed_ids = attempt_preview_question_ids(attempt)
            if data['question_id'] not in allowed_ids:
                return error_response(
                    status.HTTP_402_PAYMENT_REQUIRED, 'purchase_required', 'Subscribe to unlock this question.',
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
                'time_taken_seconds': data.get('time_taken_seconds'),
            },
        )
        return Response({'saved': True})


class MarkForReviewView(APIView):
    """Toggle 'mark for review' independently of answer() — a dedicated,
    narrow action for the same reason QuestionViewSet.bookmark/confidence
    are: SubmitAnswerView's update_or_create sets selected_option/is_correct
    unconditionally from its payload, so calling it with no option_id just
    to toggle this flag would silently blank an already-selected answer.
    Safe to call before, during, or after answering."""
    permission_classes = [IsAuthenticated]

    def post(self, request, attempt_id):
        attempt = get_object_or_404(TestAttempt, pk=attempt_id, user=request.user, status='in_progress')
        # Phase 6: same server-authoritative deadline enforcement as
        # SubmitAnswerView — marking a question for review is still a form
        # of interacting with an in-progress attempt.
        if is_attempt_expired(attempt):
            finalize_attempt(attempt, auto_submitted=True)
            return error_response(
                status.HTTP_403_FORBIDDEN, 'exam_closed',
                'This exam has ended and your attempt was automatically submitted.',
            )
        question_id = request.data.get('question_id')
        marked = request.data.get('marked') in (True, 'true', 'True', '1', 1)
        if not question_id:
            return error_response(status.HTTP_400_BAD_REQUEST, 'question_id_required', 'question_id is required.')
        get_object_or_404(attempt.test.questions, pk=question_id)
        Answer.objects.update_or_create(
            attempt=attempt, question_id=question_id,
            defaults={'is_marked_for_review': marked},
        )
        return Response({'is_marked_for_review': marked})


class SubmitTestView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, attempt_id):
        # Phase 6: the scoring/ranking body that used to live inline here
        # (record_question_result() per answer, rank/percentile via one
        # aggregate query, deferred stats via transaction.on_commit()) is
        # now tests_app.lifecycle.finalize_attempt() — the exact same
        # logic, unchanged, extracted so a manual Submit and a
        # server-triggered auto-submit (an expired attempt discovered by
        # SubmitAnswerView/AttemptDetailView/TestResultView, or by the
        # finalize_expired_attempts management command) are byte-for-byte
        # the same code path, never two independently-drifting scoring
        # implementations. See lifecycle.py's module docstring.
        #
        # get_object_or_404(..., status='in_progress') still 404s a
        # request for an attempt that's already submitted, exactly as
        # before. finalize_attempt() itself is what's race-safe now (its
        # own select_for_update() + idempotent re-check) — a concurrent
        # double-submit can no longer double-score (still enforced,
        # SubmitTestDoubleSubmissionRaceTests), though the "loser" of a
        # true race now gets a 200 with the same already-final result
        # instead of a 404 (an intentional, tested UX improvement — a
        # double-click no longer surfaces an error to the student).
        attempt = get_object_or_404(TestAttempt, pk=attempt_id, user=request.user, status='in_progress')

        # Phase 9: the frozen, immutable-once-started verdict (see
        # attempt_is_preview_only's own docstring for why this must not
        # re-derive from the student's current entitlement state).
        if attempt_is_preview_only(attempt):
            return error_response(
                status.HTTP_402_PAYMENT_REQUIRED, 'purchase_required', 'Subscribe to submit this test.',
            )

        attempt = finalize_attempt(attempt, auto_submitted=False)
        return Response(TestResultSerializer(attempt, context={'request': request}).data)


class QuestionStatsProcessingHandlerView(APIView):
    """
    POST /api/attempts/stats-process/ — internal endpoint invoked by Cloud
    Tasks (or directly, synchronously, only when STATS_PROCESSING_ASYNC=
    False, local dev only — see tests_app/stats_tasks.py). Auth is a
    shared secret header, matching this project's existing
    X-Media-Processing-Secret / X-Import-Processing-Secret convention.
    Returns a non-2xx status if process_question_stats() raises, so Cloud
    Tasks' built-in retry kicks in — safe because process_question_stats()
    releases its claim on failure (see its docstring), so a retry can
    still apply the same stats without double-counting.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        import json

        from django.conf import settings

        provided = request.headers.get('X-Stats-Processing-Secret')
        if provided != settings.STATS_PROCESSING_SECRET:
            return Response({'detail': 'Invalid secret.'}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            body = json.loads(request.body)
            attempt_id = body['attempt_id']
            deltas = body['deltas']
        except (json.JSONDecodeError, KeyError):
            return Response({'detail': 'attempt_id and deltas required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            process_question_stats(attempt_id, deltas)
        except Exception as exc:  # noqa: BLE001 - deliberately surfaced as a 500 so Cloud Tasks retries
            return Response({'detail': f'Stats processing failed: {exc}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response({'ok': True})


class TestResultView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, attempt_id):
        # FIX (Phase 4 access-decision-matrix audit, discrepancy #1): this
        # endpoint previously had NO status check at all — only the
        # ownership filter below — meaning a student could GET their own
        # still-in_progress attempt's result here and see full solutions/
        # correct-answer content before submitting, even though the
        # sibling AttemptDetailView already correctly gated the identical
        # TestResultSerializer behind attempt.status == 'submitted'. Now
        # routed through the same can_review_attempt() capability that
        # view uses, closing the gap rather than leaving two endpoints
        # disagreeing on the same data.
        attempt = get_object_or_404(TestAttempt, pk=attempt_id, user=request.user)
        # Phase 6: same lazy finalization as AttemptDetailView — a student
        # checking their result right after time ran out (before anything
        # else has touched this attempt) is finalized here rather than
        # finding a confusing "not submitted yet" 403 for an exam that's
        # actually over.
        attempt = ensure_finalized_if_expired(attempt)
        decision = can_review_attempt(request.user, attempt)
        if not decision.allowed:
            return error_response(
                status.HTTP_403_FORBIDDEN, 'attempt_not_submitted', 'This attempt has not been submitted yet.',
                access_denied=decision.as_dict(),
            )
        filter_type = request.query_params.get('filter', 'all')
        return Response(
            TestResultSerializer(attempt, context={'request': request, 'filter': filter_type}).data
        )


class GrandTestMissedReviewView(APIView):
    """Grand Test 3.0 / GT3-4 — GET /tests/{id}/missed-review/.

    The one genuinely new endpoint this phase adds; every other GT3-4
    change extends an existing endpoint/serializer (TestResultView/
    TestResultSerializer, entitlements.services). Deliberately separate
    from TestResultView: that endpoint is keyed by attempt_id and
    requires can_review_attempt (an attempt that exists); a missed
    student has neither an attempt nor an attempt_id BY DESIGN — see
    grand_test_participation_status's own docstring for why MISSED is
    derived rather than ever backed by a fabricated TestAttempt. This
    endpoint is keyed by the Test's own id, and its authorization is a
    genuinely different shape (GT3-4 spec §14): entitlement + schedule-
    ended + no-attempt-ever-existed + review-not-expired — never 'owns
    an attempt', because there is none to own."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        test = get_object_or_404(Test, pk=pk, exam_type='grand')

        access = get_grand_test_access(request.user, test)
        if not access:
            return error_response(
                status.HTTP_402_PAYMENT_REQUIRED, 'purchase_required', 'This Grand Test requires purchase.',
            )

        participation = grand_test_participation_status(test, request.user)
        if participation in ('completed', 'in_progress'):
            return error_response(
                status.HTTP_403_FORBIDDEN, 'not_missed',
                'You appeared for this Grand Test — use the regular result page instead.',
            )
        if participation != 'missed':
            # not_scheduled / upcoming / live — nothing has closed yet, so
            # there is nothing (appeared or missed) to review.
            return error_response(status.HTTP_403_FORBIDDEN, 'review_locked', 'This Grand Test has not ended yet.')

        available_at, expires_at = grand_test_review_window(test)
        now = timezone.now()
        # GT3-6 — general, evidence-safe recommendation only: a missed
        # student has content-review data, never performance data, so
        # this never touches score/rank/accuracy or the current test's
        # weak-area breakdown (see grand_test_analytics.
        # missed_student_recommendation's own docstring). Included
        # regardless of expiry — it is not derived from the per-question
        # content that expires.
        from .grand_test_analytics import missed_student_recommendation

        motivation = missed_student_recommendation(request.user, test)
        base_payload = {
            'review_type': 'missed_review',
            'test': test.id,
            'test_title': test.title,
            'solutions_available_at': available_at,
            'review_expires_at': expires_at,
            'motivation': motivation,
        }
        if expires_at and now >= expires_at:
            # GT3-4 §12: the missed record/status itself never disappears —
            # only the detailed question/solution content does.
            return Response({**base_payload, 'review_status': 'expired', 'questions': []})

        questions = [
            tq.question for tq in
            TestQuestion.objects.filter(test=test).select_related(
                'question__subject', 'question__chapter', 'question__topic',
                'question__image_asset', 'question__explanation_image_asset',
            ).prefetch_related('question__options').order_by('order')
            if tq.question_id
        ]
        return Response({
            **base_payload,
            'review_status': 'available',
            'questions': MissedReviewQuestionSerializer(questions, many=True, context={'request': request}).data,
        })


class GrandTestSeriesView(APIView):
    """Grand Test 3.0 / GT3-6 — GET /tests/grand-series/.

    The series-level dashboard: score trend, attendance/consistency, and
    personal best across every Grand Test this student holds a live
    entitlement for. See tests_app.grand_test_analytics.
    grand_test_series_summary's own docstring for the exact rules (a
    missed test is never scored as zero; trend/average are computed over
    appeared tests only)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .grand_test_analytics import grand_test_series_summary

        return Response(grand_test_series_summary(request.user))


class MyAttemptsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        attempts = TestAttempt.objects.filter(user=request.user).select_related('test').order_by('-start_time')
        return Response(TestAttemptSummarySerializer(attempts, many=True, context={'request': request}).data)


class FinalizeExpiredAttemptsView(APIView):
    """POST /api/cron/finalize-expired-attempts/ — the Release Candidate
    scheduler hook for the best-effort expired-attempt sweep.

    Runs the SAME body as the `finalize_expired_attempts` management
    command (tests_app.lifecycle.sweep_expired_attempts — never a second
    finalization algorithm). Idempotent and row-lock-safe, so Cloud
    Scheduler firing it every few minutes, overlapping with a slow run,
    or racing a student's own submit can never double-score.

    Same shared-secret pattern as billing.views.ExpireStalePaymentsView /
    courses.views.PruneExpiredPackagesView: `X-Cron-Secret` header (or
    `?secret=`), checked against settings.CRON_SECRET, which fails closed
    when unconfigured. `?dry_run=1` reports without writing."""
    permission_classes = [AllowAny]

    def post(self, request):
        from django.conf import settings

        from .lifecycle import sweep_expired_attempts

        if not settings.CRON_SECRET:
            return Response({'detail': 'Cron secret not configured.'}, status=status.HTTP_401_UNAUTHORIZED)
        provided = request.headers.get('X-Cron-Secret') or request.query_params.get('secret')
        if provided != settings.CRON_SECRET:
            return Response({'detail': 'Invalid or missing cron secret.'}, status=status.HTTP_401_UNAUTHORIZED)

        dry_run = request.query_params.get('dry_run') in ('1', 'true', 'True')
        try:
            result = sweep_expired_attempts(dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - surfaced as 500 so the scheduler retries
            logger.exception('finalize-expired-attempts sweep failed')
            return Response({'detail': f'Sweep failed: {exc}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        # Don't echo the full id list in the HTTP response (could be large);
        # the command path still logs each one.
        return Response({'checked': result['checked'], 'finalized': result['finalized'], 'dry_run': result['dry_run']})


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


def _deny_if_cannot_view_own_analytics(request):
    """Phase 7: CanViewAnalytics, wired in explicitly rather than left as
    only an implicit property of "this view never accepts a target user
    parameter" (Phase 4 built the capability specifically to make that
    invariant real and testable — see its own docstring). Every analytics
    view below is self-scoped by construction (never takes a target user
    id), so this always allows — but it's now a real, tested enforcement
    point rather than a claim about query shape alone. Returns a 403
    Response if denied, None if allowed."""
    from entitlements.services import can_view_analytics
    decision = can_view_analytics(request.user, request.user)
    if not decision.allowed:
        return Response({'detail': decision.reason, 'access_denied': decision.as_dict()}, status=status.HTTP_403_FORBIDDEN)
    return None


class StudentPerformanceOverviewView(APIView):
    permission_classes = [IsAuthenticated]

    # Scalability audit: this endpoint used to issue ~60-90 separate
    # queries per request (7 independent aggregation functions, several
    # re-fetching the exact same TestAttempt/Answer/QuestionAttempt rows —
    # see tests_app/performance.py's memoization helpers). Response cached
    # per-user for a short window on top of that fix. The cache key always
    # includes request.user.id, so one user can never be served another
    # user's cached response — this is the only variable that determines
    # which cache entry is read/written, making cross-user leakage
    # impossible by construction, not just by convention. Course/date-
    # range/granularity are part of the key too, so different filtered
    # views for the same user never collide. TTL is short (not the
    # 900s used for the platform-wide subject-rank table) because this
    # response is the student's OWN live activity — a longer TTL would
    # mean "I just submitted a test and my dashboard doesn't show it yet",
    # a real, noticeable staleness bug, not just a performance trade-off.
    OVERVIEW_CACHE_SECONDS = 30

    def get(self, request):
        from django.core.cache import cache

        denied = _deny_if_cannot_view_own_analytics(request)
        if denied:
            return denied

        course = _parse_course(request)
        date_from, date_to = _parse_date_range(request)
        granularity = request.query_params.get('granularity', 'day')

        # Keyed on the RAW request params (days=/from=/to=), not the
        # resolved date_from/date_to above — _parse_date_range's default
        # path computes date_from as timezone.now() - N days, which is a
        # different microsecond-precision value on every single call, so
        # keying on it would make every request a cache miss and silently
        # defeat the cache entirely (caught during Phase A validation: repeat
        # requests weren't actually getting faster). The raw params are
        # stable across calls within the same window, which is what "the
        # same logical request" actually means here — a few seconds'
        # difference in exactly where a 30-day window starts doesn't change
        # which TestAttempts fall in range in any way that matters for a
        # 30-second cache.
        cache_key = (
            f'perf:overview:{request.user.id}:{course}:'
            f"{request.query_params.get('days', '')}:{request.query_params.get('from', '')}:"
            f"{request.query_params.get('to', '')}:{granularity}"
        )
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        # Request-scoped only (a fresh dict every call, never persisted or
        # shared across requests/users) — collapses exact-duplicate
        # fetches across the 7 functions below into one query each. See
        # tests_app/performance.py's _memo_get_attempts/_memo_answer_stats/
        # _memo_combined_state docstrings for exactly what this does and
        # does not change.
        memo = {}

        # Scalability audit Phase 3: subject_breakdown() (its own
        # _combined_question_state scan + a per-subject aggregation loop)
        # used to run three separate times in this one request —
        # strengths_and_weaknesses() and recommendations() each called it
        # again internally. Computed once here and shared via the optional
        # `subjects` param; both functions still self-compute it when
        # called standalone elsewhere.
        subjects = performance.subject_breakdown(request.user, course, memo=memo)

        data = {
            'kpis': performance.kpi_overview(request.user, course, date_from, date_to, memo=memo),
            'trend': performance.trend_series(request.user, course, date_from, date_to, granularity, memo=memo),
            'subjects': subjects,
            'mock_tests': performance.mock_test_analytics(request.user, course, memo=memo),
            'questions': performance.question_analytics(request.user, course, memo=memo),
            'strengths_weaknesses': performance.strengths_and_weaknesses(request.user, course, subjects=subjects, memo=memo),
            'recommendations': performance.recommendations(request.user, course, subjects=subjects, memo=memo),
        }
        cache.set(cache_key, data, self.OVERVIEW_CACHE_SECONDS)
        return Response(data)


class SubjectPerformanceDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):
        from django.db.models import Q

        from academics.models import Subject
        from courses.access import eligible_course_ids

        denied = _deny_if_cannot_view_own_analytics(request)
        if denied:
            return denied

        user = request.user
        accessible = Subject.objects.filter(pk=subject_id)
        if not (user.is_authenticated and user.is_staff):
            # chapter_breakdown()/topic_mastery() take a bare subject_id with
            # no eligibility check of their own — any authenticated student
            # could otherwise request an unrelated program's subject_id
            # directly and receive its chapter/topic names and counts.
            accessible = accessible.filter(Q(courses__isnull=True) | Q(courses__id__in=eligible_course_ids(user)))
        if not accessible.exists():
            raise NotFound('Subject not found.')
        return Response(performance.chapter_breakdown(request.user, subject_id))


class ExamTypeStatsView(APIView):
    """Powers the 'Your <Exam Type> Stats' sidebar panel on each test-listing
    page (Mock/Daily/Grand/PYQ/QBank)."""
    permission_classes = [IsAuthenticated]

    def get(self, request, exam_type):
        denied = _deny_if_cannot_view_own_analytics(request)
        if denied:
            return denied
        if exam_type not in dict(Test.EXAM_TYPE_CHOICES):
            return Response({'detail': 'Unknown exam_type.'}, status=400)
        course = _parse_course(request)
        return Response(performance.exam_type_stats(request.user, exam_type, course))


class PerformanceCalendarView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        denied = _deny_if_cannot_view_own_analytics(request)
        if denied:
            return denied
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
        # Phase 11 audit: this was the one analytics endpoint of the five
        # that never ran the CanViewAnalytics check. It was not an IDOR —
        # the ownership filter below has always been present — but the
        # capability was unenforced here, so an analytics-visibility policy
        # change would have silently skipped this endpoint. Checked before
        # the ownership lookup: capability first, existence second.
        denied = _deny_if_cannot_view_own_analytics(request)
        if denied:
            return denied
        # Deliberately NotFound, not PermissionDenied: another student's
        # attempt id must not be distinguishable from a nonexistent one.
        if not TestAttempt.objects.filter(pk=attempt_id, user=request.user).exists():
            raise NotFound('Attempt not found.')
        return Response(performance.comparative(request.user, attempt_id))
