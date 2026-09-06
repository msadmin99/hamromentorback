"""Phase 6 — Exam Session, Scheduling & Attempt Lifecycle: regression suite.

Covers the mandatory timing matrix (Cases A-G), Daily/Grand Test delivery
semantics, attempt-lifecycle transitions, auto-submit/finalization
(request-time and the management-command sweep), idempotency/concurrency,
re-release/historical-integrity, and the capability-layer/API-surface
changes. See docs/PHASE_6_ARCHITECTURE.md for the design these tests hold
to account.
"""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase, APITransactionTestCase

from academics.models import Option, Question, Subject
from entitlements.services import can_continue_attempt, can_submit_attempt
from tests_app.lifecycle import (
    compute_effective_session_status, effective_attempt_end, ensure_finalized_if_expired, finalize_attempt,
    is_attempt_expired,
)
from tests_app.models import Answer, ExamSession, ExamTemplate, Test, TestAttempt, TestQuestion

User = get_user_model()


def _mkexam(exam_type='mock', allow=None, **overrides):
    """`allow`: a user (or list of users) individually assigned so
    can_access_test() grants academic eligibility — Test.courses is
    blank-by-default = visible to no one (a deliberate, unrelated,
    already-validated fail-closed default from an earlier phase's
    security fix), so every attempt-lifecycle test needs an explicit
    grant unrelated to what it's actually testing."""
    fields = {'title': f'{exam_type} exam', 'exam_type': exam_type, 'duration_minutes': 60, 'is_draft': False}
    fields.update(overrides)
    test = Test.objects.create(**fields)
    if allow:
        users = allow if isinstance(allow, (list, tuple)) else [allow]
        test.assigned_students.set(users)
    return test


def _mksession(test, start_datetime, end_datetime, **overrides):
    template = ExamTemplate.objects.create(title=test.title, exam_type=test.exam_type)
    test.exam_template = template
    test.save(update_fields=['exam_template'])
    fields = {
        'exam_template': template, 'exam_version': test, 'session_name': 'Session',
        'start_datetime': start_datetime, 'end_datetime': end_datetime, 'status': 'scheduled',
    }
    fields.update(overrides)
    return ExamSession.objects.create(**fields)


def _backdate_start(attempt, when):
    """auto_now_add only fires on INSERT — a queryset .update() bypasses
    Model.save()'s pre_save handling entirely, so this reliably backdates
    start_time for tests without fighting the field's own auto-set."""
    TestAttempt.objects.filter(pk=attempt.pk).update(start_time=when)
    attempt.refresh_from_db()
    return attempt


class EffectiveAttemptEndTests(TestCase):
    """The MIN(attempt_start + duration, session_end) rule itself, and the
    exact mandatory timing matrix cases A/B/C computed directly against the
    service function (the HTTP-level equivalents are in
    TimingMatrixEndpointTests below)."""

    def setUp(self):
        self.student = User.objects.create_user(username='ea_student', email='ea_student@example.com', password='pw')
        self.test = _mkexam(duration_minutes=180)

    def test_no_session_uses_personal_duration_only(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test)
        expected = attempt.start_time + timezone.timedelta(minutes=180)
        self.assertEqual(effective_attempt_end(attempt), expected)

    def test_case_a_start_at_session_open_full_duration_fits(self):
        """session 08:00-11:00, duration 180, start 08:00 -> effective end 11:00."""
        open_at = timezone.now()
        session = _mksession(self.test, open_at, open_at + timezone.timedelta(hours=3))
        attempt = TestAttempt.objects.create(user=self.student, test=self.test, session=session)
        attempt = _backdate_start(attempt, open_at)
        self.assertEqual(effective_attempt_end(attempt), session.end_datetime)

    def test_case_b_late_start_session_end_still_the_ceiling(self):
        """session 08:00-11:00, duration 180, start 09:30 -> effective end 11:00 (personal end 12:30 > session end)."""
        open_at = timezone.now() - timezone.timedelta(hours=1, minutes=30)
        session = _mksession(self.test, open_at, open_at + timezone.timedelta(hours=3))
        attempt = TestAttempt.objects.create(user=self.student, test=self.test, session=session)
        attempt = _backdate_start(attempt, open_at + timezone.timedelta(hours=1, minutes=30))
        self.assertEqual(effective_attempt_end(attempt), session.end_datetime)

    def test_case_c_short_duration_personal_end_is_the_ceiling(self):
        """session 08:00-11:00, duration 60, start 09:30 -> effective end 10:30 (personal end < session end)."""
        open_at = timezone.now() - timezone.timedelta(hours=1, minutes=30)
        session = _mksession(self.test, open_at, open_at + timezone.timedelta(hours=3))
        short_test = _mkexam(duration_minutes=60)
        short_test.exam_template = session.exam_template
        short_test.save(update_fields=['exam_template'])
        attempt = TestAttempt.objects.create(user=self.student, test=short_test, session=session)
        start = open_at + timezone.timedelta(hours=1, minutes=30)
        attempt = _backdate_start(attempt, start)
        self.assertEqual(effective_attempt_end(attempt), start + timezone.timedelta(minutes=60))

    def test_is_attempt_expired_false_before_deadline(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test)
        self.assertFalse(is_attempt_expired(attempt))

    def test_is_attempt_expired_true_after_deadline(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test)
        attempt = _backdate_start(attempt, timezone.now() - timezone.timedelta(hours=4))  # duration=180
        self.assertTrue(is_attempt_expired(attempt))

    def test_already_submitted_attempt_is_never_expired(self):
        """is_attempt_expired is about the in_progress state, not raw time
        — a submitted attempt (however old) is just submitted, not
        'expired'."""
        attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='submitted')
        attempt = _backdate_start(attempt, timezone.now() - timezone.timedelta(days=30))
        self.assertFalse(is_attempt_expired(attempt))


class TimingMatrixEndpointTests(APITestCase):
    """Cases D/E/F/G from the mandatory matrix, exercised through the real
    start/answer/submit endpoints — not just the service function."""

    def setUp(self):
        self.student = User.objects.create_user(username='tm_student', email='tm_student@example.com', password='pw')
        self.client.force_authenticate(user=self.student)
        self.subject = Subject.objects.create(name='Timing Subject')
        self.question = Question.objects.create(subject=self.subject, text='Q?', marks=1, negative_marks=0)
        self.correct = Option.objects.create(question=self.question, text='A', order=0, is_correct=True)

    def _exam_with_session(self, start, end, duration=180):
        test = _mkexam(duration_minutes=duration, allow=self.student)
        TestQuestion.objects.create(test=test, question=self.question)
        session = _mksession(test, start, end)
        return test, session

    def test_case_d_start_before_session_opens_fails(self):
        now = timezone.now()
        test, session = self._exam_with_session(now + timezone.timedelta(hours=1), now + timezone.timedelta(hours=4))
        resp = self.client.post(f'/api/exam-sessions/{session.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(TestAttempt.objects.filter(user=self.student, test=test).exists())

    def test_case_e_start_after_session_closes_fails(self):
        now = timezone.now()
        test, session = self._exam_with_session(now - timezone.timedelta(hours=4), now - timezone.timedelta(hours=1))
        resp = self.client.post(f'/api/exam-sessions/{session.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(TestAttempt.objects.filter(user=self.student, test=test).exists())

    def test_case_f_session_closes_while_attempt_in_progress_gets_finalized_on_next_touch(self):
        now = timezone.now()
        test, session = self._exam_with_session(now - timezone.timedelta(hours=2), now - timezone.timedelta(minutes=1))
        attempt = TestAttempt.objects.create(user=self.student, test=test, session=session)
        attempt = _backdate_start(attempt, now - timezone.timedelta(hours=2))

        resp = self.client.get(f'/api/attempts/{attempt.id}/')
        self.assertEqual(resp.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'submitted')
        self.assertTrue(attempt.auto_submitted)

    def test_case_g_request_recognizes_expiration_even_though_no_scheduler_ran(self):
        """The exact mandatory scenario: 'scheduler delayed' + 'student
        calls submit' must still result in correct rejection/finalization
        — proven here by never invoking finalize_expired_attempts at all,
        only the live submit endpoint."""
        now = timezone.now()
        test, session = self._exam_with_session(now - timezone.timedelta(hours=2), now - timezone.timedelta(minutes=1))
        attempt = TestAttempt.objects.create(user=self.student, test=test, session=session)
        attempt = _backdate_start(attempt, now - timezone.timedelta(hours=2))

        resp = self.client.post(f'/api/attempts/{attempt.id}/answer/', {'question_id': self.question.id, 'option_id': self.correct.id})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data.get('code'), 'exam_closed')

        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'submitted')
        self.assertTrue(attempt.auto_submitted)
        # No answer was recorded — the late answer was correctly rejected, not silently accepted.
        self.assertEqual(attempt.answers.count(), 0)
        self.assertEqual(float(attempt.score), 0)


class AutoSubmitFinalizationTests(APITestCase):
    """SubmitAnswerView / MarkForReviewView reject a late action and
    finalize; finalize_attempt() is idempotent; TestResultView lazily
    finalizes; a resumed-but-expired attempt falls through to a fresh
    start instead of being handed back as if still answerable."""

    def setUp(self):
        self.student = User.objects.create_user(username='fin_student', email='fin_student@example.com', password='pw')
        self.client.force_authenticate(user=self.student)
        self.subject = Subject.objects.create(name='Finalize Subject')
        self.question = Question.objects.create(subject=self.subject, text='Q?', marks=5, negative_marks=1)
        self.correct = Option.objects.create(question=self.question, text='A', order=0, is_correct=True)
        self.test = _mkexam(duration_minutes=30)
        TestQuestion.objects.create(test=self.test, question=self.question)

    def _expired_attempt(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test)
        return _backdate_start(attempt, timezone.now() - timezone.timedelta(hours=1))

    def test_mark_for_review_rejects_and_finalizes_expired_attempt(self):
        attempt = self._expired_attempt()
        resp = self.client.post(f'/api/attempts/{attempt.id}/mark-review/', {'question_id': self.question.id, 'marked': True})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'submitted')
        self.assertTrue(attempt.auto_submitted)

    def test_result_view_lazily_finalizes_and_then_allows_review(self):
        attempt = self._expired_attempt()
        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')
        self.assertEqual(resp.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'submitted')
        self.assertTrue(attempt.auto_submitted)

    def test_finalize_attempt_is_idempotent(self):
        attempt = self._expired_attempt()
        first = finalize_attempt(attempt, auto_submitted=True)
        first_end_time = first.end_time
        first_score = first.score
        second = finalize_attempt(first, auto_submitted=True)
        self.assertEqual(second.end_time, first_end_time)
        self.assertEqual(second.score, first_score)
        self.assertEqual(TestAttempt.objects.get(pk=attempt.pk).status, 'submitted')

    def test_manual_submit_after_expiry_still_finalizes_with_answers_made_before_deadline(self):
        """Late manual Submit is accepted (idempotent finalize of whatever
        was legitimately answered before the deadline started rejecting
        new answers) — a deliberate, documented choice, distinct from
        CanContinue's stricter denial. See can_submit_attempt's docstring."""
        attempt = TestAttempt.objects.create(user=self.student, test=self.test)
        Answer.objects.create(attempt=attempt, question=self.question, selected_option=self.correct, is_correct=True)
        attempt = _backdate_start(attempt, timezone.now() - timezone.timedelta(hours=1))

        resp = self.client.post(f'/api/attempts/{attempt.id}/submit/')
        self.assertEqual(resp.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'submitted')
        self.assertEqual(float(attempt.score), 5)

    def test_resume_of_expired_attempt_falls_through_to_a_new_attempt(self):
        test = _mkexam(duration_minutes=30, max_attempts=2, allow=self.student)
        TestQuestion.objects.create(test=test, question=self.question)
        attempt = TestAttempt.objects.create(user=self.student, test=test)
        attempt = _backdate_start(attempt, timezone.now() - timezone.timedelta(hours=1))

        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        new_id = resp.data['id']
        self.assertNotEqual(new_id, attempt.id)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'submitted')
        self.assertTrue(attempt.auto_submitted)


class CanContinueCanSubmitExpiryTests(TestCase):
    """Capability-layer expiry awareness — read-only, never writes."""

    def setUp(self):
        self.student = User.objects.create_user(username='cap_student', email='cap_student@example.com', password='pw')
        self.test = _mkexam(duration_minutes=30)

    def test_can_continue_denies_once_expired_even_before_any_write(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test)
        attempt = _backdate_start(attempt, timezone.now() - timezone.timedelta(hours=1))
        decision = can_continue_attempt(self.student, attempt)
        self.assertFalse(decision.allowed)
        # Purely read-only — the DB row itself is untouched by the check.
        self.assertEqual(TestAttempt.objects.get(pk=attempt.pk).status, 'in_progress')

    def test_can_submit_remains_allowed_even_when_expired(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test)
        attempt = _backdate_start(attempt, timezone.now() - timezone.timedelta(hours=1))
        self.assertTrue(can_submit_attempt(self.student, attempt).allowed)

    def test_can_continue_allowed_before_deadline(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test)
        self.assertTrue(can_continue_attempt(self.student, attempt).allowed)


class SessionEffectiveStatusTests(APITestCase):
    """ExamSessionSerializer.effective_status/my_status — always correct
    even when the raw `status` column is stale (a list/retrieve GET never
    writes)."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='sess_staff', email='sess_staff@example.com', password='pw', is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(username='sess_student', email='sess_student@example.com', password='pw')
        self.subject = Subject.objects.create(name='Session Status Subject')
        self.question = Question.objects.create(subject=self.subject, text='Q?', marks=1, negative_marks=0)

    def test_effective_status_is_completed_even_though_raw_status_column_still_says_scheduled(self):
        test = _mkexam()
        TestQuestion.objects.create(test=test, question=self.question)
        now = timezone.now()
        session = _mksession(test, now - timezone.timedelta(hours=3), now - timezone.timedelta(hours=1), status='scheduled')

        self.client.force_authenticate(user=self.staff)
        resp = self.client.get(f'/api/exam-sessions/{session.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'scheduled')  # raw column, never written by this GET
        self.assertEqual(resp.data['effective_status'], 'completed')  # live-computed, correct regardless

    def test_my_status_missed_for_a_student_with_no_attempt_after_close(self):
        test = _mkexam()
        TestQuestion.objects.create(test=test, question=self.question)
        now = timezone.now()
        session = _mksession(test, now - timezone.timedelta(hours=3), now - timezone.timedelta(hours=1))

        self.client.force_authenticate(user=self.student)
        resp = self.client.get(f'/api/exam-sessions/{session.id}/')
        self.assertEqual(resp.data['my_status'], 'missed')

    def test_my_status_submitted_for_a_student_who_did_attempt(self):
        test = _mkexam()
        TestQuestion.objects.create(test=test, question=self.question)
        now = timezone.now()
        session = _mksession(test, now - timezone.timedelta(hours=3), now - timezone.timedelta(hours=1))
        TestAttempt.objects.create(user=self.student, test=test, session=session, status='submitted')

        self.client.force_authenticate(user=self.student)
        resp = self.client.get(f'/api/exam-sessions/{session.id}/')
        self.assertEqual(resp.data['my_status'], 'submitted')

    def test_my_status_not_started_for_an_open_session(self):
        test = _mkexam()
        TestQuestion.objects.create(test=test, question=self.question)
        now = timezone.now()
        session = _mksession(test, now - timezone.timedelta(hours=1), now + timezone.timedelta(hours=1))

        self.client.force_authenticate(user=self.student)
        resp = self.client.get(f'/api/exam-sessions/{session.id}/')
        self.assertEqual(resp.data['my_status'], 'not_started')

    def test_my_status_none_for_anonymous(self):
        test = _mkexam()
        TestQuestion.objects.create(test=test, question=self.question)
        now = timezone.now()
        session = _mksession(test, now - timezone.timedelta(hours=1), now + timezone.timedelta(hours=1))
        session.status = 'live'
        session.save(update_fields=['status'])
        # Anonymous can list a non-draft session (IsStaffOrReadOnly allows read).
        resp = self.client.get(f'/api/exam-sessions/{session.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data['my_status'])

    def test_refresh_status_and_effective_status_agree(self):
        """The write path (refresh_status) and the read path
        (compute_effective_session_status) must never disagree — both
        delegate to the same function now."""
        test = _mkexam()
        now = timezone.now()
        session = _mksession(test, now - timezone.timedelta(hours=3), now - timezone.timedelta(hours=1), status='scheduled')
        session.refresh_status()
        self.assertEqual(session.status, 'completed')
        self.assertEqual(compute_effective_session_status(session), 'completed')

    def test_draft_and_cancelled_never_auto_transition(self):
        test = _mkexam()
        now = timezone.now()
        draft = _mksession(test, now - timezone.timedelta(hours=3), now - timezone.timedelta(hours=1), status='draft')
        self.assertEqual(compute_effective_session_status(draft), 'draft')
        cancelled = _mksession(test, now - timezone.timedelta(hours=3), now - timezone.timedelta(hours=1), status='cancelled')
        self.assertEqual(compute_effective_session_status(cancelled), 'cancelled')


class DailyTestDeliveryTests(APITestCase):
    """Window open/upcoming/closed; missed handling; late start; remaining
    time; re-release creates a NEW session and never mutates the old one."""

    def setUp(self):
        self.student = User.objects.create_user(username='daily_student', email='daily_student@example.com', password='pw')
        self.staff = User.objects.create_user(
            username='daily_staff', email='daily_staff@example.com', password='pw', is_staff=True, admin_role='admin',
        )
        self.subject = Subject.objects.create(name='Daily Subject')
        self.question = Question.objects.create(subject=self.subject, text='Q?', marks=1, negative_marks=0)

    def test_upcoming_window_cannot_start(self):
        test = _mkexam(exam_type='daily', allow=self.student)
        TestQuestion.objects.create(test=test, question=self.question)
        now = timezone.now()
        session = _mksession(test, now + timezone.timedelta(hours=1), now + timezone.timedelta(hours=25))
        self.client.force_authenticate(user=self.student)
        resp = self.client.post(f'/api/exam-sessions/{session.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_open_window_can_start_and_gets_personal_duration_from_start_time(self):
        test = _mkexam(exam_type='daily', duration_minutes=90, allow=self.student)
        TestQuestion.objects.create(test=test, question=self.question)
        now = timezone.now()
        session = _mksession(test, now - timezone.timedelta(hours=1), now + timezone.timedelta(hours=23))
        self.client.force_authenticate(user=self.student)
        resp = self.client.post(f'/api/exam-sessions/{session.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        attempt = TestAttempt.objects.get(pk=resp.data['id'])
        # Personal end (start + 90m) is well before session end (23h away) — MIN picks personal.
        self.assertEqual(effective_attempt_end(attempt), attempt.start_time + timezone.timedelta(minutes=90))

    def test_closed_window_never_attempted_shows_missed_cannot_start(self):
        test = _mkexam(exam_type='daily', allow=self.student)
        TestQuestion.objects.create(test=test, question=self.question)
        now = timezone.now()
        session = _mksession(test, now - timezone.timedelta(hours=25), now - timezone.timedelta(hours=1))
        self.client.force_authenticate(user=self.student)

        resp = self.client.post(f'/api/exam-sessions/{session.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        detail = self.client.get(f'/api/exam-sessions/{session.id}/')
        self.assertEqual(detail.data['my_status'], 'missed')

    def test_closed_window_attempted_student_can_still_review(self):
        test = _mkexam(exam_type='daily')
        TestQuestion.objects.create(test=test, question=self.question)
        now = timezone.now()
        session = _mksession(test, now - timezone.timedelta(hours=25), now - timezone.timedelta(hours=1))
        attempt = TestAttempt.objects.create(user=self.student, test=test, session=session, status='submitted', score=1)

        self.client.force_authenticate(user=self.student)
        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')
        self.assertEqual(resp.status_code, 200)

    def test_re_release_creates_a_new_session_old_session_and_attempt_untouched(self):
        """The Reschedule action (create_reschedule_session) already
        creates a brand new ExamSession rather than mutating the old one —
        this proves it end-to-end for a Daily Test and confirms the old
        session's window/attempt survive byte-for-byte."""
        from tests_app.exam_versioning import create_reschedule_session

        test = _mkexam(exam_type='daily')
        TestQuestion.objects.create(test=test, question=self.question)
        now = timezone.now()
        old_start, old_end = now - timezone.timedelta(hours=25), now - timezone.timedelta(hours=1)
        old_session = _mksession(test, old_start, old_end)
        old_attempt = TestAttempt.objects.create(
            user=self.student, test=test, session=old_session, status='submitted', score=7, rank=1, percentile=100,
        )

        new_session = create_reschedule_session(
            test.id, self.staff, start_datetime=now + timezone.timedelta(hours=1),
            end_datetime=now + timezone.timedelta(hours=25),
        )

        self.assertNotEqual(new_session.id, old_session.id)
        old_session.refresh_from_db()
        self.assertEqual(old_session.start_datetime, old_start)
        self.assertEqual(old_session.end_datetime, old_end)
        old_attempt.refresh_from_db()
        self.assertEqual(old_attempt.score, 7)
        self.assertEqual(old_attempt.status, 'submitted')
        self.assertEqual(old_attempt.session_id, old_session.id)
        # The new opportunity is genuinely open and independent.
        self.assertEqual(TestAttempt.objects.filter(session=new_session).count(), 0)


class GrandTestDeliveryTests(APITestCase):
    """Fixed live window, late entry gets only remaining global time,
    password is additional not a substitute for entitlement, unattempted
    students don't get post-event access."""

    def setUp(self):
        self.student = User.objects.create_user(username='grand_student', email='grand_student@example.com', password='pw')
        self.subject = Subject.objects.create(name='Grand Subject')
        self.question = Question.objects.create(subject=self.subject, text='Q?', marks=1, negative_marks=0)

    def test_before_start_denied(self):
        test = _mkexam(exam_type='grand', allow=self.student)
        TestQuestion.objects.create(test=test, question=self.question)
        now = timezone.now()
        session = _mksession(test, now + timezone.timedelta(minutes=30), now + timezone.timedelta(hours=3))
        self.client.force_authenticate(user=self.student)
        resp = self.client.post(f'/api/exam-sessions/{session.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_late_entry_receives_only_remaining_global_time_not_full_personal_duration(self):
        """08:00-11:00 window, 180-minute personal duration, joins at
        09:30 (90 minutes into the window) -> effective end is 11:00, not
        09:30+180=12:30."""
        test = _mkexam(exam_type='grand', duration_minutes=180, allow=self.student)
        TestQuestion.objects.create(test=test, question=self.question)
        window_start = timezone.now() - timezone.timedelta(hours=1, minutes=30)
        window_end = window_start + timezone.timedelta(hours=3)
        session = _mksession(test, window_start, window_end)

        self.client.force_authenticate(user=self.student)
        resp = self.client.post(f'/api/exam-sessions/{session.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        attempt = TestAttempt.objects.get(pk=resp.data['id'])
        self.assertEqual(effective_attempt_end(attempt), window_end)
        # Additive API surface: the client can read the real deadline directly.
        self.assertEqual(resp.data['effective_end_at'], window_end)

    def test_free_grand_test_password_required_even_with_valid_window(self):
        test = _mkexam(exam_type='grand', access_password='secret123', allow=self.student)
        TestQuestion.objects.create(test=test, question=self.question)
        now = timezone.now()
        session = _mksession(test, now - timezone.timedelta(minutes=10), now + timezone.timedelta(hours=2))
        self.client.force_authenticate(user=self.student)

        wrong = self.client.post(f'/api/exam-sessions/{session.id}/start/', {'access_password': 'nope'})
        self.assertEqual(wrong.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(TestAttempt.objects.filter(user=self.student, test=test).exists())

        right = self.client.post(f'/api/exam-sessions/{session.id}/start/', {'access_password': 'secret123'})
        self.assertEqual(right.status_code, status.HTTP_201_CREATED)

    def test_never_attempted_student_gets_no_post_event_review_access(self):
        test = _mkexam(exam_type='grand', allow=self.student)
        TestQuestion.objects.create(test=test, question=self.question)
        now = timezone.now()
        session = _mksession(test, now - timezone.timedelta(hours=3), now - timezone.timedelta(hours=1))
        self.client.force_authenticate(user=self.student)
        resp = self.client.post(f'/api/exam-sessions/{session.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(TestAttempt.objects.filter(user=self.student, test=test).exists())


class AttemptLifecycleSecurityTests(APITestCase):
    """Students cannot manipulate lifecycle fields, access another
    student's attempt, or replay an old attempt via a guessed id."""

    def setUp(self):
        self.student = User.objects.create_user(username='sec_student', email='sec_student@example.com', password='pw')
        self.other = User.objects.create_user(username='sec_other', email='sec_other@example.com', password='pw')
        self.subject = Subject.objects.create(name='Security Subject')
        self.question = Question.objects.create(subject=self.subject, text='Q?', marks=1, negative_marks=0)
        self.test = _mkexam(duration_minutes=30, allow=[self.student, self.other])
        TestQuestion.objects.create(test=self.test, question=self.question)

    def test_client_supplied_start_time_and_status_are_ignored_on_start(self):
        self.client.force_authenticate(user=self.student)
        spoofed_start = (timezone.now() - timezone.timedelta(days=10)).isoformat()
        resp = self.client.post(f'/api/tests/{self.test.id}/start/', {
            'start_time': spoofed_start, 'status': 'submitted', 'score': 999, 'rank': 1,
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        attempt = TestAttempt.objects.get(pk=resp.data['id'])
        self.assertEqual(attempt.status, 'in_progress')
        self.assertEqual(float(attempt.score), 0)
        self.assertGreater(attempt.start_time, timezone.now() - timezone.timedelta(minutes=1))

    def test_cannot_answer_another_students_attempt(self):
        attempt = TestAttempt.objects.create(user=self.other, test=self.test)
        self.client.force_authenticate(user=self.student)
        resp = self.client.post(f'/api/attempts/{attempt.id}/answer/', {'question_id': self.question.id})
        self.assertEqual(resp.status_code, 404)

    def test_cannot_view_another_students_attempt_detail(self):
        attempt = TestAttempt.objects.create(user=self.other, test=self.test)
        self.client.force_authenticate(user=self.student)
        resp = self.client.get(f'/api/attempts/{attempt.id}/')
        self.assertEqual(resp.status_code, 404)

    def test_cannot_submit_another_students_attempt(self):
        attempt = TestAttempt.objects.create(user=self.other, test=self.test)
        self.client.force_authenticate(user=self.student)
        resp = self.client.post(f'/api/attempts/{attempt.id}/submit/')
        self.assertEqual(resp.status_code, 404)


class FinalizeExpiredAttemptsCommandTests(TestCase):
    """The secondary background-sweep management command — best-effort,
    idempotent, never required for correctness (see its own docstring)."""

    def setUp(self):
        self.student = User.objects.create_user(username='cmd_student', email='cmd_student@example.com', password='pw')
        self.test = _mkexam(duration_minutes=30)

    def _expired_attempt(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test)
        return _backdate_start(attempt, timezone.now() - timezone.timedelta(hours=1))

    def test_dry_run_reports_but_does_not_write(self):
        attempt = self._expired_attempt()
        out = StringIO()
        call_command('finalize_expired_attempts', '--dry-run', stdout=out)
        self.assertIn('Would finalize 1', out.getvalue())
        self.assertEqual(TestAttempt.objects.get(pk=attempt.pk).status, 'in_progress')

    def test_apply_finalizes_expired_and_leaves_active_alone(self):
        expired = self._expired_attempt()
        active = TestAttempt.objects.create(user=self.student, test=self.test)  # just started, not expired

        out = StringIO()
        call_command('finalize_expired_attempts', stdout=out)

        self.assertEqual(TestAttempt.objects.get(pk=expired.pk).status, 'submitted')
        self.assertTrue(TestAttempt.objects.get(pk=expired.pk).auto_submitted)
        self.assertEqual(TestAttempt.objects.get(pk=active.pk).status, 'in_progress')
        self.assertIn('Finalized 1 expired attempt(s) out of 2', out.getvalue())

    def test_running_twice_is_a_safe_no_op_the_second_time(self):
        self._expired_attempt()
        call_command('finalize_expired_attempts', stdout=StringIO())
        out2 = StringIO()
        call_command('finalize_expired_attempts', stdout=out2)
        self.assertIn('Finalized 0 expired attempt(s)', out2.getvalue())

    def test_command_and_request_time_check_racing_never_double_score(self):
        """The two backstops (command sweep, request-time check) agree —
        whichever gets there first wins, the second is a no-op."""
        attempt = self._expired_attempt()
        Answer.objects.create(attempt=attempt, question=Question.objects.create(
            subject=Subject.objects.create(name='Race CMD Subject'), text='Q', marks=3, negative_marks=0,
        ), is_correct=True, selected_option=None)

        # Command finalizes first.
        call_command('finalize_expired_attempts', stdout=StringIO())
        attempt.refresh_from_db()
        first_score = attempt.score

        # A request-time check on the now-already-submitted attempt is a no-op.
        result = ensure_finalized_if_expired(attempt)
        self.assertEqual(result.score, first_score)


class ConcurrentFinalizationTests(APITransactionTestCase):
    """Two overlapping finalization attempts (a manual submit racing an
    auto-finalize / the command) on the same attempt must never
    double-score — mirrors the existing SubmitTestDoubleSubmissionRaceTests
    pattern (same documented SQLite-vs-MySQL locking caveat)."""

    def test_concurrent_finalize_calls_only_score_once(self):
        import threading
        import time

        from django.db import connection

        student = User.objects.create_user(username='cf_student', email='cf_student@example.com', password='pw')
        subject = Subject.objects.create(name='Concurrent Finalize Subject')
        question = Question.objects.create(subject=subject, text='2+2=?', marks=4, negative_marks=0)
        correct = Option.objects.create(question=question, text='4', order=0, is_correct=True)
        test = _mkexam(duration_minutes=30)
        TestQuestion.objects.create(test=test, question=question)
        attempt = TestAttempt.objects.create(user=student, test=test)
        Answer.objects.create(attempt=attempt, question=question, selected_option=correct, is_correct=True)

        results = []
        lock = threading.Lock()

        def finalize_once():
            for attempt_no in range(20):
                try:
                    if connection.vendor == 'sqlite':
                        with connection.cursor() as cur:
                            cur.execute('PRAGMA busy_timeout = 30000')
                    a = TestAttempt.objects.get(pk=attempt.pk)
                    finalized = finalize_attempt(a, auto_submitted=True)
                    with lock:
                        results.append(float(finalized.score))
                    return
                except Exception as exc:  # noqa: BLE001 — SQLite lock-contention retry, matches existing precedent
                    if 'locked' in str(exc).lower() and attempt_no < 19:
                        time.sleep(0.05)
                        continue
                    raise
                finally:
                    connection.close()

        threads = [threading.Thread(target=finalize_once) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertTrue(all(r == 4.0 for r in results), results)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'submitted')
        self.assertEqual(float(attempt.score), 4.0)
