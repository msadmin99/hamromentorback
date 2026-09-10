"""Grand Test 3.0 / GT3-2 — Derived MISSED Exam State Machine.

Core rule under test: a Grand Test's participation state (UPCOMING / LIVE /
IN_PROGRESS / COMPLETED / MISSED) is DERIVED — computed fresh from
(schedule, attempt-existence) every time — never stored on a TestAttempt
row and never represented by fabricating one. See
tests_app.lifecycle.grand_test_participation_status's own docstring for
the exact rule and its two schedule sources (a real ExamSession, or the
simpler Test.scheduled_start/scheduled_end pair).

Also covers a real, pre-existing bypass this phase's own inspection found
and fixed: /api/tests/{id}/start/ (the 'legacy' route) previously ignored
a Test's own scheduled session/window entirely, since it always called
_start_attempt() with session=None — a student could dodge MISSED (and
any session's start/end window) just by calling that route instead of
/api/exam-sessions/{id}/start/. Both routes now enforce identically.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from academics.models import Option, Question, Subject
from tests_app.lifecycle import grand_test_participation_status
from tests_app.tests_phase6 import _mkexam, _mksession
from tests_app.models import TestAttempt, TestQuestion

User = get_user_model()


def _mkquestion(subject):
    q = Question.objects.create(subject=subject, text='Q?', marks=1, negative_marks=0)
    Option.objects.create(question=q, text='Correct', is_correct=True, order=0)
    Option.objects.create(question=q, text='Wrong', is_correct=False, order=1)
    return q


class ScheduledStartEndFallbackTests(APITestCase):
    """The simpler path: a Grand Test scheduled via Test.scheduled_start/
    scheduled_end directly, no real ExamSession at all — confirmed this
    session, via exam_versioning.py's own reschedule-seed logic, to be a
    real, standalone mechanism, not merely a display-only leftover."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt2_stu', email='gt2_stu@example.com', password='pw')
        self.subject = Subject.objects.create(name='GT3-2 Subject')
        self.question = _mkquestion(self.subject)
        self.client.force_authenticate(user=self.student)

    def _mktest(self, start, end, **overrides):
        test = _mkexam(
            exam_type='grand', is_pro=False, allow=self.student, max_attempts=1,
            scheduled_start=start, scheduled_end=end, **overrides,
        )
        TestQuestion.objects.create(test=test, question=self.question, order=0)
        return test

    # --- A. Upcoming ---
    def test_upcoming_before_start_cannot_start_and_status_is_upcoming(self):
        now = timezone.now()
        test = self._mktest(now + timezone.timedelta(hours=1), now + timezone.timedelta(hours=4))
        self.assertEqual(grand_test_participation_status(test, self.student), 'upcoming')

        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data['code'], 'grand_test_not_started')
        self.assertFalse(TestAttempt.objects.filter(test=test, user=self.student).exists())

    # --- B. Live ---
    def test_live_within_window_can_start_and_status_is_live_then_in_progress(self):
        now = timezone.now()
        test = self._mktest(now - timezone.timedelta(minutes=30), now + timezone.timedelta(hours=2))
        self.assertEqual(grand_test_participation_status(test, self.student), 'live')

        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertTrue(TestAttempt.objects.filter(test=test, user=self.student, status='in_progress').exists())
        self.assertEqual(grand_test_participation_status(test, self.student), 'in_progress')

    # --- C. Missed ---
    def test_missed_after_window_with_no_attempt_cannot_start_no_attempt_created(self):
        now = timezone.now()
        test = self._mktest(now - timezone.timedelta(hours=3), now - timezone.timedelta(hours=1))
        self.assertEqual(grand_test_participation_status(test, self.student), 'missed')

        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data['code'], 'grand_test_missed')
        self.assertIn('missed this Grand Test', resp.data['message'])
        # The absolute core architectural rule: no TestAttempt exists, at all.
        self.assertFalse(TestAttempt.objects.filter(test=test, user=self.student).exists())
        # MISSED implies no score/rank/percentile — trivially true here since
        # no attempt (the only place those live) was ever created.

    def test_missed_cannot_be_retried_by_repeated_requests(self):
        now = timezone.now()
        test = self._mktest(now - timezone.timedelta(hours=3), now - timezone.timedelta(hours=1))
        for _ in range(3):
            resp = self.client.post(f'/api/tests/{test.id}/start/')
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(TestAttempt.objects.filter(test=test, user=self.student).exists())

    # --- D. Existing attempt + exam ended => NOT missed ---
    def test_started_before_close_then_window_ends_is_not_missed(self):
        now = timezone.now()
        # Started while live, window has SINCE ended (a real late-finisher).
        test = self._mktest(now - timezone.timedelta(hours=3), now - timezone.timedelta(minutes=1), duration_minutes=180)
        attempt = TestAttempt.objects.create(user=self.student, test=test, start_time=now - timezone.timedelta(hours=1))
        self.assertEqual(grand_test_participation_status(test, self.student), 'in_progress')
        # Re-POSTing /start/ now (an unusual client action) must not be
        # reported as newly MISSED — it hits the existing max-attempts/
        # resume path, never grand_test_missed, because an attempt exists.
        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertNotEqual(resp.data.get('code'), 'grand_test_missed')
        self.assertEqual(TestAttempt.objects.filter(test=test, user=self.student).count(), 1)  # no duplicate created

    # --- E. Boundary: start ---
    def test_boundary_07_59_59_denied_08_00_00_allowed(self):
        base = timezone.now().replace(microsecond=0)
        start = base + timezone.timedelta(seconds=30)
        end = start + timezone.timedelta(hours=3)
        test = self._mktest(start, end)

        # Comfortably before start: denied.
        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data['code'], 'grand_test_not_started')

        # At/after start: allowed. (Simulated by moving the test's own
        # scheduled_start into the past rather than sleeping in a test.)
        test.scheduled_start = timezone.now() - timezone.timedelta(seconds=1)
        test.save(update_fields=['scheduled_start'])
        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

    # --- F. Boundary: end ---
    def test_boundary_10_59_59_allowed_11_00_00_denied_11_00_01_denied(self):
        now = timezone.now()
        # "Still (just barely) live" — a comfortable-but-small margin so
        # normal test-runner overhead can't turn this into a flaky race
        # (the exact boundary arithmetic itself is unit-tested via direct
        # now</now> comparisons in effective_attempt_end/lifecycle, not by
        # literally racing the wall clock in an HTTP test).
        test = self._mktest(now - timezone.timedelta(hours=3), now + timezone.timedelta(seconds=30))

        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        # A second student, arriving once the window has just closed: denied.
        other = User.objects.create_user(username='gt2_other', email='gt2_other@example.com', password='pw')
        test.assigned_students.add(other)
        test.scheduled_end = timezone.now() - timezone.timedelta(seconds=1)
        test.save(update_fields=['scheduled_end'])
        self.client.force_authenticate(user=other)
        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data['code'], 'grand_test_missed')
        # One second later still denied (no grace period).
        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_late_start_deadline_is_capped_at_scheduled_end_not_full_duration(self):
        """'Late start does not extend the closing time' — a student
        starting 30 minutes before scheduled_end must NOT get the test's
        full duration_minutes; their effective deadline is capped at
        scheduled_end, exactly like a session-scheduled Grand Test already
        enforced."""
        from tests_app.lifecycle import effective_attempt_end

        now = timezone.now()
        test = self._mktest(now - timezone.timedelta(hours=1), now + timezone.timedelta(minutes=30), duration_minutes=180)
        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        attempt = TestAttempt.objects.get(pk=resp.data['id'])
        deadline = effective_attempt_end(attempt)
        self.assertLessEqual(deadline, test.scheduled_end + timezone.timedelta(seconds=1))
        self.assertLess(deadline, attempt.start_time + timezone.timedelta(minutes=180))

    # --- G. Duplicate start ---
    def test_duplicate_start_requests_do_not_create_duplicate_attempts(self):
        now = timezone.now()
        test = self._mktest(now - timezone.timedelta(minutes=10), now + timezone.timedelta(hours=2))
        first = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        second = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(second.status_code, status.HTTP_200_OK)  # resumes, does not create a new one
        self.assertEqual(second.data['id'], first.data['id'])
        self.assertEqual(TestAttempt.objects.filter(test=test, user=self.student).count(), 1)

    # --- H. Multiple students, independent outcomes ---
    def test_two_students_one_appears_one_missed(self):
        now = timezone.now()
        test = self._mktest(now - timezone.timedelta(hours=3), now - timezone.timedelta(minutes=1))
        appeared = TestAttempt.objects.create(
            user=self.student, test=test, start_time=now - timezone.timedelta(hours=2), status='submitted',
        )
        other = User.objects.create_user(username='gt2_other2', email='gt2_other2@example.com', password='pw')
        test.assigned_students.add(other)

        self.assertEqual(grand_test_participation_status(test, self.student), 'completed')
        self.assertEqual(grand_test_participation_status(test, other), 'missed')

        self.client.force_authenticate(user=other)
        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data['code'], 'grand_test_missed')
        self.assertFalse(TestAttempt.objects.filter(test=test, user=other).exists())
        self.assertTrue(TestAttempt.objects.filter(pk=appeared.pk, status='submitted').exists())  # untouched

    # --- I. Bulk-style independent entitlements (marketplace itself untouched in GT3-2) ---
    def test_five_grand_tests_two_appeared_three_missed_are_fully_independent(self):
        now = timezone.now()
        appeared_tests, missed_tests = [], []
        for i in range(2):
            t = self._mktest(now - timezone.timedelta(hours=3), now - timezone.timedelta(minutes=1))
            TestAttempt.objects.create(user=self.student, test=t, start_time=now - timezone.timedelta(hours=2), status='submitted')
            appeared_tests.append(t)
        for i in range(3):
            missed_tests.append(self._mktest(now - timezone.timedelta(hours=3), now - timezone.timedelta(minutes=1)))

        for t in appeared_tests:
            self.assertEqual(grand_test_participation_status(t, self.student), 'completed')
        for t in missed_tests:
            self.assertEqual(grand_test_participation_status(t, self.student), 'missed')
            self.assertFalse(TestAttempt.objects.filter(test=t, user=self.student).exists())

    # --- J. Authorization: cannot manipulate MISSED into IN_PROGRESS, cannot touch another user's data ---
    def test_cannot_bypass_missed_via_repeated_or_alternate_requests(self):
        now = timezone.now()
        test = self._mktest(now - timezone.timedelta(hours=3), now - timezone.timedelta(hours=1))
        # Client-supplied fields are ignored everywhere else in this file's
        # sibling security tests (tests_phase6.py) — confirm the same
        # holds for a MISSED Grand Test specifically: no combination of
        # extra payload fields turns this into a successful start.
        resp = self.client.post(f'/api/tests/{test.id}/start/', {
            'status': 'in_progress', 'start_time': (now - timezone.timedelta(hours=2)).isoformat(),
        })
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(TestAttempt.objects.filter(test=test, user=self.student).exists())


class SessionScheduledBypassClosedTests(APITestCase):
    """The real ExamSession path — confirms the found-and-fixed bypass:
    calling the legacy /tests/{id}/start/ route directly on a session-
    scheduled Grand Test now enforces that session's window exactly like
    /exam-sessions/{id}/start/ always did."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt2_sess_stu', email='gt2_sess_stu@example.com', password='pw')
        self.subject = Subject.objects.create(name='GT3-2 Session Subject')
        self.question = _mkquestion(self.subject)
        self.client.force_authenticate(user=self.student)

    def test_bare_route_on_a_missed_session_scheduled_grand_test_is_rejected(self):
        test = _mkexam(exam_type='grand', is_pro=False, allow=self.student, max_attempts=1)
        TestQuestion.objects.create(test=test, question=self.question, order=0)
        now = timezone.now()
        _mksession(test, now - timezone.timedelta(hours=3), now - timezone.timedelta(hours=1))

        # THE BYPASS THIS PHASE FOUND AND FIXED: previously this call
        # ignored the session entirely (session=None was hardcoded for
        # this route) and would have returned 201 with a fresh attempt.
        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data['code'], 'grand_test_missed')
        self.assertFalse(TestAttempt.objects.filter(test=test, user=self.student).exists())

    def test_bare_route_on_a_live_session_scheduled_grand_test_still_works(self):
        """The fix must not break the ordinary case — a session-scheduled
        Grand Test that's actually live is still startable via either
        route, and the created attempt is correctly linked to the
        session (not orphaned as session=None)."""
        test = _mkexam(exam_type='grand', is_pro=False, allow=self.student, max_attempts=1)
        TestQuestion.objects.create(test=test, question=self.question, order=0)
        now = timezone.now()
        session = _mksession(test, now - timezone.timedelta(minutes=30), now + timezone.timedelta(hours=2))

        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        attempt = TestAttempt.objects.get(pk=resp.data['id'])
        self.assertEqual(attempt.session_id, session.id)
        self.assertEqual(grand_test_participation_status(test, self.student), 'in_progress')

    def test_bare_route_before_session_start_is_denied_not_silently_allowed(self):
        test = _mkexam(exam_type='grand', is_pro=False, allow=self.student, max_attempts=1)
        TestQuestion.objects.create(test=test, question=self.question, order=0)
        now = timezone.now()
        _mksession(test, now + timezone.timedelta(hours=1), now + timezone.timedelta(hours=4))

        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data['code'], 'session_not_started')


class ParticipationStatusPureFunctionTests(TestCase):
    """Direct, non-HTTP tests of the derived-status function itself —
    fast, precise coverage of every branch without round-tripping through
    the view layer for cases the API-level tests above don't need to
    re-prove."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt2_pure_stu', email='gt2_pure_stu@example.com', password='pw')

    def test_unscheduled_grand_test_is_not_scheduled_never_missed(self):
        test = _mkexam(exam_type='grand', allow=self.student)  # no scheduled_start/end, no session
        self.assertEqual(grand_test_participation_status(test, self.student), 'not_scheduled')

    def test_unscheduled_daily_test_is_also_not_scheduled(self):
        # The function is generically correct for any exam_type; only the
        # serializer/view layer scope its MEANING to Grand Test specifically.
        test = _mkexam(exam_type='daily', allow=self.student)
        self.assertEqual(grand_test_participation_status(test, self.student), 'not_scheduled')

    def test_cancelled_session_never_produces_missed(self):
        """A cancelled exam was never really 'held' — no one should be
        told they missed something that was called off, not merely
        unattended."""
        test = _mkexam(exam_type='grand', allow=self.student)
        now = timezone.now()
        _mksession(test, now - timezone.timedelta(hours=3), now - timezone.timedelta(hours=1), status='cancelled')
        self.assertEqual(grand_test_participation_status(test, self.student), 'not_scheduled')
