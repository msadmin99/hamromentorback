"""Daily Test schedule audit — regression suite for the six approved
changes:
  1. card_access.py: resolve_card_access() extended to exam_type='daily'
  2. views.py: _start_attempt() extended to exam_type='daily'
  3. lifecycle.py: effective_attempt_end() extended to exam_type='daily'
  4. PastTestRow.js — covered by the Admin/Frontend test suite, not here
  5. daily-test/page.js + UpcomingTestRow.js — covered by the Frontend
     test suite, not here
  6. TestConfigStep.js timezone fix — covered by the Admin test suite

Mirrors tests_phase6.py's _mkexam/_backdate_start conventions (kept
separate from that file rather than added to it, since this audit's
scope is specifically Daily Test scheduling, not the whole Phase 6
matrix). Explicitly includes regression coverage proving Grand Test's
own, already-correct behavior is untouched by widening these three
functions' conditions — see each *_grand_test_* test below.

Covers: future, today/mid-window, closed-window-missed,
closed-window-still-reviewable, start-at-open-boundary,
start-at-close-boundary (the one instant where Daily Test is
deliberately stricter than Grand Test), unscheduled-test (backward
compatibility), the 45-minute personal duration (already-correct,
confirmed unaffected), and the 24h-window cap on a late start.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from academics.models import Question, Subject
from tests_app.card_access import StudentEntitlementSnapshot, resolve_card_access
from tests_app.lifecycle import effective_attempt_end
from tests_app.models import Test, TestAttempt, TestQuestion

User = get_user_model()


def _mkdaily(allow=None, **overrides):
    """Mirrors tests_phase6._mkexam exactly, fixed to exam_type='daily'
    and a duration matching this feature's own worked example (45m)."""
    fields = {'title': 'Daily exam', 'exam_type': 'daily', 'duration_minutes': 45, 'is_draft': False}
    fields.update(overrides)
    test = Test.objects.create(**fields)
    if allow:
        users = allow if isinstance(allow, (list, tuple)) else [allow]
        test.assigned_students.set(users)
    return test


def _mkgrand(**overrides):
    fields = {'title': 'Grand exam', 'exam_type': 'grand', 'duration_minutes': 180, 'is_draft': False}
    fields.update(overrides)
    return Test.objects.create(**fields)


def _backdate_start(attempt, when):
    """auto_now_add only fires on INSERT — bypass Model.save() via a
    queryset .update() to reliably backdate start_time for tests."""
    TestAttempt.objects.filter(pk=attempt.pk).update(start_time=when)
    attempt.refresh_from_db()
    return attempt


class DailyTestStartAttemptEnforcementTests(APITestCase):
    """POST /api/tests/<id>/start/ — server-side enforcement
    (views.py:_start_attempt). This is the route ExamCard.js's "Start
    Test" button actually calls (TestViewSet.start -> _start_attempt
    with session=None) — not the ExamSession-based route, which Daily
    Test's real admin workflow never uses."""

    def setUp(self):
        self.student = User.objects.create_user(username='daily_student', email='daily_student@example.com', password='pw')
        self.subject = Subject.objects.create(name='Daily Subject')
        self.question = Question.objects.create(subject=self.subject, text='Q?', marks=1, negative_marks=0)
        self.client.force_authenticate(user=self.student)

    def _add_question(self, test):
        TestQuestion.objects.create(test=test, question=self.question)

    def test_future_scheduled_start_blocks_starting(self):
        now = timezone.now()
        test = _mkdaily(
            allow=self.student, scheduled_start=now + timezone.timedelta(hours=2),
            scheduled_end=now + timezone.timedelta(hours=26),
        )
        self._add_question(test)

        resp = self.client.post(f'/api/tests/{test.id}/start/')

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data.get('code'), 'daily_test_not_started')
        self.assertFalse(TestAttempt.objects.filter(test=test).exists())

    def test_mid_window_allows_starting_at_any_point(self):
        now = timezone.now()
        test = _mkdaily(
            allow=self.student, scheduled_start=now - timezone.timedelta(hours=10),
            scheduled_end=now + timezone.timedelta(hours=14),
        )
        self._add_question(test)

        resp = self.client.post(f'/api/tests/{test.id}/start/')

        self.assertEqual(resp.status_code, 201)
        self.assertTrue(TestAttempt.objects.filter(test=test, user=self.student, status='in_progress').exists())

    def test_start_at_the_open_boundary_is_allowed(self):
        now = timezone.now()
        test = _mkdaily(
            allow=self.student, scheduled_start=now - timezone.timedelta(milliseconds=50),
            scheduled_end=now + timezone.timedelta(hours=24),
        )
        self._add_question(test)

        resp = self.client.post(f'/api/tests/{test.id}/start/')

        self.assertEqual(resp.status_code, 201)

    def test_window_closed_no_prior_attempt_blocks_starting_with_daily_missed_code(self):
        now = timezone.now()
        test = _mkdaily(
            allow=self.student, scheduled_start=now - timezone.timedelta(hours=30),
            scheduled_end=now - timezone.timedelta(hours=6),
        )
        self._add_question(test)

        resp = self.client.post(f'/api/tests/{test.id}/start/')

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data.get('code'), 'daily_test_missed')

    def test_start_at_the_close_boundary_is_blocked(self):
        """Daily Test's own requirement: 'at/after scheduled_end, no new
        starting' — one instant stricter than Grand Test's own existing
        `now > scheduled_end` (see the sibling Grand Test test below,
        which allows exactly-on-boundary and must keep doing so)."""
        now = timezone.now()
        test = _mkdaily(
            allow=self.student, scheduled_start=now - timezone.timedelta(hours=24),
            scheduled_end=now - timezone.timedelta(milliseconds=1),
        )
        self._add_question(test)

        resp = self.client.post(f'/api/tests/{test.id}/start/')

        self.assertEqual(resp.status_code, 403)

    def test_no_schedule_at_all_is_completely_unaffected(self):
        """A Daily Test with no scheduled_start/end set must behave
        exactly as before this change — always startable — matching
        every Daily Test created before this feature existed."""
        test = _mkdaily(allow=self.student)
        self._add_question(test)

        resp = self.client.post(f'/api/tests/{test.id}/start/')

        self.assertEqual(resp.status_code, 201)

    def test_closed_window_but_already_submitted_can_still_be_reviewed(self):
        """A student who attempted within the window keeps review access
        after the window closes — 'missed' only ever applies to a
        student who never started one at all."""
        now = timezone.now()
        test = _mkdaily(
            allow=self.student, scheduled_start=now - timezone.timedelta(hours=30),
            scheduled_end=now - timezone.timedelta(hours=6),
        )
        self._add_question(test)
        TestAttempt.objects.create(user=self.student, test=test, status='submitted', score=1)

        resp = self.client.get(f'/api/tests/?exam_type=daily')

        self.assertEqual(resp.status_code, 200)
        row = next(r for r in resp.data if r['id'] == test.id)
        self.assertEqual(row['access']['state'], 'review')

    def test_grand_test_start_at_close_boundary_is_still_allowed_unchanged(self):
        """Regression guard: Grand Test's own boundary behavior
        (`now > scheduled_end`, exactly-on-boundary still allowed) must
        be byte-for-byte unchanged by this audit's branching. Freezes
        `timezone.now()` to exactly `scheduled_end` — a real wall-clock
        test can never land on that exact instant by construction, since
        more time always elapses between fixture setup and the view's own
        `timezone.now()` call."""
        window_end = timezone.now() + timezone.timedelta(hours=1)
        test = _mkgrand(scheduled_start=window_end - timezone.timedelta(hours=4), scheduled_end=window_end)
        gq = Question.objects.create(subject=self.subject, text='GQ?', marks=1, negative_marks=0)
        TestQuestion.objects.create(test=test, question=gq)
        test.assigned_students.set([self.student])

        with patch('tests_app.views.timezone.now', return_value=window_end):
            resp = self.client.post(f'/api/tests/{test.id}/start/')

        self.assertEqual(resp.status_code, 201)

    def test_daily_test_start_at_close_boundary_is_blocked_precisely(self):
        """The precise counterpart to the Grand Test test above, using the
        same time-freeze technique: Daily Test's stricter `now >=
        scheduled_end` means exactly-on-boundary is blocked, unlike
        Grand Test's `now > scheduled_end`."""
        window_end = timezone.now() + timezone.timedelta(hours=1)
        test = _mkdaily(
            allow=self.student, scheduled_start=window_end - timezone.timedelta(hours=24), scheduled_end=window_end,
        )
        self._add_question(test)

        with patch('tests_app.views.timezone.now', return_value=window_end):
            resp = self.client.post(f'/api/tests/{test.id}/start/')

        self.assertEqual(resp.status_code, 403)


class DailyTestAccessStateTests(TestCase):
    """access.state (card_access.py: resolve_card_access) — the field
    ExamCard.js/accessState.js actually render the badge and CTA from."""

    def setUp(self):
        self.student = User.objects.create_user(username='daily_access', email='daily_access@example.com', password='pw')

    def _access_for(self, test):
        snapshot = StudentEntitlementSnapshot(self.student)
        return resolve_card_access(test, snapshot, attempts=[])

    def test_future_daily_test_access_state_is_upcoming(self):
        now = timezone.now()
        test = _mkdaily(scheduled_start=now + timezone.timedelta(days=3), scheduled_end=now + timezone.timedelta(days=3, hours=24))

        access = self._access_for(test)

        self.assertEqual(access['state'], 'upcoming')

    def test_open_window_daily_test_access_state_is_start(self):
        now = timezone.now()
        test = _mkdaily(scheduled_start=now - timezone.timedelta(hours=2), scheduled_end=now + timezone.timedelta(hours=22))

        access = self._access_for(test)

        self.assertEqual(access['state'], 'start')

    def test_closed_window_daily_test_access_state_is_missed(self):
        now = timezone.now()
        test = _mkdaily(scheduled_start=now - timezone.timedelta(hours=30), scheduled_end=now - timezone.timedelta(hours=6))

        access = self._access_for(test)

        self.assertEqual(access['state'], 'missed')

    def test_unscheduled_daily_test_access_state_is_unaffected(self):
        test = _mkdaily()

        access = self._access_for(test)

        self.assertEqual(access['state'], 'start')

    def test_grand_test_access_state_is_unaffected_by_widening_the_condition(self):
        now = timezone.now()
        test = _mkgrand(scheduled_start=now + timezone.timedelta(days=1), scheduled_end=now + timezone.timedelta(days=1, hours=3))

        access = self._access_for(test)

        self.assertEqual(access['state'], 'upcoming')


class DailyTestAttemptDurationTests(TestCase):
    """lifecycle.py: effective_attempt_end() — the 45-minute personal
    timer (already-correct, generic across exam types — confirmed
    unaffected) plus the new 24h-window cap for a late-starting Daily
    Test attempt."""

    def setUp(self):
        self.student = User.objects.create_user(username='daily_timer', email='daily_timer@example.com', password='pw')

    def test_45_minute_personal_duration_is_unaffected_well_inside_the_window(self):
        """The worked example from the spec: a 60-MCQ/45-minute Daily
        Test started with most of the 24h window still remaining gets
        exactly its configured 45 minutes, not something shorter."""
        now = timezone.now()
        test = _mkdaily(duration_minutes=45, scheduled_start=now - timezone.timedelta(hours=1), scheduled_end=now + timezone.timedelta(hours=23))
        attempt = TestAttempt.objects.create(user=self.student, test=test, status='in_progress')
        attempt = _backdate_start(attempt, now)

        end = effective_attempt_end(attempt)

        self.assertEqual(end, attempt.start_time + timezone.timedelta(minutes=45))

    def test_late_start_is_capped_at_scheduled_end_not_the_full_45_minutes(self):
        """Starting 10 minutes before the window closes must cap the
        attempt at scheduled_end, not grant the full 45 minutes past it —
        the exact requirement: 'if the attempt would extend beyond
        scheduled_end, cap the attempt at scheduled_end.'"""
        now = timezone.now()
        window_end = now + timezone.timedelta(minutes=10)
        test = _mkdaily(duration_minutes=45, scheduled_start=now - timezone.timedelta(hours=23, minutes=50), scheduled_end=window_end)
        attempt = TestAttempt.objects.create(user=self.student, test=test, status='in_progress')
        attempt = _backdate_start(attempt, now)

        end = effective_attempt_end(attempt)

        self.assertEqual(end, window_end)
        self.assertLess(end, attempt.start_time + timezone.timedelta(minutes=45))

    def test_grand_test_capping_is_unaffected_by_widening_the_condition(self):
        now = timezone.now()
        window_end = now + timezone.timedelta(minutes=5)
        test = _mkgrand(duration_minutes=180, scheduled_start=now - timezone.timedelta(hours=2), scheduled_end=window_end)
        attempt = TestAttempt.objects.create(user=self.student, test=test, status='in_progress')
        attempt = _backdate_start(attempt, now)

        end = effective_attempt_end(attempt)

        self.assertEqual(end, window_end)

    def test_unscheduled_daily_test_personal_duration_is_completely_unaffected(self):
        test = _mkdaily(duration_minutes=45)
        now = timezone.now()
        attempt = TestAttempt.objects.create(user=self.student, test=test, status='in_progress')
        attempt = _backdate_start(attempt, now)

        end = effective_attempt_end(attempt)

        self.assertEqual(end, attempt.start_time + timezone.timedelta(minutes=45))
