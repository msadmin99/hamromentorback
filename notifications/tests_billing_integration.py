"""Phase 2 — Billing domain-event integration tests (P0-BILLING,
P0-TRANSACTION-SAFETY, P0-DEDUPLICATION for the billing events).

Drives the REAL API + payment_service.activate() flow (create purchase ->
submit payment -> activate), reusing billing/tests.py's own
BillingTestCase fixtures/helpers, per the acceptance contract's
instruction to prove the real lifecycle rather than calling
notifications.billing_integration functions directly in isolation.
"""
from unittest.mock import patch

from django.db import transaction

from billing import payment_service
from billing.models import Purchase
from billing.tests import BillingTestCase

from .models import Notification


class PaymentApprovedNotificationTests(BillingTestCase):
    def test_activation_notification_only_exists_after_commit(self):
        """RENAMED (subscription-notification-model follow-up): a
        subscription purchase (BillingTestCase._create_purchase()'s
        default kind) now fires SUBSCRIPTION_ACTIVATED, not PAYMENT_APPROVED
        — see test_subscription_activation_fires_subscription_activated_only
        below for the full "one meaningful event, one notification" proof.
        This test's own original point (on_commit timing) is unaffected by
        which specific event type it is."""
        purchase = self._create_purchase()
        self._submit(purchase['id'], reference='TXN-101')

        self.assertFalse(Notification.objects.filter(purchase_id=purchase['id']).exists())

        with self.captureOnCommitCallbacks(execute=True):
            payment_service.activate(purchase['id'], actor=self.staff)

        n = Notification.objects.get(purchase_id=purchase['id'], event_type='SUBSCRIPTION_ACTIVATED')
        self.assertEqual(n.user_id, self.student.id)
        self.assertEqual(n.course_id, self.course.id)  # subscription plan's own course

    def test_subscription_activation_fires_subscription_activated_only(self):
        """RENAMED — was 'also fires', proving SUBSCRIPTION_ACTIVATED
        existed ALONGSIDE PAYMENT_APPROVED. Product decision reversed
        that: a subscription/combo activation must fire ONLY
        SUBSCRIPTION_ACTIVATED, never additionally the generic
        PAYMENT_APPROVED (the student doesn't need two notifications for
        the same successful activation)."""
        purchase = self._create_purchase()
        self._submit(purchase['id'], reference='TXN-102')

        with self.captureOnCommitCallbacks(execute=True):
            payment_service.activate(purchase['id'], actor=self.staff)

        self.assertTrue(
            Notification.objects.filter(purchase_id=purchase['id'], event_type='SUBSCRIPTION_ACTIVATED').exists()
        )
        self.assertFalse(
            Notification.objects.filter(purchase_id=purchase['id'], event_type='PAYMENT_APPROVED').exists()
        )
        self.assertEqual(Notification.objects.filter(purchase_id=purchase['id']).count(), 1)

    def test_rolled_back_activation_creates_no_notification(self):
        """P0-TRANSACTION-SAFETY: if activate() raises after the point where
        a real caller might have committed a partial change, no notification
        must exist. Simulated here by activating an already-approved
        purchase a second time, inside an atomic block, and asserting the
        forced failure leaves no notification for that second attempt."""
        purchase = self._create_purchase()
        self._submit(purchase['id'], reference='TXN-103')
        with self.captureOnCommitCallbacks(execute=True):
            payment_service.activate(purchase['id'], actor=self.staff)
        first_count = Notification.objects.filter(purchase_id=purchase['id']).count()
        self.assertGreater(first_count, 0)

        # Second activate() on an already-approved purchase must raise
        # PaymentError BEFORE any notification call — the atomic block's
        # own guard (`purchase.status not in allowed_statuses`) fires
        # first, so notify_payment_approved is never even reached.
        with self.assertRaises(payment_service.PaymentError):
            with self.captureOnCommitCallbacks(execute=True):
                payment_service.activate(purchase['id'], actor=self.staff)

        self.assertEqual(Notification.objects.filter(purchase_id=purchase['id']).count(), first_count)

    def test_outer_transaction_rollback_after_activate_creates_no_notification(self):
        """P0-TRANSACTION-SAFETY, the real case notify_payment_approved's
        transaction.on_commit is defending against: activate() is called
        from inside a LARGER atomic block (e.g. a bulk-approve admin
        action) that itself fails/rolls back after activate() returns.
        Because the notification call is registered via on_commit rather
        than fired immediately, it must never have executed — proving
        on_commit (not a plain call) was the right choice here, and that a
        rollback anywhere in the outer transaction really does prevent the
        notification, not just a rollback inside activate()'s own inner
        block (already covered by the PaymentError case above)."""
        purchase = self._create_purchase()
        self._submit(purchase['id'], reference='TXN-105')

        class _DeliberateFailure(Exception):
            pass

        try:
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    payment_service.activate(purchase['id'], actor=self.staff)
                    raise _DeliberateFailure()
        except _DeliberateFailure:
            pass

        self.assertFalse(Notification.objects.filter(purchase_id=purchase['id']).exists())
        # And the Purchase itself was rolled back too — proving this is a
        # real, whole-transaction rollback, not a partial one.
        purchase_row = Purchase.objects.get(pk=purchase['id'])
        self.assertNotEqual(purchase_row.status, 'approved')

    def test_calling_activate_via_admin_endpoint_creates_exactly_one_notification(self):
        """Real HTTP call site, not the service function directly —
        billing/views.py's approve action. SUBSCRIPTION_ACTIVATED, not
        PAYMENT_APPROVED, for this default subscription purchase — see
        test_subscription_activation_fires_subscription_activated_only."""
        purchase = self._create_purchase()
        self._submit(purchase['id'], reference='TXN-104')

        self.client.force_authenticate(user=self.staff)
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(f'/api/purchases/{purchase["id"]}/approve/')
        self.assertEqual(resp.status_code, 200, resp.data)

        self.assertEqual(
            Notification.objects.filter(purchase_id=purchase['id'], event_type='SUBSCRIPTION_ACTIVATED').count(), 1,
        )
        self.assertEqual(Notification.objects.filter(purchase_id=purchase['id']).count(), 1)

    def test_grand_test_purchase_has_no_course_but_still_gets_payment_approved(self):
        from billing.models import GrandTestAccess
        from tests_app.models import Test

        test = Test.objects.create(title='Grand Test Alpha', exam_type='grand', is_draft=False, price=100)
        self.client.force_authenticate(user=self.student)
        resp = self.client.post('/api/purchases/', {'kind': 'grand_test', 'grand_test_id': test.id})
        self.assertEqual(resp.status_code, 201, resp.data)
        purchase_id = resp.data['id']
        self._submit(purchase_id, reference='TXN-GT-1')

        with patch('billing.payment_service._send_grand_test_email'):
            with self.captureOnCommitCallbacks(execute=True):
                payment_service.activate(purchase_id, actor=self.staff)

        n = Notification.objects.get(purchase_id=purchase_id, event_type='PAYMENT_APPROVED')
        self.assertIsNone(n.course_id)
        self.assertFalse(Notification.objects.filter(purchase_id=purchase_id, event_type='SUBSCRIPTION_ACTIVATED').exists())
