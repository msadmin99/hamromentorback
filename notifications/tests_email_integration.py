"""Phase 4 — integration tests driving the REAL event-producer path (never
calling create_notification/email_tasks directly for these), per this
project's own established convention: Announcement -> signal, payment
approval -> billing_integration, Grand/Daily Test -> exam_integration.
Only `email_adapter.send` is mocked."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from billing import payment_service
from billing.models import GrandTestAccess
from billing.tests import BillingTestCase
from core.models import Announcement
from courses.models import Course, Enrollment
from tests_app.models import ExamSession, ExamTemplate, Test

from . import email_tasks
from .email_adapter import EmailSendResult
from .models import Notification, NotificationDelivery

User = get_user_model()


def _make_user(username):
    return User.objects.create_user(username=username, email=f'{username}@example.com', password='pw12345')


def _make_course(name):
    return Course.objects.create(name=name, prefix=name[:10])


class AnnouncementEmailIntegrationTests(TestCase):
    def test_real_announcement_creates_email_deliveries_for_every_active_student(self):
        student_a = _make_user('ann_email_a')
        student_b = _make_user('ann_email_b')

        with patch('notifications.email_adapter.send') as mock_send:
            mock_send.return_value = EmailSendResult(outcome=EmailSendResult.OUTCOME_SENT)
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                Announcement.objects.create(message='Real announcement for email test')

        self.assertTrue(len(callbacks) >= 2)
        for user in (student_a, student_b):
            delivery = NotificationDelivery.objects.get(notification__user=user, notification__event_type='ANNOUNCEMENT', channel='email')
            self.assertEqual(delivery.status, NotificationDelivery.STATUS_SENT)


class PaymentApprovedEmailIntegrationTests(BillingTestCase):
    def test_real_payment_approval_creates_a_sent_email_delivery(self):
        purchase = self._create_purchase()
        self._submit(purchase['id'], reference='TXN-EMAIL-1')

        with patch('notifications.email_adapter.send') as mock_send:
            mock_send.return_value = EmailSendResult(outcome=EmailSendResult.OUTCOME_SENT)
            with self.captureOnCommitCallbacks(execute=True):
                payment_service.activate(purchase['id'], actor=self.staff)
        delivery = NotificationDelivery.objects.get(
            notification__purchase_id=purchase['id'], notification__event_type='PAYMENT_APPROVED', channel='email',
        )
        self.assertEqual(delivery.status, NotificationDelivery.STATUS_SENT)

    def test_subscription_activated_also_gets_a_real_email_delivery(self):
        purchase = self._create_purchase()
        self._submit(purchase['id'], reference='TXN-EMAIL-2')

        with patch('notifications.email_adapter.send') as mock_send:
            mock_send.return_value = EmailSendResult(outcome=EmailSendResult.OUTCOME_SENT)
            with self.captureOnCommitCallbacks(execute=True):
                payment_service.activate(purchase['id'], actor=self.staff)

        self.assertTrue(
            NotificationDelivery.objects.filter(
                notification__purchase_id=purchase['id'], notification__event_type='SUBSCRIPTION_ACTIVATED', channel='email',
            ).exists()
        )


class ExamEventEmailIntegrationTests(TestCase):
    def setUp(self):
        self.course = _make_course('CEE-MBBS')
        self.student = _make_user('exam_email_student')
        Enrollment.objects.create(user=self.student, course=self.course)

    def test_grand_test_reminder_creates_a_real_email_delivery(self):
        test = Test.objects.create(title='Grand Test I', exam_type='grand', is_draft=False)
        test.courses.set([self.course])
        GrandTestAccess.objects.create(user=self.student, test=test)
        template = ExamTemplate.objects.create(title=test.title, exam_type='grand')

        with self.captureOnCommitCallbacks(execute=True):
            with patch('notifications.email_adapter.send') as mock_send:
                mock_send.return_value = EmailSendResult(outcome=EmailSendResult.OUTCOME_SENT)
                session = ExamSession.objects.create(
                    exam_template=template, exam_version=test, session_name='Session 1',
                    start_datetime=timezone.now() + timezone.timedelta(days=2),
                    end_datetime=timezone.now() + timezone.timedelta(days=2, hours=2),
                    status='scheduled',
                )
                from notifications.exam_integration import schedule_session_reminders
                schedule_session_reminders(session)

        self.assertTrue(
            NotificationDelivery.objects.filter(
                notification__user=self.student, notification__event_type='GRAND_TEST_SCHEDULED', channel='email',
            ).exists()
        )

    def test_daily_test_available_creates_a_real_email_delivery(self):
        test = Test.objects.create(title='Daily Test — Today', exam_type='daily', is_draft=False)
        test.courses.set([self.course])
        template = ExamTemplate.objects.create(title=test.title, exam_type='daily')

        with self.captureOnCommitCallbacks(execute=True):
            with patch('notifications.email_adapter.send') as mock_send:
                mock_send.return_value = EmailSendResult(outcome=EmailSendResult.OUTCOME_SENT)
                session = ExamSession.objects.create(
                    exam_template=template, exam_version=test, session_name='Session 1',
                    start_datetime=timezone.now() - timezone.timedelta(minutes=5),
                    end_datetime=timezone.now() + timezone.timedelta(hours=5),
                    status='scheduled',
                )
                from notifications.exam_integration import schedule_session_reminders
                schedule_session_reminders(session)

        self.assertTrue(
            NotificationDelivery.objects.filter(
                notification__user=self.student, notification__event_type='DAILY_TEST_AVAILABLE', channel='email',
            ).exists()
        )

    def test_course_isolation_still_holds_for_email_zero_new_scoping_logic(self):
        """P0-9/§34 — academic email must remain course-aware, inherited
        from Phase 2's own audience resolution, never re-derived here."""
        other_course = _make_course('BDS')
        other_student = _make_user('exam_email_other')
        Enrollment.objects.create(user=other_student, course=other_course)

        test = Test.objects.create(title='Grand Test Isolated', exam_type='grand', is_draft=False)
        test.courses.set([self.course])
        GrandTestAccess.objects.create(user=self.student, test=test)
        template = ExamTemplate.objects.create(title=test.title, exam_type='grand')

        with self.captureOnCommitCallbacks(execute=True):
            with patch('notifications.email_adapter.send') as mock_send:
                mock_send.return_value = EmailSendResult(outcome=EmailSendResult.OUTCOME_SENT)
                session = ExamSession.objects.create(
                    exam_template=template, exam_version=test, session_name='Session 1',
                    start_datetime=timezone.now() + timezone.timedelta(days=2),
                    end_datetime=timezone.now() + timezone.timedelta(days=2, hours=2),
                    status='scheduled',
                )
                from notifications.exam_integration import schedule_session_reminders
                schedule_session_reminders(session)

        self.assertTrue(NotificationDelivery.objects.filter(notification__user=self.student, channel='email').exists())
        self.assertFalse(Notification.objects.filter(user=other_student).exists())


class CloudTaskCallbackIntegrationTests(TestCase):
    """The real HTTP callback view, not the bare function — proves the
    secret check and the full request/response cycle."""

    def setUp(self):
        self.user = _make_user('callback_email_student')
        self.notification = Notification.objects.create(
            user=self.user, event_type='ANNOUNCEMENT', category='announcements', title='Hi', body='Body',
        )
        self.delivery = NotificationDelivery.objects.create(notification=self.notification, channel='email')

    def test_missing_secret_rejected(self):
        response = self.client.post('/api/notifications/email/process/', {'delivery_id': self.delivery.id}, content_type='application/json')
        self.assertEqual(response.status_code, 401)

    def test_wrong_secret_rejected(self):
        response = self.client.post(
            '/api/notifications/email/process/', {'delivery_id': self.delivery.id},
            content_type='application/json', HTTP_X_EMAIL_PROCESSING_SECRET='wrong',
        )
        self.assertEqual(response.status_code, 401)

    def test_correct_secret_processes_the_real_delivery(self):
        with patch('notifications.email_tasks.email_adapter.send') as mock_send:
            mock_send.return_value = EmailSendResult(outcome=EmailSendResult.OUTCOME_SENT)
            response = self.client.post(
                '/api/notifications/email/process/', {'delivery_id': self.delivery.id},
                content_type='application/json', HTTP_X_EMAIL_PROCESSING_SECRET='dev-email-secret-change-me',
            )
        self.assertEqual(response.status_code, 200)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, NotificationDelivery.STATUS_SENT)

    def test_temporary_failure_returns_500_signaling_cloud_tasks_to_retry(self):
        with patch('notifications.email_tasks.email_adapter.send') as mock_send:
            mock_send.return_value = EmailSendResult(outcome=EmailSendResult.OUTCOME_TEMPORARY_FAILURE, error_code='timeout')
            response = self.client.post(
                '/api/notifications/email/process/', {'delivery_id': self.delivery.id},
                content_type='application/json', HTTP_X_EMAIL_PROCESSING_SECRET='dev-email-secret-change-me',
            )
        self.assertEqual(response.status_code, 500)
