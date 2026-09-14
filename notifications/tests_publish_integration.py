"""Phase 2 — 'Publish' (is_draft True -> False) integration tests.

Daily/Grand Test publish is a plain DRF PATCH of is_draft (confirmed real
behavior, tests_app/views.py's own comment) — TestViewSet.perform_update()
is the additive hook. Covers the real gap this closes: a session created
while the Test is still draft produces zero notifications at creation
time (can_access_test denies every non-staff/creator student), and only
publishing the Test actually makes it visible — so publish must be the
point that (re-)runs scheduling for that session's audience.
"""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from billing.models import GrandTestAccess
from courses.models import Course, Enrollment
from tests_app.models import ExamSession, ExamTemplate, Test

from . import exam_integration
from .models import Notification
from .tests_exam_integration import _enroll, _grant_grand_access, _make_course, _make_session, _make_user


class PublishTriggersSchedulingTests(APITestCase):
    def setUp(self):
        self.course = _make_course('CEE-MBBS')
        self.student = _make_user('publish_student')
        _enroll(self.student, self.course)
        self.staff = _make_user('publish_staff', is_staff=True)

    def test_daily_test_session_created_while_draft_notifies_no_one_until_published(self):
        test = Test.objects.create(title='Daily Test — Draft', exam_type='daily', is_draft=True)
        test.courses.set([self.course])
        start = timezone.now() - timedelta(minutes=5)
        session = _make_session(test, start, start + timedelta(hours=5))

        created = exam_integration.schedule_session_reminders(session)
        self.assertEqual(created, [])  # is_draft=True -> can_access_test denies everyone

        self.client.force_authenticate(self.staff)
        url = reverse('test-detail', args=[test.id])
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(url, {'is_draft': False}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.assertTrue(
            Notification.objects.filter(user=self.student, event_type='DAILY_TEST_AVAILABLE').exists()
        )

    def test_publishing_an_already_notified_session_does_not_duplicate(self):
        test = Test.objects.create(title='Daily Test — Already Live', exam_type='daily', is_draft=False)
        test.courses.set([self.course])
        start = timezone.now() - timedelta(minutes=5)
        session = _make_session(test, start, start + timedelta(hours=5))
        exam_integration.schedule_session_reminders(session)
        self.assertEqual(
            Notification.objects.filter(user=self.student, event_type='DAILY_TEST_AVAILABLE').count(), 1,
        )

        # Publish flips is_draft False->False (a no-op transition) via an
        # unrelated field edit — must not create a second notification.
        self.client.force_authenticate(self.staff)
        url = reverse('test-detail', args=[test.id])
        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(url, {'title': 'Daily Test — Renamed'}, format='json')

        self.assertEqual(
            Notification.objects.filter(user=self.student, event_type='DAILY_TEST_AVAILABLE').count(), 1,
        )

    def test_publishing_grand_test_with_cancelled_session_does_not_notify_for_it(self):
        test = Test.objects.create(title='Grand Test — Draft', exam_type='grand', is_draft=True)
        test.courses.set([self.course])
        _grant_grand_access(self.student, test)
        start = timezone.now() + timedelta(days=2)
        session = _make_session(test, start, start + timedelta(hours=2))
        session.status = 'cancelled'
        session.save(update_fields=['status'])

        self.client.force_authenticate(self.staff)
        url = reverse('test-detail', args=[test.id])
        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(url, {'is_draft': False}, format='json')

        self.assertFalse(
            Notification.objects.filter(user=self.student, metadata__session_id=session.id).exists()
        )
