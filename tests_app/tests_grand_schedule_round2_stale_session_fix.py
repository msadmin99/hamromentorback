"""Grand Test schedule fix, round 2 — the reported regression: an admin
edits "Scheduled start"/"Scheduled end" via the exam-management form,
saves successfully (the Admin UI shows the new value), but the student
`/grand-test` page keeps showing the OLD date/time.

TRACED root cause (reproduced below, not merely described):

  1. WRITE PATH — exam-management/page.js's simple Scheduled start/end
     fields POST straight to TestAdminSerializer, which writes
     Test.scheduled_start/scheduled_end. This save genuinely succeeds —
     the new value really is in the database.

  2. READ PATH — the student card reads `grand_test_schedule`
     (TestListSerializer.get_grand_test_schedule / round 1 of this fix),
     which calls tests_app.lifecycle.resolve_test_schedule_session(obj).
     Whenever a real ExamSession already exists for this Test (created at
     some earlier point via "Reschedule" or the Admin's own "Edit
     Schedule" button), that function ALWAYS prefers the session over the
     plain fields — unconditionally, with no way to know the plain fields
     were just intentionally edited.

  3. RESULT: Test.scheduled_start/end genuinely changed in the database
     (step 1 really happened), but the student-visible grand_test_schedule
     never moves, because it was never reading that field in the first
     place — it was reading the untouched ExamSession the whole time.
     The admin's hypothesis in the bug report is confirmed exactly.

THE FIX: there must be exactly one live, writable schedule per Test —
decided by the same resolve_test_schedule_session() every reader already
uses. `TestAdminSerializer.active_session_id` now exposes which object
that is, so the Admin UI can disable the plain fields and point at the
one editor that actually reaches students (the "Edit Schedule" button /
PATCH /exam-sessions/{id}/) whenever a session exists — closing the
"two writable surfaces, only one of which is seen by anyone" hole at its
root, rather than trying to keep two independent sources magically in
sync.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from tests_app.lifecycle import resolve_test_schedule_session
from tests_app.models import Test
from tests_app.serializers import TestAdminSerializer, TestListSerializer
from tests_app.tests_phase6 import _mkexam, _mksession

User = get_user_model()


class StaleSessionScheduleReproductionTests(TestCase):
    """Reproduces the exact bug end-to-end: a Test with a real ExamSession,
    edited via the plain Test.scheduled_start/end fields (the ONLY path
    that existed before this fix), proves the student-visible schedule
    never moves."""

    def test_editing_plain_fields_does_not_change_what_students_see_when_a_session_exists(self):
        now = timezone.now()
        t = _mkexam(exam_type='grand', title='GT3 stale-session repro',
                    scheduled_start=now + timedelta(days=1), scheduled_end=now + timedelta(days=1, hours=3))
        session = _mksession(t, now + timedelta(days=1), now + timedelta(days=1, hours=3), status='scheduled')

        # This IS what resolve_test_schedule_session returns before any edit.
        self.assertEqual(resolve_test_schedule_session(t).id, session.id)

        # The admin edits the PLAIN fields (the old exam-management form's
        # only write path) — moves the start 3 hours later.
        new_start = now + timedelta(days=1, hours=3)
        new_end = now + timedelta(days=1, hours=5)
        ser = TestAdminSerializer(t, data={'scheduled_start': new_start.isoformat(), 'scheduled_end': new_end.isoformat()}, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        t.refresh_from_db()

        # The write genuinely succeeded.
        self.assertEqual(t.scheduled_start, new_start)

        # But the student-visible schedule is UNCHANGED — still the
        # session's original value, not the admin's new one. This is the
        # bug, reproduced directly.
        list_data = TestListSerializer(t, context={'request': None}).data
        self.assertEqual(list_data['grand_test_schedule']['start'], session.start_datetime)
        self.assertNotEqual(list_data['grand_test_schedule']['start'], t.scheduled_start)


class ActiveSessionIdExposureTests(APITestCase):
    """The fix's actual mechanism: TestAdminSerializer.active_session_id
    tells the Admin UI, unambiguously and from the same canonical
    resolver every other consumer uses, whether the plain fields are
    live (None) or dead weight (a real session id) for this Test."""

    def setUp(self):
        self.staff = User.objects.create_superuser(username='gt3r2_admin', email='gt3r2_admin@example.com', password='pw')
        self.client.force_authenticate(user=self.staff)

    def test_no_session_means_active_session_id_is_null_and_plain_fields_stay_live(self):
        now = timezone.now()
        t = _mkexam(exam_type='grand', title='No session', scheduled_start=now + timedelta(days=1), scheduled_end=now + timedelta(days=1, hours=3))
        resp = self.client.get(f'/api/tests/{t.id}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIsNone(resp.json()['active_session_id'])

    def test_real_session_means_active_session_id_points_at_it(self):
        now = timezone.now()
        t = _mkexam(exam_type='grand', title='Has session')
        session = _mksession(t, now + timedelta(days=1), now + timedelta(days=1, hours=3), status='scheduled')
        resp = self.client.get(f'/api/tests/{t.id}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()['active_session_id'], session.id)

    def test_non_grand_non_daily_exam_types_never_get_an_active_session_id(self):
        t = _mkexam(exam_type='mock', title='Mock exam')
        resp = self.client.get(f'/api/tests/{t.id}/')
        self.assertIsNone(resp.json()['active_session_id'])

    def test_cancelled_session_does_not_count_as_active(self):
        now = timezone.now()
        t = _mkexam(exam_type='grand', title='Cancelled session')
        _mksession(t, now + timedelta(days=1), now + timedelta(days=1, hours=3), status='cancelled')
        resp = self.client.get(f'/api/tests/{t.id}/')
        self.assertIsNone(resp.json()['active_session_id'])


class EditScheduleViaSessionReachesStudentsTests(APITestCase):
    """The ACTUAL correct write path (PATCH /exam-sessions/{id}/, what the
    Admin's 'Edit Schedule' button / EditSessionModal calls) must change
    what students see — proving the fix end-to-end, not just that the
    diagnosis field exists."""

    def setUp(self):
        self.staff = User.objects.create_superuser(username='gt3r2_admin2', email='gt3r2_admin2@example.com', password='pw')
        self.client.force_authenticate(user=self.staff)

    def test_patching_the_session_directly_changes_the_student_visible_schedule(self):
        now = timezone.now()
        t = _mkexam(exam_type='grand', title='Edit via session')
        session = _mksession(t, now + timedelta(days=1), now + timedelta(days=1, hours=3), status='scheduled')

        new_start = now + timedelta(days=2)
        new_end = now + timedelta(days=2, hours=3)
        resp = self.client.patch(
            f'/api/exam-sessions/{session.id}/',
            {'start_datetime': new_start.isoformat(), 'end_datetime': new_end.isoformat()},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        session.refresh_from_db()
        list_data = TestListSerializer(t, context={'request': None}).data
        self.assertEqual(list_data['grand_test_schedule']['start'], session.start_datetime)
        self.assertNotEqual(list_data['grand_test_schedule']['start'], now + timedelta(days=1))

    def test_session_update_rejects_end_before_start(self):
        now = timezone.now()
        t = _mkexam(exam_type='grand', title='Bad session edit')
        session = _mksession(t, now + timedelta(days=1), now + timedelta(days=1, hours=3), status='scheduled')

        resp = self.client.patch(
            f'/api/exam-sessions/{session.id}/',
            {'start_datetime': (now + timedelta(days=2)).isoformat(), 'end_datetime': (now + timedelta(days=1)).isoformat()},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('end_datetime', resp.json())


class UnrelatedEditNoLongerBlockedByStaleLegacyScheduleTests(APITestCase):
    """Guards against the bug my own round-1 validate() could have
    introduced: once the Admin UI stops sending scheduled_start/end for a
    session-governed Test, those plain fields can legitimately sit in a
    stale, inconsistent state forever — an unrelated save (e.g. changing
    the title) must never be blocked by a schedule pair nobody is
    actually touching."""

    def setUp(self):
        self.staff = User.objects.create_superuser(username='gt3r2_admin3', email='gt3r2_admin3@example.com', password='pw')
        self.client.force_authenticate(user=self.staff)

    def test_saving_an_unrelated_field_is_not_blocked_by_a_stale_already_invalid_schedule_pair(self):
        now = timezone.now()
        t = _mkexam(exam_type='grand', title='Stale pair', scheduled_start=now, scheduled_end=now - timedelta(days=1))
        resp = self.client.patch(f'/api/tests/{t.id}/', {'title': 'Renamed, unrelated to schedule'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
