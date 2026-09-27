"""Subscription/purchase notification lifecycle (Phase 2b) — additive
wiring of the real Purchase/Subscription state transitions
(billing.payment_service.submit/activate/reject/cancel, and billing's
existing SendRenewalRemindersView cron sweep) into the modern
Notification/NotificationDelivery pipeline. See notifications/
billing_integration.py's own module docstring for the exact mapping and
why several requested "events" (PURCHASE_CREATED, PAYMENT_FAILED,
'deactivated', 'grace_period') are deliberately NOT wired here — no
matching real, distinct state transition exists for them.

Only `notifications.email_adapter.send` is ever mocked, and only where an
EMAIL delivery is actually being asserted — everything else exercises the
real service-layer call path (payment_service.*, the real cron view),
matching this project's own established convention (see notifications/
tests_email_integration.py's module docstring)."""
import io
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from billing import payment_service
from billing.models import PaymentMethod, Purchase, Subscription, SubscriptionPlan
from billing.notifications import _send_email  # noqa: F401 (documents the legacy send this module runs alongside, not called directly here)
from courses.models import Course
from notifications.email_adapter import EmailSendResult
from notifications.models import Notification, NotificationDelivery

User = get_user_model()

SENT = EmailSendResult(outcome=EmailSendResult.OUTCOME_SENT, provider_message_id='msg-1')


def _user(username, **kw):
    kw.setdefault('email_verified', True)
    return User.objects.create_user(username=username, email=f'{username}@example.com', password='pw12345', **kw)


def _course(name):
    return Course.objects.create(name=name, prefix=name[:10])


def _plan(course, **overrides):
    defaults = dict(course=course, product_type='qbank', name='3 Month QBank', price=500)
    defaults.update(overrides)
    return SubscriptionPlan.objects.create(**defaults)


def _method():
    return PaymentMethod.objects.create(name='Fonepay', provider_type='fonepay')


def _valid_screenshot(name='proof.png'):
    buf = io.BytesIO()
    Image.new('RGB', (2, 2), color='white').save(buf, format='PNG')
    buf.seek(0)
    return SimpleUploadedFile(name, buf.read(), content_type='image/png')


def _purchase(user, plan, status='pending', **overrides):
    defaults = dict(
        user=user, kind='subscription', plan=plan, currency='NRS',
        original_amount=plan.price, discount_amount=0, final_amount=plan.price,
        status=status,
    )
    defaults.update(overrides)
    return Purchase.objects.create(**defaults)


class PaymentPendingNotificationTests(TestCase):
    """A. Purchase initiated / awaiting verification — payment_service.submit()."""

    def setUp(self):
        self.course = _course('PendingCourse')
        self.plan = _plan(self.course)
        self.method = _method()
        self.student = _user('pending_student')
        self.purchase = _purchase(self.student, self.plan, status='unpaid')

    def _submit(self):
        with patch('billing.screenshot_storage.upload_bytes'):
            with self.captureOnCommitCallbacks(execute=True):
                return payment_service.submit(
                    self.purchase.id, payment_method=self.method, payment_reference='TXN-PEND-1',
                    screenshot_file=_valid_screenshot(),
                )

    def test_submit_creates_payment_pending_notification_for_the_correct_user(self):
        self._submit()
        notification = Notification.objects.get(event_type='PAYMENT_PENDING', purchase=self.purchase)
        self.assertEqual(notification.user_id, self.student.id)
        self.assertEqual(notification.course_id, self.course.id)

    def test_no_other_user_receives_it(self):
        stranger = _user('pending_stranger')
        self._submit()
        self.assertFalse(Notification.objects.filter(event_type='PAYMENT_PENDING', user=stranger).exists())

    def test_retrying_submit_after_resubmission_request_does_not_duplicate(self):
        """submit() can run twice for the same purchase (resubmission_requested -> pending again)
        — the dedupe_key is purchase-scoped, not call-count-scoped, so a
        second submit correctly does NOT create a second PAYMENT_PENDING
        (the earlier one already told the student their proof is in review;
        this call reuses the exact same dedupe_key by design)."""
        self._submit()
        payment_service.request_resubmission(self.purchase.id, 'Screenshot unreadable')
        with patch('billing.screenshot_storage.upload_bytes'):
            with self.captureOnCommitCallbacks(execute=True):
                payment_service.submit(
                    self.purchase.id, payment_method=self.method, payment_reference='TXN-PEND-2',
                    screenshot_file=_valid_screenshot('proof2.png'),
                )
        self.assertEqual(Notification.objects.filter(event_type='PAYMENT_PENDING', purchase=self.purchase).count(), 1)


class PaymentApprovedAndSubscriptionActivatedTests(TestCase):
    """B/C/D — payment success and subscription approval are the same real
    transition (Purchase 'pending' -> 'approved') in this codebase.

    Product decision (subscription-notification-model follow-up): for a
    course subscription/combo purchase, that transition fires ONLY
    SUBSCRIPTION_ACTIVATED — never also the generic PAYMENT_APPROVED (one
    meaningful event, one notification). PAYMENT_APPROVED remains the
    right, sole notification for every OTHER purchase kind (grand_test,
    teacher_course, grand_test_package, and a course-less "build your own"
    combo), which has no activation concept of its own — see
    test_grand_test_purchase_never_gets_a_subscription_activated_notification
    below."""

    def setUp(self):
        self.course = _course('ApproveCourse')
        self.plan = _plan(self.course)
        self.student = _user('approve_student')
        self.purchase = _purchase(self.student, self.plan, status='pending', payment_reference='TXN-APR-1')

    def _activate(self):
        with self.captureOnCommitCallbacks(execute=True):
            return payment_service.activate(self.purchase.id, actor=None)

    def test_subscription_activation_creates_subscription_activated_only(self):
        """Product decision: ONE meaningful event -> ONE notification — a
        subscription/combo activation must never ALSO fire the generic
        PAYMENT_APPROVED (the student doesn't need two notifications for
        the same successful activation)."""
        self._activate()
        self.assertEqual(Notification.objects.filter(event_type='SUBSCRIPTION_ACTIVATED', purchase=self.purchase).count(), 1)
        self.assertFalse(Notification.objects.filter(event_type='PAYMENT_APPROVED', purchase=self.purchase).exists())

    def test_retrying_activation_does_not_duplicate_the_activation_notification(self):
        """activate() itself only ever runs once per purchase (its own
        status guard raises PaymentError on a second call) — proves the
        dedupe_key still holds even if create_notification were somehow
        invoked twice for the same purchase (e.g. a future retried task)."""
        from notifications import services

        self._activate()
        from notifications.billing_integration import _course_and_item_for_purchase

        course, _item = _course_and_item_for_purchase(self.purchase)
        title, body = ('dup', 'dup')
        services.create_notification(
            self.student, 'SUBSCRIPTION_ACTIVATED', title, body,
            course=course, purchase=self.purchase,
            dedupe_key=f'purchase:{self.purchase.id}:subscription_activated',
        )
        self.assertEqual(Notification.objects.filter(event_type='SUBSCRIPTION_ACTIVATED', purchase=self.purchase).count(), 1)

    def test_only_the_correct_student_receives_the_activation_notification(self):
        other_student = _user('activation_bystander')
        self._activate()
        self.assertFalse(Notification.objects.filter(event_type='SUBSCRIPTION_ACTIVATED', user=other_student).exists())
        notification = Notification.objects.get(event_type='SUBSCRIPTION_ACTIVATED', purchase=self.purchase)
        self.assertEqual(notification.user_id, self.student.id)

    def test_subscription_activated_includes_the_real_expiry_date_when_one_exists(self):
        self._activate()
        Subscription.objects.filter(user=self.student, course=self.course).update(
            expires_at=timezone.datetime(2026, 10, 15, tzinfo=timezone.get_current_timezone()),
        )
        # Re-fire to observe content with a known expiry (idempotency guard
        # means the FIRST call already froze content — verify the value the
        # real, unmodified first activation produced reflects an actual DB row).
        sub = Subscription.objects.get(user=self.student, course=self.course)
        self.assertIsNotNone(sub.expires_at)
        notification = Notification.objects.get(event_type='SUBSCRIPTION_ACTIVATED', purchase=self.purchase)
        self.assertIn(self.course.name, notification.body)

    def test_grand_test_purchase_never_gets_a_subscription_activated_notification(self):
        from tests_app.models import Test

        test_obj = Test.objects.create(title='GT', exam_type='grand', is_draft=False, price=200)
        gt_purchase = Purchase.objects.create(
            user=self.student, kind='grand_test', grand_test=test_obj, currency='NRS',
            original_amount=200, discount_amount=0, final_amount=200, status='pending',
            payment_reference='TXN-GT-1',
        )
        with self.captureOnCommitCallbacks(execute=True):
            payment_service.activate(gt_purchase.id, actor=None)
        self.assertTrue(Notification.objects.filter(event_type='PAYMENT_APPROVED', purchase=gt_purchase).exists())
        self.assertFalse(Notification.objects.filter(event_type='SUBSCRIPTION_ACTIVATED', purchase=gt_purchase).exists())

    def test_course_less_custom_combo_still_gets_payment_approved_not_silently_un_notified(self):
        """kind == 'combo' alone is NOT sufficient to suppress
        PAYMENT_APPROVED — a "build your own" combo with no combo_plan_id
        resolves no single course (_course_and_item_for_purchase's own
        "never guess when genuinely ambiguous" rule), so it has no
        SUBSCRIPTION_ACTIVATED to fall back on and must keep getting
        PAYMENT_APPROVED, exactly like before this change."""
        combo_purchase = Purchase.objects.create(
            user=self.student, kind='combo', combo_plan=None, currency='NRS',
            original_amount=300, discount_amount=0, final_amount=300, status='pending',
            payment_reference='TXN-COMBO-1',
        )
        with self.captureOnCommitCallbacks(execute=True):
            payment_service.activate(combo_purchase.id, actor=None)
        self.assertTrue(Notification.objects.filter(event_type='PAYMENT_APPROVED', purchase=combo_purchase).exists())
        self.assertFalse(Notification.objects.filter(event_type='SUBSCRIPTION_ACTIVATED', purchase=combo_purchase).exists())


class PaymentRejectedNotificationTests(TestCase):
    """H — payment_service.reject()."""

    def setUp(self):
        self.course = _course('RejectCourse')
        self.plan = _plan(self.course)
        self.student = _user('reject_student')
        self.purchase = _purchase(self.student, self.plan, status='pending', payment_reference='TXN-REJ-1')

    def test_reject_creates_notification_with_the_real_reason(self):
        with self.captureOnCommitCallbacks(execute=True):
            payment_service.reject(self.purchase.id, 'Screenshot does not match the reference number.')
        notification = Notification.objects.get(event_type='PAYMENT_REJECTED', purchase=self.purchase)
        self.assertEqual(notification.user_id, self.student.id)
        self.assertIn('Screenshot does not match the reference number.', notification.body)

    def test_rejection_never_marks_anything_activated(self):
        with self.captureOnCommitCallbacks(execute=True):
            payment_service.reject(self.purchase.id, 'Invalid proof.')
        self.assertFalse(Notification.objects.filter(event_type='SUBSCRIPTION_ACTIVATED', purchase=self.purchase).exists())
        self.assertFalse(Subscription.objects.filter(user=self.student, course=self.course).exists())


class PurchaseCancelledNotificationTests(TestCase):
    """G — payment_service.cancel(). Only reachable from 'unpaid' or
    'resubmission_requested' (see Purchase.STATUS_CHOICES/cancel() itself)
    — a final, confirmed cancellation, never a mere "request"."""

    def setUp(self):
        self.course = _course('CancelCourse')
        self.plan = _plan(self.course)
        self.student = _user('cancel_student')
        self.purchase = _purchase(self.student, self.plan, status='unpaid')

    def test_cancel_creates_a_notification(self):
        with self.captureOnCommitCallbacks(execute=True):
            payment_service.cancel(self.purchase.id)
        self.assertTrue(Notification.objects.filter(event_type='PURCHASE_CANCELLED', purchase=self.purchase).exists())

    def test_cancel_from_approved_is_rejected_by_the_service_layer_and_notifies_nobody(self):
        self.purchase.status = 'approved'
        self.purchase.save(update_fields=['status'])
        from billing.payment_service import PaymentError

        with self.assertRaises(PaymentError):
            with self.captureOnCommitCallbacks(execute=True):
                payment_service.cancel(self.purchase.id)
        self.assertFalse(Notification.objects.filter(event_type='PURCHASE_CANCELLED', purchase=self.purchase).exists())


@override_settings(CRON_SECRET='test-cron-secret')
class SubscriptionExpiryLifecycleTests(TestCase):
    """I / F — the ALREADY-EXISTING SendRenewalRemindersView sweep
    (billing.REMINDER_SCHEDULE: 30/15/7/3/1 days before + 0 = expiry),
    additively wired into the modern pipeline. Real HTTP view, real cron
    secret, matching billing/tests_email_connection_reuse.py's own
    established precedent for testing this exact endpoint."""

    def setUp(self):
        self.course = _course('ExpiryCourse')
        self.student = _user('expiry_student')

    def _hit_cron(self):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post('/api/cron/send-renewal-reminders/', HTTP_X_CRON_SECRET='test-cron-secret')

    def test_seven_days_before_expiry_creates_subscription_expiring(self):
        sub = Subscription.objects.create(
            user=self.student, course=self.course, product_type='qbank',
            expires_at=timezone.now() + timezone.timedelta(days=7),
        )
        self._hit_cron()
        notification = Notification.objects.get(event_type='SUBSCRIPTION_EXPIRING', course=self.course)
        self.assertEqual(notification.user_id, self.student.id)
        self.assertIn(self.course.name, notification.body)

    def test_on_expiry_day_creates_subscription_expired_not_expiring(self):
        sub = Subscription.objects.create(
            user=self.student, course=self.course, product_type='qbank',
            expires_at=timezone.now(),
        )
        self._hit_cron()
        self.assertTrue(Notification.objects.filter(event_type='SUBSCRIPTION_EXPIRED', course=self.course).exists())
        self.assertFalse(Notification.objects.filter(event_type='SUBSCRIPTION_EXPIRING', course=self.course).exists())

    def test_grace_period_day_creates_no_modern_notification(self):
        sub = Subscription.objects.create(
            user=self.student, course=self.course, product_type='qbank',
            expires_at=timezone.now() - timezone.timedelta(days=3),
        )
        self._hit_cron()
        self.assertFalse(Notification.objects.filter(course=self.course, event_type__startswith='SUBSCRIPTION_').exists())

    def test_a_day_with_no_matching_offset_creates_nothing(self):
        Subscription.objects.create(
            user=self.student, course=self.course, product_type='qbank',
            expires_at=timezone.now() + timezone.timedelta(days=12),
        )
        self._hit_cron()
        self.assertFalse(Notification.objects.filter(course=self.course).exists())

    def test_repeated_cron_hits_the_same_day_do_not_duplicate(self):
        """Both the legacy NotificationLog dedupe AND the modern
        dedupe_key independently prevent a duplicate — proving the modern
        side specifically, since NotificationLog's own guard is already
        covered by billing's existing tests."""
        Subscription.objects.create(
            user=self.student, course=self.course, product_type='qbank',
            expires_at=timezone.now() + timezone.timedelta(days=3),
        )
        self._hit_cron()
        self._hit_cron()
        self._hit_cron()
        self.assertEqual(Notification.objects.filter(event_type='SUBSCRIPTION_EXPIRING', course=self.course).count(), 1)

    def test_inactive_subscription_is_never_swept_at_all(self):
        Subscription.objects.create(
            user=self.student, course=self.course, product_type='qbank',
            expires_at=timezone.now() + timezone.timedelta(days=7), is_active=False,
        )
        self._hit_cron()
        self.assertFalse(Notification.objects.filter(course=self.course).exists())


class SubscriptionEventChannelIndependenceTests(TestCase):
    """Task 25/29's explicit matrix: unverified -> in-app/push yes, email
    no; verified -> all three subject to normal preference/device rules.
    Uses SUBSCRIPTION_ACTIVATED — the one notification a subscription
    purchase's activation now fires (see
    PaymentApprovedAndSubscriptionActivatedTests for why PAYMENT_APPROVED
    is deliberately NOT also fired for this kind)."""

    def setUp(self):
        self.course = _course('ChannelCourse')
        self.plan = _plan(self.course)

    def _approve(self, student):
        purchase = _purchase(student, self.plan, status='pending', payment_reference=f'TXN-CH-{student.id}')
        with patch('notifications.email_adapter.send') as mock_send:
            mock_send.return_value = SENT
            with self.captureOnCommitCallbacks(execute=True):
                payment_service.activate(purchase.id, actor=None)
        return purchase, mock_send

    def test_unverified_student_gets_in_app_and_push_but_not_email(self):
        from notifications.models import PushSubscription

        student = _user('unverified_billing_student', email_verified=False)
        PushSubscription.objects.create(
            user=student, endpoint=f'https://push.example.com/{student.id}', p256dh='k' * 20, auth='a' * 10,
            status=PushSubscription.STATUS_ACTIVE,
        )
        purchase, mock_send = self._approve(student)
        notification = Notification.objects.get(event_type='SUBSCRIPTION_ACTIVATED', purchase=purchase)
        self.assertTrue(NotificationDelivery.objects.filter(notification=notification, channel='in_app').exists())
        self.assertTrue(NotificationDelivery.objects.filter(notification=notification, channel='push').exists())
        self.assertFalse(NotificationDelivery.objects.filter(notification=notification, channel='email').exists())
        mock_send.assert_not_called()

    def test_verified_student_gets_email_too(self):
        student = _user('verified_billing_student', email_verified=True)
        purchase, mock_send = self._approve(student)
        notification = Notification.objects.get(event_type='SUBSCRIPTION_ACTIVATED', purchase=purchase)
        delivery = NotificationDelivery.objects.get(notification=notification, channel='email')
        self.assertEqual(delivery.status, NotificationDelivery.STATUS_SENT)
        mock_send.assert_called_once()

    def test_billing_category_is_mandatory_for_in_app_but_email_still_needs_verification(self):
        """Mandatory-category (§27) means preferences can't disable it —
        it must NOT mean 'skip the email_verified gate too'. This is the
        one interaction the audit explicitly required a test for."""
        from notifications.models import NotificationPreference

        student = _user('mandatory_vs_verified_student', email_verified=False)
        # Even an explicit attempt to force-enable email preference must not
        # matter — email is gated by _resolve_channels (never a candidate at
        # all for an unverified user), not by is_channel_enabled's preference
        # check that mandatory categories bypass.
        NotificationPreference.objects.create(user=student, category='billing', channel='email', course=None, enabled=True)
        purchase, mock_send = self._approve(student)
        notification = Notification.objects.get(event_type='SUBSCRIPTION_ACTIVATED', purchase=purchase)
        self.assertFalse(NotificationDelivery.objects.filter(notification=notification, channel='email').exists())
        mock_send.assert_not_called()
