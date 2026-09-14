"""Phase 3 — Delivery pipeline tests: channel fan-out (P0-06/§12 of the
brief), async/transaction safety (P0-08/P0-12), retry classification
(P0-09), permanent failure invalidation (P0-10), multi-device delivery
(P0-03/§20), deep-link payload (P0-11), and course isolation reuse
(P0-07).

Only `webpush_adapter.send` is ever mocked — the one genuinely external
call in this phase (matching `billing/tests.py`'s own established
convention of mocking only real external I/O, never the notification
logic itself)."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from billing.models import GrandTestAccess
from courses.models import Course, Enrollment
from tests_app.models import Test

from . import push_service, services
from .models import Notification, NotificationDelivery, NotificationPreference, PushDeliveryAttempt, PushSubscription
from .webpush_adapter import PushSendResult

User = get_user_model()


def _make_user(username):
    return User.objects.create_user(username=username, email=f'{username}@example.com', password='pw12345')


def _make_course(name):
    return Course.objects.create(name=name, prefix=name[:10])


def _subscribe(user, endpoint):
    return push_service.register_subscription(user, endpoint=endpoint, p256dh='p', auth='a')


class ChannelFanOutTests(TestCase):
    """Mandatory regression per the Phase 3 brief §12."""

    def setUp(self):
        self.user = _make_user('fanout_student')
        _subscribe(self.user, 'https://fcm.googleapis.com/fcm/send/fanout-device')

    def test_push_enabled_creates_both_in_app_and_push_deliveries(self):
        notification = services.create_notification(self.user, 'ANNOUNCEMENT', 'Hello')
        deliveries = NotificationDelivery.objects.filter(notification=notification)
        # in_app + push + email (Phase 4 — this test user always has a
        # real `email`, so email is always a candidate channel too; see
        # notifications/services.py: _resolve_channels).
        self.assertEqual(deliveries.count(), 3)
        self.assertEqual(set(deliveries.values_list('channel', flat=True)), {'in_app', 'push', 'email'})

    def test_push_disabled_preference_creates_no_push_delivery_but_in_app_unaffected(self):
        NotificationPreference.objects.create(user=self.user, category='announcements', channel='push', course=None, enabled=False)
        notification = services.create_notification(self.user, 'ANNOUNCEMENT', 'Hello')
        deliveries = NotificationDelivery.objects.filter(notification=notification)
        channels = set(deliveries.values_list('channel', flat=True))
        self.assertIn('in_app', channels)
        push_delivery = deliveries.get(channel='push')
        self.assertEqual(push_delivery.status, NotificationDelivery.STATUS_SKIPPED)
        in_app_delivery = deliveries.get(channel='in_app')
        self.assertEqual(in_app_delivery.status, NotificationDelivery.STATUS_DELIVERED)

    def test_mandatory_category_delivers_push_regardless_of_preference(self):
        NotificationPreference.objects.create(user=self.user, category='billing', channel='push', course=None, enabled=False)
        notification = services.create_notification(self.user, 'PAYMENT_APPROVED', 'Paid', 'Your payment was approved.')
        push_delivery = NotificationDelivery.objects.get(notification=notification, channel='push')
        self.assertNotEqual(push_delivery.status, NotificationDelivery.STATUS_SKIPPED)

    def test_no_active_subscription_creates_no_push_delivery_row_at_all(self):
        no_device_user = _make_user('no_device_student')
        notification = services.create_notification(no_device_user, 'ANNOUNCEMENT', 'Hello')
        self.assertFalse(NotificationDelivery.objects.filter(notification=notification, channel='push').exists())
        # in_app + email only (Phase 4 — no push, since this user has no
        # active subscription; email is still always a candidate).
        self.assertEqual(NotificationDelivery.objects.filter(notification=notification).count(), 2)

    def test_explicit_channels_argument_bypasses_auto_resolution(self):
        notification = services.create_notification(self.user, 'ANNOUNCEMENT', 'Hello', channels=('in_app',))
        self.assertEqual(NotificationDelivery.objects.filter(notification=notification).count(), 1)
        self.assertFalse(NotificationDelivery.objects.filter(notification=notification, channel='push').exists())

    def test_revoked_subscription_does_not_count_as_active_for_fan_out(self):
        PushSubscription.objects.filter(user=self.user).update(status=PushSubscription.STATUS_REVOKED)
        notification = services.create_notification(self.user, 'ANNOUNCEMENT', 'Hello')
        self.assertFalse(NotificationDelivery.objects.filter(notification=notification, channel='push').exists())


class TransactionSafetyTests(TestCase):
    """P0-08/P0-12 — never a provider call inside an uncommitted transaction."""

    def setUp(self):
        self.user = _make_user('txn_student')
        _subscribe(self.user, 'https://fcm.googleapis.com/fcm/send/txn-device')

    @override_settings(PUSH_PROCESSING_ASYNC=False)
    def test_push_task_only_runs_after_commit(self):
        with patch('notifications.push_tasks.process_push_delivery') as mock_process:
            with transaction.atomic():
                services.create_notification(self.user, 'ANNOUNCEMENT', 'Hello')
                # Still inside the transaction — the on_commit callback must not have run yet.
                mock_process.assert_not_called()
        # Transaction committed (TestCase wraps the whole test, but this
        # nested atomic() block is a real savepoint boundary) — however
        # Django's on_commit only fires on the OUTERMOST commit, which in
        # a plain TestCase never happens. Use captureOnCommitCallbacks to
        # observe it deterministically instead.

    def test_captured_on_commit_callback_enqueues_exactly_once(self):
        with patch('notifications.push_tasks.process_push_delivery') as mock_process:
            with self.captureOnCommitCallbacks(execute=True):
                services.create_notification(self.user, 'ANNOUNCEMENT', 'Hello')
            mock_process.assert_called_once()

    def test_rolled_back_transaction_never_enqueues_push_task(self):
        class _Boom(Exception):
            pass

        with patch('notifications.push_tasks.process_push_delivery') as mock_process:
            try:
                with self.captureOnCommitCallbacks(execute=True):
                    with transaction.atomic():
                        services.create_notification(self.user, 'ANNOUNCEMENT', 'Hello')
                        raise _Boom()
            except _Boom:
                pass
            mock_process.assert_not_called()
        self.assertFalse(Notification.objects.filter(user=self.user).exists())


class RetryAndInvalidationTests(TestCase):
    """P0-09 (temporary -> retry) and P0-10 (permanent -> invalidate,
    never endlessly retried)."""

    def setUp(self):
        self.user = _make_user('retry_student')
        self.subscription = _subscribe(self.user, 'https://fcm.googleapis.com/fcm/send/retry-device')
        self.notification = Notification.objects.create(
            user=self.user, event_type='ANNOUNCEMENT', category='announcements', title='Hi',
        )
        self.delivery = NotificationDelivery.objects.create(notification=self.notification, channel='push')

    def test_temporary_failure_is_retried_not_marked_invalid(self):
        from . import push_tasks

        with patch('notifications.push_tasks.webpush_adapter.send') as mock_send:
            mock_send.return_value = PushSendResult(outcome=PushSendResult.OUTCOME_TEMPORARY_FAILURE, status_code=503)
            complete = push_tasks.process_push_delivery(self.delivery.id)

        self.assertFalse(complete)  # signals "please retry" to the Cloud Tasks callback
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, PushSubscription.STATUS_ACTIVE)
        attempt = PushDeliveryAttempt.objects.get(delivery=self.delivery, subscription=self.subscription)
        self.assertEqual(attempt.status, PushDeliveryAttempt.STATUS_QUEUED)
        self.assertEqual(attempt.attempt_count, 1)

    def test_attempt_count_increments_and_caps_at_max_attempts(self):
        from . import push_tasks

        with patch('notifications.push_tasks.webpush_adapter.send') as mock_send:
            mock_send.return_value = PushSendResult(outcome=PushSendResult.OUTCOME_TEMPORARY_FAILURE, status_code=503)
            for _ in range(push_tasks.MAX_DELIVERY_ATTEMPTS):
                push_tasks.process_push_delivery(self.delivery.id)

        attempt = PushDeliveryAttempt.objects.get(delivery=self.delivery, subscription=self.subscription)
        self.assertEqual(attempt.attempt_count, push_tasks.MAX_DELIVERY_ATTEMPTS)
        self.assertEqual(attempt.status, PushDeliveryAttempt.STATUS_FAILED)
        # Even after capping, the subscription itself is NOT marked invalid —
        # a run of 5xx failures doesn't mean the browser subscription is
        # bad, only that this notification's delivery gave up (P0-10 is
        # specifically about PERMANENT provider signals, not attempt exhaustion).
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, PushSubscription.STATUS_ACTIVE)

    def test_permanent_failure_marks_subscription_invalid_immediately(self):
        from . import push_tasks

        with patch('notifications.push_tasks.webpush_adapter.send') as mock_send:
            mock_send.return_value = PushSendResult(outcome=PushSendResult.OUTCOME_PERMANENT_FAILURE, status_code=410, error_code='http_410')
            complete = push_tasks.process_push_delivery(self.delivery.id)

        self.assertTrue(complete)  # nothing left to retry
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, PushSubscription.STATUS_INVALID)
        attempt = PushDeliveryAttempt.objects.get(delivery=self.delivery, subscription=self.subscription)
        self.assertEqual(attempt.status, PushDeliveryAttempt.STATUS_FAILED)
        self.assertEqual(attempt.provider_status_code, 410)

    def test_invalid_subscription_excluded_from_future_delivery_without_further_retries(self):
        from . import push_tasks

        with patch('notifications.push_tasks.webpush_adapter.send') as mock_send:
            mock_send.return_value = PushSendResult(outcome=PushSendResult.OUTCOME_PERMANENT_FAILURE, status_code=404)
            push_tasks.process_push_delivery(self.delivery.id)

        # A brand new notification for the same user resolves zero active
        # subscriptions — no push delivery row is even created, so nothing
        # ever retries the now-invalid device again.
        notification2 = services.create_notification(self.user, 'ANNOUNCEMENT', 'Again')
        self.assertFalse(NotificationDelivery.objects.filter(notification=notification2, channel='push').exists())

    def test_retry_is_idempotent_does_not_resend_to_already_succeeded_device(self):
        from . import push_tasks

        second_subscription = _subscribe(self.user, 'https://fcm.googleapis.com/fcm/send/retry-device-2')
        # self.subscription always fails (simulating the one flaky device);
        # second_subscription always succeeds — keyed by identity, not by
        # queryset iteration order (PushSubscription.Meta.ordering is
        # -last_seen_at, so which one comes "first" isn't something this
        # test should assume).
        failing_id = self.subscription.id

        def _side_effect(subscription, payload):
            if subscription.id == failing_id:
                return PushSendResult(outcome=PushSendResult.OUTCOME_TEMPORARY_FAILURE, status_code=503)
            return PushSendResult(outcome=PushSendResult.OUTCOME_SENT)

        with patch('notifications.push_tasks.webpush_adapter.send') as mock_send:
            mock_send.side_effect = _side_effect
            complete = push_tasks.process_push_delivery(self.delivery.id)
        self.assertFalse(complete)
        self.assertEqual(mock_send.call_count, 2)

        # Simulated Cloud Tasks retry of the SAME delivery.
        with patch('notifications.push_tasks.webpush_adapter.send') as mock_send:
            mock_send.return_value = PushSendResult(outcome=PushSendResult.OUTCOME_SENT)
            push_tasks.process_push_delivery(self.delivery.id)
            # Only the device that hadn't yet succeeded should be attempted this run.
            self.assertEqual(mock_send.call_count, 1)
            self.assertEqual(mock_send.call_args[0][0].id, failing_id)


class MultiDeviceDeliveryTests(TestCase):
    """P0-03/§20 — one invalid device must never stop delivery to others."""

    def setUp(self):
        self.user = _make_user('multi_device_student')
        self.mac = _subscribe(self.user, 'https://fcm.googleapis.com/fcm/send/mac')
        self.android = _subscribe(self.user, 'https://fcm.googleapis.com/fcm/send/android')
        self.stale = _subscribe(self.user, 'https://fcm.googleapis.com/fcm/send/stale')
        self.stale.status = PushSubscription.STATUS_INVALID
        self.stale.save(update_fields=['status'])

        self.notification = Notification.objects.create(
            user=self.user, event_type='ANNOUNCEMENT', category='announcements', title='Hi',
        )
        self.delivery = NotificationDelivery.objects.create(notification=self.notification, channel='push')

    def test_all_active_subscriptions_attempted_stale_one_skipped(self):
        from . import push_tasks

        with patch('notifications.push_tasks.webpush_adapter.send') as mock_send:
            mock_send.return_value = PushSendResult(outcome=PushSendResult.OUTCOME_SENT)
            push_tasks.process_push_delivery(self.delivery.id)

        attempted_subscription_ids = {call.args[0].id for call in mock_send.call_args_list}
        self.assertEqual(attempted_subscription_ids, {self.mac.id, self.android.id})
        self.assertFalse(PushDeliveryAttempt.objects.filter(subscription=self.stale).exists())

    def test_one_device_failing_does_not_stop_delivery_to_another(self):
        from . import push_tasks

        with patch('notifications.push_tasks.webpush_adapter.send') as mock_send:
            def _side_effect(subscription, payload):
                if subscription.id == self.mac.id:
                    return PushSendResult(outcome=PushSendResult.OUTCOME_PERMANENT_FAILURE, status_code=410)
                return PushSendResult(outcome=PushSendResult.OUTCOME_SENT)
            mock_send.side_effect = _side_effect

            push_tasks.process_push_delivery(self.delivery.id)

        self.mac.refresh_from_db()
        self.android.refresh_from_db()
        self.assertEqual(self.mac.status, PushSubscription.STATUS_INVALID)
        self.assertEqual(self.android.status, PushSubscription.STATUS_ACTIVE)
        android_attempt = PushDeliveryAttempt.objects.get(delivery=self.delivery, subscription=self.android)
        self.assertEqual(android_attempt.status, PushDeliveryAttempt.STATUS_SENT)


class DeepLinkPayloadTests(TestCase):
    """P0-11 — payload always carries the notification's own action_url verbatim."""

    def test_payload_uses_notification_action_url_verbatim(self):
        from .push_tasks import _build_payload

        course = _make_course('CEE-MBBS')
        user = _make_user('deeplink_student')
        notification = Notification.objects.create(
            user=user, course=course, event_type='GRAND_TEST_REMINDER', category='tests',
            title='Reminder', body='Starts soon', action_url='/grand-test/82',
        )
        payload = _build_payload(notification)
        self.assertEqual(payload['action_url'], '/grand-test/82')
        self.assertEqual(payload['notification_id'], notification.id)
        self.assertEqual(payload['course'], {'id': course.id, 'name': course.name})

    def test_payload_omits_course_key_for_a_global_notification(self):
        from .push_tasks import _build_payload

        user = _make_user('deeplink_student2')
        notification = Notification.objects.create(
            user=user, event_type='ANNOUNCEMENT', category='announcements', title='Hi', action_url='',
        )
        payload = _build_payload(notification)
        self.assertNotIn('course', payload)


class CourseIsolationReuseTests(TestCase):
    """P0-07 — push inherits Phase 2's own audience resolution; no new
    course-scoping logic exists in the push layer itself."""

    def test_grand_test_reminder_only_creates_push_delivery_for_enrolled_subscribed_student(self):
        from . import exam_integration
        from tests_app.models import ExamSession, ExamTemplate

        course = _make_course('CEE-MBBS')
        other_course = _make_course('BDS')
        enrolled_student = _make_user('course_iso_enrolled')
        other_student = _make_user('course_iso_other')
        Enrollment.objects.create(user=enrolled_student, course=course)
        Enrollment.objects.create(user=other_student, course=other_course)
        _subscribe(enrolled_student, 'https://fcm.googleapis.com/fcm/send/enrolled-device')
        _subscribe(other_student, 'https://fcm.googleapis.com/fcm/send/other-device')

        test = Test.objects.create(title='Grand Test', exam_type='grand', is_draft=False)
        test.courses.set([course])
        GrandTestAccess.objects.create(user=enrolled_student, test=test)

        template = ExamTemplate.objects.create(title=test.title, exam_type='grand')
        session = ExamSession.objects.create(
            exam_template=template, exam_version=test, session_name='Session 1',
            start_datetime=timezone.now() + timezone.timedelta(days=2),
            end_datetime=timezone.now() + timezone.timedelta(days=2, hours=2),
            status='scheduled',
        )
        exam_integration.schedule_session_reminders(session)

        self.assertTrue(
            NotificationDelivery.objects.filter(notification__user=enrolled_student, channel='push').exists()
        )
        self.assertFalse(
            NotificationDelivery.objects.filter(notification__user=other_student, channel='push').exists()
        )
