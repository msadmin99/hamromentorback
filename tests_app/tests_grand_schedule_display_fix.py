"""Grand Test schedule display/timezone fix.

Covers the root cause this fix addresses:

  1. TestListSerializer (GET /tests/?exam_type=grand — what the student
     Grand Test listing page / ExamCard.js actually consumes) previously
     never exposed `grand_test_schedule`/`grand_test_status` at all — only
     TestDetailSerializer did. A Grand Test scheduled via a real
     ExamSession could therefore show a stale/disconnected date on its
     card even though access control already correctly used the session.
     This file proves the LIST endpoint now returns the same
     session-authoritative schedule the detail endpoint always has.

  2. TestAdminSerializer had no `scheduled_end >= scheduled_start`
     validation at all (the richer ExamSession path already had the
     equivalent check via exam_versioning.validate_session_window).

  3. A timezone round-trip proof: an admin-submitted ISO datetime with an
     explicit Asia/Kathmandu offset (+05:45) must be stored as the
     correct absolute UTC instant and must reproduce the exact original
     Kathmandu wall-clock time when converted back — the exact contract
     the fixed Admin frontend now relies on (see Admin/src/lib/
     kathmanduDatetime.js, which attaches this offset client-side instead
     of sending a naive datetime-local string, the actual root cause of
     the reported "12:45 AM" bug).

Scenario labels A-F below match the originating spec's own lettering.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from academics.models import Option, Question, Subject
from tests_app.models import Test, TestQuestion
from tests_app.tests_phase6 import _mkexam, _mksession

User = get_user_model()
KATHMANDU = ZoneInfo('Asia/Kathmandu')


def _mkquestion(subject):
    q = Question.objects.create(subject=subject, text='Q?', marks=1, negative_marks=0)
    Option.objects.create(question=q, text='Correct', is_correct=True, order=0)
    Option.objects.create(question=q, text='Wrong', is_correct=False, order=1)
    return q


class GrandTestListScheduleExposureTests(APITestCase):
    """The LIST endpoint (ExamCard.js's actual data source) must return
    the same authoritative grand_test_schedule/status the detail endpoint
    already returns — session-resolved when a session exists, the plain
    fields otherwise, None for an unscheduled test or any non-grand type."""

    def setUp(self):
        self.student = User.objects.create_user(username='sched_stu', email='sched_stu@example.com', password='pw')
        self.subject = Subject.objects.create(name='Schedule Fix Subject')
        self.question = _mkquestion(self.subject)
        self.client.force_authenticate(user=self.student)

    def _list(self):
        resp = self.client.get('/api/tests/?exam_type=grand')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        return resp.json()

    def _mktest(self, **overrides):
        test = _mkexam(exam_type='grand', is_pro=False, allow=self.student, max_attempts=1, **overrides)
        TestQuestion.objects.create(test=test, question=self.question, order=0)
        return test

    # --- Scenario D: no schedule at all — unaffected ---
    def test_unscheduled_grand_test_list_card_has_null_schedule(self):
        self._mktest(title='Unscheduled GT')
        [row] = [r for r in self._list() if r['title'] == 'Unscheduled GT']
        self.assertIsNone(row['grand_test_schedule'])
        self.assertEqual(row['grand_test_status'], 'not_scheduled')

    # --- Scenario A: plain-field scheduling, future start ---
    def test_plain_field_future_schedule_appears_on_list_card(self):
        now = timezone.now()
        start, end = now + timedelta(days=1), now + timedelta(days=1, hours=3)
        self._mktest(title='Plain Future GT', scheduled_start=start, scheduled_end=end)
        [row] = [r for r in self._list() if r['title'] == 'Plain Future GT']
        self.assertEqual(row['grand_test_status'], 'upcoming')
        self.assertIsNotNone(row['grand_test_schedule'])
        self.assertEqual(row['grand_test_schedule']['start'][:19], start.isoformat()[:19])
        self.assertFalse(row['access']['can_start'])
        self.assertEqual(row['access']['state'], 'upcoming')

    # --- The actual root-cause case: a real ExamSession's schedule must
    # win over a stale/absent plain Test.scheduled_start/end, on the LIST
    # endpoint specifically (this is what was broken). ---
    def test_session_schedule_wins_over_stale_plain_fields_on_list_card(self):
        now = timezone.now()
        # Plain fields deliberately stale/wrong (as if left over from
        # before this Test was ever put through Reschedule) — the session
        # below is the real, current schedule and must be what shows.
        test = self._mktest(
            title='Session-Scheduled GT',
            scheduled_start=now - timedelta(days=30),
            scheduled_end=now - timedelta(days=29),
        )
        real_start = now + timedelta(hours=2)
        real_end = now + timedelta(hours=5)
        _mksession(test, real_start, real_end, status='scheduled')

        [row] = [r for r in self._list() if r['title'] == 'Session-Scheduled GT']
        self.assertEqual(row['grand_test_status'], 'upcoming')
        self.assertEqual(row['grand_test_schedule']['start'][:19], real_start.isoformat()[:19])
        # And access control agrees with what's displayed — the exact
        # "Available + Start Test shown while displayed time is still in
        # the future" bug this fix closes end-to-end.
        self.assertFalse(row['access']['can_start'])
        self.assertEqual(row['access']['state'], 'upcoming')

    # --- Scenario B: live window ---
    def test_live_window_allows_start_and_schedule_is_shown(self):
        now = timezone.now()
        self._mktest(title='Live GT', scheduled_start=now - timedelta(hours=1), scheduled_end=now + timedelta(hours=1))
        [row] = [r for r in self._list() if r['title'] == 'Live GT']
        self.assertEqual(row['grand_test_status'], 'live')
        self.assertIsNotNone(row['grand_test_schedule'])
        self.assertTrue(row['access']['can_start'])

    # --- Scenario C: closed window, never attempted -> missed ---
    def test_closed_window_blocks_start_and_reports_missed(self):
        now = timezone.now()
        self._mktest(title='Closed GT', scheduled_start=now - timedelta(hours=3), scheduled_end=now - timedelta(hours=1))
        [row] = [r for r in self._list() if r['title'] == 'Closed GT']
        self.assertEqual(row['grand_test_status'], 'missed')
        self.assertFalse(row['access']['can_start'])
        self.assertEqual(row['access']['state'], 'missed')

    # --- Non-grand exam types must be completely unaffected ---
    def test_daily_test_has_no_grand_test_schedule_field_value(self):
        daily = _mkexam(exam_type='daily', is_pro=False, allow=self.student, title='Daily unaffected')
        TestQuestion.objects.create(test=daily, question=self.question, order=0)
        resp = self.client.get('/api/tests/?exam_type=daily')
        [row] = [r for r in resp.json() if r['title'] == 'Daily unaffected']
        self.assertIsNone(row['grand_test_schedule'])
        self.assertIsNone(row['grand_test_status'])


class TestAdminScheduleValidationTests(APITestCase):
    """§12 — scheduled_end must not be before scheduled_start; either may
    be omitted. Exercised through the real admin endpoint, matching how
    the Admin frontend's ExamBuilderModal/exam-management page actually
    calls it."""

    def setUp(self):
        self.staff = User.objects.create_superuser(username='sched_admin', email='sched_admin@example.com', password='pw')
        self.client.force_authenticate(user=self.staff)

    def _base_payload(self, **overrides):
        payload = {
            'title': 'Admin Schedule Test', 'exam_type': 'grand', 'duration_minutes': 60,
            'is_draft': True, 'courses': [], 'assigned_students': [], 'assigned_batches': [],
        }
        payload.update(overrides)
        return payload

    def test_rejects_end_before_start(self):
        resp = self.client.post(
            '/api/tests/',
            self._base_payload(
                scheduled_start='2026-10-03T13:15:00+00:00',
                scheduled_end='2026-10-03T10:00:00+00:00',
            ),
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('scheduled_end', resp.json())

    def test_rejects_end_equal_to_start(self):
        resp = self.client.post(
            '/api/tests/',
            self._base_payload(
                scheduled_start='2026-10-03T13:15:00+00:00',
                scheduled_end='2026-10-03T13:15:00+00:00',
            ),
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_accepts_valid_window(self):
        resp = self.client.post(
            '/api/tests/',
            self._base_payload(
                scheduled_start='2026-10-03T13:15:00+00:00',
                scheduled_end='2026-10-03T16:15:00+00:00',
            ),
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_both_omitted_is_still_valid_unscheduled_test(self):
        resp = self.client.post('/api/tests/', self._base_payload(), format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(resp.json()['scheduled_start'])

    # --- Scenario F: admin edits an existing scheduled test ---
    def test_editing_an_existing_test_still_validates_and_propagates(self):
        create = self.client.post(
            '/api/tests/',
            self._base_payload(
                scheduled_start='2026-10-03T13:15:00+00:00',
                scheduled_end='2026-10-03T16:15:00+00:00',
            ),
            format='json',
        )
        test_id = create.json()['id']

        bad_edit = self.client.patch(
            f'/api/tests/{test_id}/', {'scheduled_end': '2026-10-03T12:00:00+00:00'}, format='json',
        )
        self.assertEqual(bad_edit.status_code, status.HTTP_400_BAD_REQUEST)

        good_edit = self.client.patch(
            f'/api/tests/{test_id}/',
            {'scheduled_start': '2026-10-04T13:15:00+00:00', 'scheduled_end': '2026-10-04T16:15:00+00:00'},
            format='json',
        )
        self.assertEqual(good_edit.status_code, status.HTTP_200_OK)

        fetched = self.client.get(f'/api/tests/{test_id}/').json()
        self.assertEqual(fetched['scheduled_start'][:10], '2026-10-04')


class KathmanduTimezoneRoundTripTests(TestCase):
    """Scenario E — the actual mechanism behind the reported "12:45 AM"
    bug: a naive datetime-local value with no offset gets silently
    interpreted as UTC (settings.TIME_ZONE='UTC', no per-request
    timezone.activate() anywhere in this project — confirmed by
    inspection). Proves the FIX's contract: once the Admin frontend
    attaches an explicit +05:45 offset (Admin/src/lib/
    kathmanduDatetime.js's kathmanduLocalToISOString), the backend stores
    the correct absolute instant and converting it back to Kathmandu
    reproduces the admin's exact original wall-clock entry — not a
    UTC-shifted one."""

    def test_explicit_kathmandu_offset_round_trips_to_the_same_wall_clock_time(self):
        # Admin enters "October 3, 2026, 7:00 PM" intending Asia/Kathmandu.
        submitted_start = datetime(2026, 10, 3, 19, 0, 0, tzinfo=KATHMANDU)
        submitted_end = datetime(2026, 10, 3, 22, 0, 0, tzinfo=KATHMANDU)
        test = Test.objects.create(
            title='TZ round trip', exam_type='grand', duration_minutes=60,
            scheduled_start=submitted_start, scheduled_end=submitted_end,
        )
        test.refresh_from_db()
        kathmandu_local = test.scheduled_start.astimezone(KATHMANDU)
        self.assertEqual((kathmandu_local.year, kathmandu_local.month, kathmandu_local.day), (2026, 10, 3))
        self.assertEqual((kathmandu_local.hour, kathmandu_local.minute), (19, 0))

    def test_naive_datetime_without_offset_is_the_bug_this_fix_avoids(self):
        """Documents the exact failure mode: a bare 'YYYY-MM-DDTHH:MM' with
        no offset — what the Admin form used to send before this fix — is
        interpreted as UTC by Django (settings.TIME_ZONE='UTC'), landing
        5h45m later than intended once viewed in Kathmandu. This is a
        characterization test of the bug class, not of currently-shipping
        behavior — it documents why the frontend must never send a naive
        string (see Admin/src/lib/kathmanduDatetime.js), which the fixed
        Admin code no longer does."""
        from django.utils.dateparse import parse_datetime

        naive_input = '2026-10-03T19:00:00'  # what a bare datetime-local value looks like
        parsed = parse_datetime(naive_input)
        aware = timezone.make_aware(parsed)  # what DRF does by default under USE_TZ=True, TIME_ZONE='UTC'
        shifted_kathmandu = aware.astimezone(KATHMANDU)
        # 19:00 UTC + 5:45 crosses midnight -> 00:45 the NEXT day, i.e. the
        # exact ":45"-past-midnight fingerprint reported in production.
        self.assertEqual(shifted_kathmandu.hour, 0)
        self.assertEqual(shifted_kathmandu.minute, 45)
        self.assertEqual(shifted_kathmandu.day, 4)
