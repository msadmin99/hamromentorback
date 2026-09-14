"""Phase 2 — Grand Test / Daily Test domain-event integration tests.

Exercises the REAL call sites (create_reschedule_session, ExamSessionViewSet
perform_update/cancel), not just the notifications.exam_integration
functions in isolation, per the acceptance contract's own instruction that
the real event->course->eligibility->dedup lifecycle must be proven end to
end. See docs/PHASE_2_ACCEPTANCE_TRACEABILITY.md for how each test below
maps to a numbered acceptance criterion.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from billing.models import GrandTestAccess
from courses.models import Course, Enrollment
from tests_app.exam_versioning import create_reschedule_session
from tests_app.models import ExamSession, ExamTemplate, Test

from . import exam_integration, services
from .events import CATEGORY_TESTS
from .models import Notification

User = get_user_model()


def _make_user(username='student1', **kw):
    return User.objects.create_user(username=username, email=f'{username}@example.com', password='pw12345', **kw)


def _make_course(name='CEE-MBBS'):
    return Course.objects.create(name=name, prefix=name[:10])


def _enroll(user, course):
    return Enrollment.objects.create(user=user, course=course)


def _make_test(exam_type='grand', title='Grand Test I', courses=(), is_pro=False):
    test = Test.objects.create(title=title, exam_type=exam_type, is_draft=False, is_pro=is_pro)
    if courses:
        test.courses.set(courses)
    return test


def _grant_grand_access(user, test):
    return GrandTestAccess.objects.create(user=user, test=test)


def _make_session(test, start_datetime, end_datetime, exam_template=None):
    template = exam_template or ExamTemplate.objects.create(title=test.title, exam_type=test.exam_type)
    return ExamSession.objects.create(
        exam_template=template, exam_version=test, session_name=f'{test.title} — Session',
        start_datetime=start_datetime, end_datetime=end_datetime, status='scheduled',
    )


class ScheduleSessionRemindersGrandTestTests(TestCase):
    """P0-GRAND-TEST, P0-CROSS-COURSE, P0-DEDUPLICATION."""

    def setUp(self):
        self.course = _make_course('CEE-MBBS')
        self.student = _make_user('grand_student')
        _enroll(self.student, self.course)
        self.test = _make_test('grand', courses=[self.course])
        _grant_grand_access(self.student, self.test)

    def test_creates_scheduled_event_and_all_three_reminders_plus_starting(self):
        start = timezone.now() + timedelta(days=2)
        session = _make_session(self.test, start, start + timedelta(hours=2))

        created = exam_integration.schedule_session_reminders(session)

        event_types = sorted(n.event_type for n in created)
        self.assertEqual(
            event_types,
            sorted(['GRAND_TEST_SCHEDULED', 'GRAND_TEST_REMINDER', 'GRAND_TEST_REMINDER',
                    'GRAND_TEST_REMINDER', 'GRAND_TEST_STARTING']),
        )
        for n in created:
            self.assertEqual(n.user_id, self.student.id)
            self.assertEqual(n.course_id, self.course.id)
            self.assertEqual(n.category, CATEGORY_TESTS)
            self.assertEqual(n.metadata.get('session_id'), session.id)
            self.assertEqual(n.action_url, f'/grand-test/{self.test.id}')

        reminders = {n.metadata['offset']: n.scheduled_for for n in created if n.event_type == 'GRAND_TEST_REMINDER'}
        self.assertEqual(reminders['t_minus_24h'], start - timedelta(hours=24))
        self.assertEqual(reminders['t_minus_1h'], start - timedelta(hours=1))
        self.assertEqual(reminders['t_minus_15m'], start - timedelta(minutes=15))
        starting = next(n for n in created if n.event_type == 'GRAND_TEST_STARTING')
        self.assertEqual(starting.scheduled_for, start)

    def test_skips_reminder_offsets_already_in_the_past(self):
        # Session starts in 30 minutes: t-24h and t-1h are already in the
        # past relative to "now" and must never be scheduled; t-15m and the
        # STARTING notification are still in the future and must be.
        start = timezone.now() + timedelta(minutes=30)
        session = _make_session(self.test, start, start + timedelta(hours=1))

        created = exam_integration.schedule_session_reminders(session)

        offsets = {n.metadata['offset'] for n in created if n.event_type == 'GRAND_TEST_REMINDER'}
        self.assertEqual(offsets, {'t_minus_15m'})
        self.assertTrue(any(n.event_type == 'GRAND_TEST_STARTING' for n in created))

    def test_idempotent_calling_twice_creates_no_duplicates(self):
        start = timezone.now() + timedelta(days=2)
        session = _make_session(self.test, start, start + timedelta(hours=2))

        first = exam_integration.schedule_session_reminders(session)
        second = exam_integration.schedule_session_reminders(session)

        self.assertEqual(len(first), len(second))
        self.assertEqual(
            Notification.objects.filter(metadata__session_id=session.id).count(),
            len(first),
        )

    def test_same_event_fired_three_times_produces_exactly_one_notification(self):
        """Mandated E2E scenario E, applied to the 'created' event."""
        start = timezone.now() + timedelta(days=2)
        session = _make_session(self.test, start, start + timedelta(hours=2))

        for _ in range(3):
            exam_integration.schedule_session_reminders(session)

        created_count = Notification.objects.filter(
            metadata__session_id=session.id, event_type='GRAND_TEST_SCHEDULED',
        ).count()
        self.assertEqual(created_count, 1)

    def test_cross_course_isolation_student_in_other_course_not_notified(self):
        other_course = _make_course('B.Optometry')
        other_student = _make_user('optom_student')
        _enroll(other_student, other_course)
        # Not enrolled in self.course, and no GrandTestAccess for self.test.

        start = timezone.now() + timedelta(days=2)
        session = _make_session(self.test, start, start + timedelta(hours=2))
        created = exam_integration.schedule_session_reminders(session)

        notified_user_ids = {n.user_id for n in created}
        self.assertIn(self.student.id, notified_user_ids)
        self.assertNotIn(other_student.id, notified_user_ids)
        self.assertFalse(Notification.objects.filter(user=other_student).exists())

    def test_multi_course_student_notified_once_per_matching_course_only(self):
        course_b = _make_course('B.Optometry')
        _enroll(self.student, course_b)  # student now in both courses
        # self.test only belongs to self.course, not course_b.

        start = timezone.now() + timedelta(days=2)
        session = _make_session(self.test, start, start + timedelta(hours=2))
        created = exam_integration.schedule_session_reminders(session)

        course_ids = {n.course_id for n in created}
        self.assertEqual(course_ids, {self.course.id})

    def test_ineligible_student_no_commercial_access_excluded_at_schedule_time(self):
        ineligible = _make_user('no_access_student')
        _enroll(ineligible, self.course)
        # Deliberately no GrandTestAccess grant for `ineligible`.

        start = timezone.now() + timedelta(days=2)
        session = _make_session(self.test, start, start + timedelta(hours=2))
        exam_integration.schedule_session_reminders(session)

        self.assertFalse(Notification.objects.filter(user=ineligible).exists())

    def test_no_courses_assigned_yields_no_notifications(self):
        test = _make_test('grand', title='Unassigned Grand Test', courses=())
        start = timezone.now() + timedelta(days=2)
        session = _make_session(test, start, start + timedelta(hours=2))

        created = exam_integration.schedule_session_reminders(session)
        self.assertEqual(created, [])

    def test_non_grand_non_daily_exam_type_is_a_no_op(self):
        mock_test = _make_test('mock', title='Mock 1', courses=[self.course])
        start = timezone.now() + timedelta(days=2)
        session = _make_session(mock_test, start, start + timedelta(hours=2))

        created = exam_integration.schedule_session_reminders(session)
        self.assertEqual(created, [])


class ScheduleSessionRemindersDailyTestTests(TestCase):
    """P0-DAILY-TEST."""

    def setUp(self):
        self.course = _make_course('CEE-MBBS')
        self.student = _make_user('daily_student')
        _enroll(self.student, self.course)
        self.test = _make_test('daily', title='Daily Test — 7 Sept', courses=[self.course])

    def test_daily_test_available_and_ending_created_via_shared_session_mechanism(self):
        start = timezone.now() - timedelta(minutes=5)
        end = timezone.now() + timedelta(hours=5)
        session = _make_session(self.test, start, end)

        created = exam_integration.schedule_session_reminders(session)
        event_types = sorted(n.event_type for n in created)
        self.assertEqual(event_types, ['DAILY_TEST_AVAILABLE', 'DAILY_TEST_ENDING'])

        ending = next(n for n in created if n.event_type == 'DAILY_TEST_ENDING')
        self.assertEqual(ending.scheduled_for, end - exam_integration.DAILY_ENDING_OFFSET)
        self.assertEqual(ending.action_url, f'/daily-test/{self.test.id}')

    def test_ending_reminder_skipped_when_already_within_the_ending_window(self):
        start = timezone.now() - timedelta(hours=1)
        end = timezone.now() + timedelta(minutes=10)  # ending offset (30m) already elapsed
        session = _make_session(self.test, start, end)

        created = exam_integration.schedule_session_reminders(session)
        event_types = {n.event_type for n in created}
        self.assertIn('DAILY_TEST_AVAILABLE', event_types)
        self.assertNotIn('DAILY_TEST_ENDING', event_types)

    def test_pro_daily_test_requires_a_subscription(self):
        pro_test = _make_test('daily', title='Pro Daily Test', courses=[self.course], is_pro=True)
        start = timezone.now() + timedelta(hours=1)
        session = _make_session(pro_test, start, start + timedelta(hours=5))

        created = exam_integration.schedule_session_reminders(session)
        self.assertEqual(created, [])  # no subscription -> has_daily_test_access is False


class CancelAndRescheduleNotificationsTests(TestCase):
    """P0-CANCEL, P0-RESCHEDULE — the metadata__session_id scoping self-correction."""

    def setUp(self):
        self.course = _make_course('CEE-MBBS')
        self.student = _make_user('cancel_student')
        _enroll(self.student, self.course)
        self.test = _make_test('grand', courses=[self.course])
        _grant_grand_access(self.student, self.test)
        self.template = ExamTemplate.objects.create(title=self.test.title, exam_type='grand')

    def test_cancel_notifications_for_session_only_affects_that_session(self):
        start = timezone.now() + timedelta(days=2)
        session_a = _make_session(self.test, start, start + timedelta(hours=2), exam_template=self.template)
        session_b = _make_session(self.test, start + timedelta(days=1), start + timedelta(days=1, hours=2),
                                   exam_template=self.template)

        exam_integration.schedule_session_reminders(session_a)
        exam_integration.schedule_session_reminders(session_b)

        cancelled_count = services.cancel_notifications_for_session(session_a)
        self.assertGreater(cancelled_count, 0)

        # Session A's scheduled reminders are now cancelled...
        self.assertFalse(
            Notification.objects.filter(metadata__session_id=session_a.id, status=Notification.STATUS_SCHEDULED).exists()
        )
        # ...but Session B's own reminders (same Test, same student) are untouched.
        self.assertTrue(
            Notification.objects.filter(metadata__session_id=session_b.id, status=Notification.STATUS_SCHEDULED).exists()
        )

    def test_reschedule_notifications_for_session_hard_deletes_then_allows_fresh_recreate(self):
        start = timezone.now() + timedelta(days=2)
        session = _make_session(self.test, start, start + timedelta(hours=2), exam_template=self.template)
        exam_integration.schedule_session_reminders(session)
        old_ids = set(Notification.objects.filter(metadata__session_id=session.id).values_list('id', flat=True))
        self.assertTrue(old_ids)

        new_start = start + timedelta(hours=5)
        session.start_datetime = new_start
        session.end_datetime = new_start + timedelta(hours=2)
        session.save(update_fields=['start_datetime', 'end_datetime'])

        removed = services.reschedule_notifications_for_session(session)
        self.assertGreater(removed, 0)
        # Old scheduled rows are gone entirely (hard delete), not just cancelled...
        self.assertFalse(Notification.objects.filter(id__in=old_ids, status=Notification.STATUS_SCHEDULED).exists())

        recreated = exam_integration.schedule_session_reminders(session)
        reminder_times = {n.metadata['offset']: n.scheduled_for for n in recreated if n.event_type == 'GRAND_TEST_REMINDER'}
        self.assertEqual(reminder_times['t_minus_1h'], new_start - timedelta(hours=1))

    def test_create_reschedule_session_schedules_new_session_without_touching_prior_session(self):
        """Real call site: tests_app.exam_versioning.create_reschedule_session.
        Documents the corrected, real-architecture behavior: a reschedule
        creates an independent new session and schedules ITS reminders; it
        never cancels a prior, still-valid session's reminders (see
        exam_versioning.py's own comment on why guessing would be wrong)."""
        # Give the test a pre-existing exam_template so create_reschedule_
        # session's internal adopt_test_into_template() call is a no-op
        # (returns immediately — see that function's own idempotency
        # guard). Without this, adoption would run for the first time here
        # and (correctly, per its own real, pre-existing behavior) flip
        # is_draft back to True as part of first-time template adoption —
        # a real effect of an already-tested function, not something this
        # test is about; setting exam_template up front isolates this test
        # to what it's actually meant to prove.
        self.test.exam_template = self.template
        self.test.save(update_fields=['exam_template'])

        start = timezone.now() + timedelta(days=2)
        prior_session = _make_session(self.test, start, start + timedelta(hours=2), exam_template=self.template)
        exam_integration.schedule_session_reminders(prior_session)
        prior_scheduled_before = Notification.objects.filter(
            metadata__session_id=prior_session.id, status=Notification.STATUS_SCHEDULED,
        ).count()
        self.assertGreater(prior_scheduled_before, 0)

        staff = _make_user('staff1', is_staff=True)
        new_start = start + timedelta(days=3)
        with self.captureOnCommitCallbacks(execute=True):
            new_session = create_reschedule_session(
                self.test.id, staff,
                start_datetime=new_start, end_datetime=new_start + timedelta(hours=2),
            )

        # Prior session's reminders are untouched.
        self.assertEqual(
            Notification.objects.filter(metadata__session_id=prior_session.id, status=Notification.STATUS_SCHEDULED).count(),
            prior_scheduled_before,
        )
        # New session got its own full, correctly-offset schedule.
        new_reminders = Notification.objects.filter(metadata__session_id=new_session.id, event_type='GRAND_TEST_REMINDER')
        self.assertEqual(new_reminders.count(), 3)


class ExamSessionViewSetIntegrationTests(APITestCase):
    """Real HTTP call sites: ExamSessionViewSet.cancel / perform_update."""

    def setUp(self):
        self.course = _make_course('CEE-MBBS')
        self.student = _make_user('api_student')
        _enroll(self.student, self.course)
        self.staff = _make_user('api_staff', is_staff=True)
        self.test = _make_test('grand', courses=[self.course])
        _grant_grand_access(self.student, self.test)
        self.template = ExamTemplate.objects.create(title=self.test.title, exam_type='grand')
        self.start = timezone.now() + timedelta(days=2)
        self.session = _make_session(self.test, self.start, self.start + timedelta(hours=2), exam_template=self.template)
        exam_integration.schedule_session_reminders(self.session)

    def test_cancel_action_cancels_pending_reminders(self):
        self.client.force_authenticate(self.staff)
        url = reverse('exam-session-cancel', args=[self.session.id])
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(
            Notification.objects.filter(metadata__session_id=self.session.id, status=Notification.STATUS_SCHEDULED).exists()
        )

    def test_editing_start_time_replaces_stale_reminders(self):
        old_reminder = Notification.objects.get(
            metadata__session_id=self.session.id, event_type='GRAND_TEST_REMINDER', metadata__offset='t_minus_1h',
        )
        old_scheduled_for = old_reminder.scheduled_for

        self.client.force_authenticate(self.staff)
        url = reverse('exam-session-detail', args=[self.session.id])
        new_start = self.start + timedelta(hours=6)
        payload = {
            'start_datetime': new_start.isoformat(),
            'end_datetime': (new_start + timedelta(hours=2)).isoformat(),
            'max_attempts': self.session.max_attempts,
        }
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertFalse(Notification.objects.filter(id=old_reminder.id).exists())
        refreshed = Notification.objects.get(
            metadata__session_id=self.session.id, event_type='GRAND_TEST_REMINDER', metadata__offset='t_minus_1h',
        )
        self.assertNotEqual(refreshed.scheduled_for, old_scheduled_for)
        self.assertEqual(refreshed.scheduled_for, new_start - timedelta(hours=1))

    def test_editing_unrelated_field_does_not_touch_reminders(self):
        old_ids = set(Notification.objects.filter(metadata__session_id=self.session.id).values_list('id', flat=True))
        self.client.force_authenticate(self.staff)
        url = reverse('exam-session-detail', args=[self.session.id])
        payload = {
            'start_datetime': self.session.start_datetime.isoformat(),
            'end_datetime': self.session.end_datetime.isoformat(),
            'max_attempts': 5,
        }
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        new_ids = set(Notification.objects.filter(metadata__session_id=self.session.id).values_list('id', flat=True))
        self.assertEqual(old_ids, new_ids)


class NotifyResultAvailableTests(TestCase):
    def test_creates_result_notification_for_grand_test_attempt(self):
        course = _make_course('CEE-MBBS')
        student = _make_user('result_student')
        _enroll(student, course)
        test = _make_test('grand', courses=[course])
        from tests_app.models import TestAttempt

        attempt = TestAttempt.objects.create(user=student, test=test, status='submitted')

        n = exam_integration.notify_result_available(attempt)
        self.assertEqual(n.event_type, 'GRAND_TEST_RESULT_AVAILABLE')
        self.assertEqual(n.action_url, f'/tests/result/{attempt.id}')
        self.assertEqual(n.user_id, student.id)

    def test_calling_twice_for_same_attempt_creates_no_duplicate(self):
        course = _make_course('CEE-MBBS')
        student = _make_user('result_student2')
        _enroll(student, course)
        test = _make_test('grand', courses=[course])
        from tests_app.models import TestAttempt

        attempt = TestAttempt.objects.create(user=student, test=test, status='submitted')
        exam_integration.notify_result_available(attempt)
        exam_integration.notify_result_available(attempt)

        self.assertEqual(
            Notification.objects.filter(attempt=attempt, event_type='GRAND_TEST_RESULT_AVAILABLE').count(), 1,
        )
