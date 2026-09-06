"""Phase 9 — Commercial Integrity: regression suite.

Covers the mandatory matrices: refund → entitlement revocation (and its
strict non-interference with every other source), the cross-source
entitlement matrix, coupon redemption limits under real concurrency,
payment/refund idempotency, financial authorization, IDOR, and the
historical-integrity guarantee that commerce state never rewrites exam
history. See docs/PHASE_9_ARCHITECTURE.md.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase, APITransactionTestCase

from billing import payment_service
from billing.access import _active_subscriptions, get_grand_test_access, has_qbank_access
from billing.models import (
    Coupon,
    GrandTestAccess,
    PaymentAuditLog,
    Purchase,
    PurchaseComboItem,
    PurchaseEntitlementGrant,
    Scholarship,
    Subscription,
    SubscriptionPlan,
)
from courses.models import Course, Enrollment
from tests_app.models import Test

User = get_user_model()


def _has_active_subscription(user, course, product_type):
    """The shared primitive every public has_*_access() composes — asserting
    on it directly keeps these commerce tests about entitlement state rather
    than about Subject/Test fixture plumbing. One end-to-end test below still
    goes through the real public has_qbank_access() with a real Subject."""
    return _active_subscriptions(user, product_type, course=course).exists()


def _approved_subscription_purchase(user, plan, *, coupon=None, amount=None):
    """A purchase taken all the way through the real service layer, so the
    PurchaseEntitlementGrant rows are created exactly as production would."""
    price = plan.price if amount is None else amount
    purchase = Purchase.objects.create(
        user=user, kind='subscription', plan=plan, coupon=coupon,
        original_amount=price, final_amount=price, status='pending',
    )
    return payment_service.activate(purchase.id)


class Phase9Base(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='p9_student', email='p9_student@example.com', password='pw')
        self.course = Course.objects.create(name='P9 Course', prefix='P9C')
        self.plan = SubscriptionPlan.objects.create(
            course=self.course, product_type='qbank', name='QBank 3M', price=500,
            duration_value=3, duration_unit='month',
        )


class RefundRevocationTests(Phase9Base):
    """Refund reverses exactly what its own purchase granted — nothing more."""

    def test_refund_deactivates_the_subscription_that_purchase_created(self):
        purchase = _approved_subscription_purchase(self.student, self.plan)
        self.assertTrue(_has_active_subscription(self.student, self.course, 'qbank'))

        payment_service.refund(purchase.id, 'Duplicate payment')

        purchase.refresh_from_db()
        self.assertEqual(purchase.status, 'refunded')
        self.assertIsNotNone(purchase.refunded_at)
        self.assertFalse(_has_active_subscription(self.student, self.course, 'qbank'))

    def test_refund_of_a_renewal_rolls_the_expiry_back_instead_of_killing_prior_paid_time(self):
        """The purchase that RENEWED a subscription must not, on refund, take
        the earlier separately-paid-for period with it."""
        first = _approved_subscription_purchase(self.student, self.plan)
        subscription = Subscription.objects.get(user=self.student, course=self.course, product_type='qbank')
        after_first_expiry = subscription.expires_at

        second = _approved_subscription_purchase(self.student, self.plan)
        subscription.refresh_from_db()
        self.assertGreater(subscription.expires_at, after_first_expiry)  # extended, same row

        payment_service.refund(second.id, 'Refunding the renewal only')

        subscription.refresh_from_db()
        self.assertTrue(subscription.is_active)  # NOT deactivated
        self.assertEqual(subscription.expires_at, after_first_expiry)  # exactly the first purchase's time remains
        self.assertTrue(_has_active_subscription(self.student, self.course, 'qbank'))
        first.refresh_from_db()
        self.assertEqual(first.status, 'approved')  # untouched

    def test_refund_is_idempotent(self):
        purchase = _approved_subscription_purchase(self.student, self.plan)
        subscription = Subscription.objects.get(user=self.student, course=self.course)
        payment_service.refund(purchase.id, 'First refund')
        first_refunded_at = Purchase.objects.get(pk=purchase.pk).refunded_at

        payment_service.refund(purchase.id, 'Second call')  # no error, no double-reversal

        purchase.refresh_from_db()
        subscription.refresh_from_db()
        self.assertEqual(purchase.refunded_at, first_refunded_at)
        self.assertFalse(subscription.is_active)
        self.assertEqual(PaymentAuditLog.objects.filter(purchase=purchase, action='refunded').count(), 1)

    def test_only_an_approved_purchase_can_be_refunded(self):
        purchase = Purchase.objects.create(
            user=self.student, kind='subscription', plan=self.plan,
            original_amount=500, final_amount=500, status='pending',
        )
        with self.assertRaises(payment_service.PaymentError):
            payment_service.refund(purchase.id, 'Not approved yet')

    def test_refund_requires_a_reason(self):
        purchase = _approved_subscription_purchase(self.student, self.plan)
        with self.assertRaises(payment_service.PaymentError):
            payment_service.refund(purchase.id, '   ')

    def test_refund_writes_an_audit_entry_naming_what_it_reversed(self):
        purchase = _approved_subscription_purchase(self.student, self.plan)
        payment_service.refund(purchase.id, 'Chargeback')

        entry = PaymentAuditLog.objects.get(purchase=purchase, action='refunded')
        self.assertEqual(entry.previous_status, 'approved')
        self.assertEqual(entry.new_status, 'refunded')
        self.assertEqual(entry.reason, 'Chargeback')
        self.assertTrue(entry.metadata['reversed'])

    def test_grand_test_refund_revokes_access_without_deleting_the_record(self):
        test = Test.objects.create(title='Grand', exam_type='grand', is_pro=True, price=1000)
        purchase = Purchase.objects.create(
            user=self.student, kind='grand_test', grand_test=test,
            original_amount=1000, final_amount=1000, status='pending',
        )
        with patch('billing.payment_service._send_grand_test_email'):
            payment_service.activate(purchase.id)
        self.assertIsNotNone(get_grand_test_access(self.student, test))

        payment_service.refund(purchase.id, 'Refunded grand test')

        self.assertIsNone(get_grand_test_access(self.student, test))  # no longer grants access
        access = GrandTestAccess.objects.get(user=self.student, test=test)
        self.assertIsNotNone(access.revoked_at)  # row (and its issued password) preserved

    def test_repurchasing_after_a_refund_restores_grand_test_access(self):
        test = Test.objects.create(title='Grand', exam_type='grand', is_pro=True, price=1000)
        first = Purchase.objects.create(
            user=self.student, kind='grand_test', grand_test=test,
            original_amount=1000, final_amount=1000, status='pending',
        )
        with patch('billing.payment_service._send_grand_test_email'):
            payment_service.activate(first.id)
        payment_service.refund(first.id, 'Refunded')
        self.assertIsNone(get_grand_test_access(self.student, test))

        second = Purchase.objects.create(
            user=self.student, kind='grand_test', grand_test=test,
            original_amount=1000, final_amount=1000, status='pending',
        )
        with patch('billing.payment_service._send_grand_test_email'):
            payment_service.activate(second.id)

        self.assertIsNotNone(get_grand_test_access(self.student, test))

    def test_refund_removes_real_student_facing_qbank_access_end_to_end(self):
        """Goes through the real public has_qbank_access() with a real
        Subject — proving the refund removes access as a student actually
        experiences it, not just that a row flipped."""
        from academics.models import Subject

        subject = Subject.objects.create(name='Paid Subject', is_free=False)
        subject.courses.set([self.course])
        purchase = _approved_subscription_purchase(self.student, self.plan)
        self.assertTrue(has_qbank_access(self.student, subject))

        payment_service.refund(purchase.id, 'Refund')

        self.assertFalse(has_qbank_access(self.student, subject))

    def test_refund_does_not_touch_the_shared_course_enrollment(self):
        """courses.Enrollment is one shared row per (user, course) across every
        source — deactivating it on refund would break access the student still
        holds elsewhere. Entitlement is enforced by Subscription, which IS
        reversed."""
        purchase = _approved_subscription_purchase(self.student, self.plan)
        payment_service.refund(purchase.id, 'Refund')

        enrollment = Enrollment.objects.get(user=self.student, course=self.course)
        self.assertTrue(enrollment.is_active)
        self.assertFalse(_has_active_subscription(self.student, self.course, 'qbank'))  # paid access still correctly gone


class RefundNonInterferenceTests(Phase9Base):
    """The mandatory revocation matrix: refunding/revoking one source never
    invalidates another independently valid one."""

    def _mock_plan(self):
        return SubscriptionPlan.objects.create(
            course=self.course, product_type='mock_test', name='Mock 3M', price=800,
            duration_value=3, duration_unit='month',
        )

    def test_refund_purchase_while_a_second_purchase_of_a_different_product_remains(self):
        qbank_purchase = _approved_subscription_purchase(self.student, self.plan)
        mock_purchase = _approved_subscription_purchase(self.student, self._mock_plan())

        payment_service.refund(qbank_purchase.id, 'Refund qbank only')

        self.assertFalse(_has_active_subscription(self.student, self.course, 'qbank'))
        self.assertTrue(_has_active_subscription(self.student, self.course, 'mock_test'))
        mock_purchase.refresh_from_db()
        self.assertEqual(mock_purchase.status, 'approved')

    def test_refund_purchase_while_scholarship_for_the_same_product_remains(self):
        purchase = _approved_subscription_purchase(self.student, self.plan)
        scholarship_sub, _ = payment_service._extend_or_create_subscription(
            self.student, self.course, 'qbank', timezone.timedelta(days=90), is_scholarship=True,
        )
        Scholarship.objects.create(
            user=self.student, course=self.course, product_type='qbank', subscription=scholarship_sub,
        )

        payment_service.refund(purchase.id, 'Refund the paid one')

        self.assertTrue(_has_active_subscription(self.student, self.course, 'qbank'))  # scholarship still grants it
        scholarship_sub.refresh_from_db()
        self.assertTrue(scholarship_sub.is_active)

    def test_revoking_scholarship_while_paid_purchase_remains(self):
        """Phase 2's separation, re-verified from the commerce side."""
        purchase = _approved_subscription_purchase(self.student, self.plan)
        scholarship_sub, _ = payment_service._extend_or_create_subscription(
            self.student, self.course, 'qbank', timezone.timedelta(days=90), is_scholarship=True,
        )
        scholarship = Scholarship.objects.create(
            user=self.student, course=self.course, product_type='qbank', subscription=scholarship_sub,
        )

        scholarship.is_active = False
        scholarship.save(update_fields=['is_active'])
        scholarship_sub.is_active = False
        scholarship_sub.save(update_fields=['is_active'])

        self.assertTrue(_has_active_subscription(self.student, self.course, 'qbank'))  # paid access survives
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, 'approved')

    def test_refund_combo_while_direct_purchase_remains(self):
        mock_plan = self._mock_plan()
        combo = Purchase.objects.create(
            user=self.student, kind='combo', original_amount=1300, final_amount=1100, status='pending',
        )
        PurchaseComboItem.objects.create(purchase=combo, plan=self.plan, price=500)
        PurchaseComboItem.objects.create(purchase=combo, plan=mock_plan, price=800)
        payment_service.activate(combo.id)

        # An independent, separately-paid direct purchase of one of the same products.
        direct = _approved_subscription_purchase(self.student, mock_plan)

        payment_service.refund(combo.id, 'Refund the combo')

        # The combo's qbank subscription is gone...
        self.assertFalse(_has_active_subscription(self.student, self.course, 'qbank'))
        # ...but the mock_test row the combo created was then EXTENDED by the
        # direct purchase, so refunding the combo only rolls back the combo's
        # own contribution and the direct purchase's time survives.
        self.assertTrue(_has_active_subscription(self.student, self.course, 'mock_test'))
        direct.refresh_from_db()
        self.assertEqual(direct.status, 'approved')

    def test_refund_direct_purchase_while_combo_remains(self):
        mock_plan = self._mock_plan()
        direct = _approved_subscription_purchase(self.student, mock_plan)
        combo = Purchase.objects.create(
            user=self.student, kind='combo', original_amount=1300, final_amount=1100, status='pending',
        )
        PurchaseComboItem.objects.create(purchase=combo, plan=self.plan, price=500)
        PurchaseComboItem.objects.create(purchase=combo, plan=mock_plan, price=800)
        payment_service.activate(combo.id)

        payment_service.refund(direct.id, 'Refund the direct purchase')

        self.assertTrue(_has_active_subscription(self.student, self.course, 'qbank'))  # combo's qbank untouched
        self.assertTrue(_has_active_subscription(self.student, self.course, 'mock_test'))  # combo's extension survives

    def test_expired_enrollment_does_not_invalidate_an_active_subscription_purchase(self):
        purchase = _approved_subscription_purchase(self.student, self.plan)
        enrollment = Enrollment.objects.get(user=self.student, course=self.course)
        enrollment.expires_at = timezone.now() - timezone.timedelta(days=1)
        enrollment.save(update_fields=['expires_at'])

        # The subscription itself is still valid and still the entitlement source.
        subscription = Subscription.objects.get(user=self.student, course=self.course, product_type='qbank')
        self.assertTrue(subscription.is_current)
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, 'approved')

    def test_refund_never_touches_another_students_access(self):
        other = User.objects.create_user(username='p9_other', email='p9_other@example.com', password='pw')
        mine = _approved_subscription_purchase(self.student, self.plan)
        theirs = _approved_subscription_purchase(other, self.plan)

        payment_service.refund(mine.id, 'Mine only')

        self.assertFalse(_has_active_subscription(self.student, self.course, 'qbank'))
        self.assertTrue(_has_active_subscription(other, self.course, 'qbank'))
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, 'approved')


class EntitlementGrantTraceabilityTests(Phase9Base):
    """The purchase → entitlement link the audit found missing."""

    def test_activation_records_a_grant_per_granted_subscription(self):
        purchase = _approved_subscription_purchase(self.student, self.plan)
        grants = PurchaseEntitlementGrant.objects.filter(purchase=purchase)
        self.assertEqual(grants.count(), 1)
        grant = grants.first()
        self.assertTrue(grant.was_created)
        self.assertIsNone(grant.previous_expires_at)
        self.assertIsNotNone(grant.subscription_id)

    def test_a_combo_records_one_grant_per_bundled_plan(self):
        mock_plan = SubscriptionPlan.objects.create(
            course=self.course, product_type='mock_test', name='Mock', price=800,
            duration_value=3, duration_unit='month',
        )
        combo = Purchase.objects.create(
            user=self.student, kind='combo', original_amount=1300, final_amount=1100, status='pending',
        )
        PurchaseComboItem.objects.create(purchase=combo, plan=self.plan, price=500)
        PurchaseComboItem.objects.create(purchase=combo, plan=mock_plan, price=800)
        payment_service.activate(combo.id)

        self.assertEqual(PurchaseEntitlementGrant.objects.filter(purchase=combo).count(), 2)

    def test_a_renewal_grant_records_the_previous_expiry(self):
        _approved_subscription_purchase(self.student, self.plan)
        original_expiry = Subscription.objects.get(user=self.student, course=self.course).expires_at

        second = _approved_subscription_purchase(self.student, self.plan)

        grant = PurchaseEntitlementGrant.objects.get(purchase=second)
        self.assertFalse(grant.was_created)
        self.assertEqual(grant.previous_expires_at, original_expiry)
        self.assertGreater(grant.granted_expires_at, original_expiry)

    def test_scholarship_and_admin_grants_create_no_purchase_grant_rows(self):
        payment_service._extend_or_create_subscription(
            self.student, self.course, 'qbank', timezone.timedelta(days=90), is_scholarship=True,
        )
        self.assertEqual(PurchaseEntitlementGrant.objects.count(), 0)


class CouponRedemptionLimitTests(Phase9Base):
    """Redemption-time enforcement — the creation-time check alone could never
    cap anything, since approval happens later via a human review step."""

    def _coupon(self, **overrides):
        defaults = dict(code='SAVE10', discount_type='percentage', discount_value=10, max_uses=1)
        defaults.update(overrides)
        return Coupon.objects.create(**defaults)

    def test_max_uses_is_enforced_at_activation_not_only_at_order_creation(self):
        coupon = self._coupon(max_uses=1)
        other = User.objects.create_user(username='p9_c2', email='p9_c2@example.com', password='pw')

        # Both orders are created while usage_count is still 0 — exactly the
        # window the pre-Phase-9 creation-time check could not cover.
        first = Purchase.objects.create(
            user=self.student, kind='subscription', plan=self.plan, coupon=coupon,
            original_amount=500, final_amount=450, status='pending',
        )
        second = Purchase.objects.create(
            user=other, kind='subscription', plan=self.plan, coupon=coupon,
            original_amount=500, final_amount=450, status='pending',
        )

        payment_service.activate(first.id)
        with self.assertRaises(payment_service.PaymentError):
            payment_service.activate(second.id)

        coupon.refresh_from_db()
        self.assertEqual(coupon.usage_count, 1)  # never exceeds max_uses
        second.refresh_from_db()
        self.assertEqual(second.status, 'pending')  # left undecided, not silently approved

    def test_max_uses_per_user_is_enforced_at_activation(self):
        coupon = self._coupon(max_uses=None, max_uses_per_user=1)
        first = Purchase.objects.create(
            user=self.student, kind='subscription', plan=self.plan, coupon=coupon,
            original_amount=500, final_amount=450, status='pending',
        )
        second = Purchase.objects.create(
            user=self.student, kind='subscription', plan=self.plan, coupon=coupon,
            original_amount=500, final_amount=450, status='pending',
        )

        payment_service.activate(first.id)
        with self.assertRaises(payment_service.PaymentError):
            payment_service.activate(second.id)

        coupon.refresh_from_db()
        self.assertEqual(coupon.usage_count, 1)

    def test_a_second_user_can_still_redeem_a_per_user_limited_coupon(self):
        coupon = self._coupon(max_uses=None, max_uses_per_user=1)
        other = User.objects.create_user(username='p9_c3', email='p9_c3@example.com', password='pw')
        first = Purchase.objects.create(
            user=self.student, kind='subscription', plan=self.plan, coupon=coupon,
            original_amount=500, final_amount=450, status='pending',
        )
        second = Purchase.objects.create(
            user=other, kind='subscription', plan=self.plan, coupon=coupon,
            original_amount=500, final_amount=450, status='pending',
        )

        payment_service.activate(first.id)
        payment_service.activate(second.id)  # different user — allowed

        coupon.refresh_from_db()
        self.assertEqual(coupon.usage_count, 2)

    def test_unlimited_coupon_is_unaffected(self):
        coupon = self._coupon(max_uses=None, max_uses_per_user=10)
        for _ in range(3):
            purchase = Purchase.objects.create(
                user=self.student, kind='subscription', plan=self.plan, coupon=coupon,
                original_amount=500, final_amount=450, status='pending',
            )
            payment_service.activate(purchase.id)
        coupon.refresh_from_db()
        self.assertEqual(coupon.usage_count, 3)

    def test_refund_does_not_return_the_coupon_slot(self):
        """Documented business decision: the cap stays a hard ceiling that a
        buy/refund cycle can't be used to farm past. See
        docs/PHASE_9_ARCHITECTURE.md if this policy is ever revisited."""
        coupon = self._coupon(max_uses=1)
        purchase = Purchase.objects.create(
            user=self.student, kind='subscription', plan=self.plan, coupon=coupon,
            original_amount=500, final_amount=450, status='pending',
        )
        payment_service.activate(purchase.id)
        payment_service.refund(purchase.id, 'Refunded')

        coupon.refresh_from_db()
        self.assertEqual(coupon.usage_count, 1)


class CouponConcurrencyTests(APITransactionTestCase):
    """Real concurrent activation against a limited coupon — the invariant is
    that usage_count can never exceed max_uses. Mirrors the threading +
    SQLite busy_timeout pattern already established by
    tests_app.SubmitTestDoubleSubmissionRaceTests."""

    def test_concurrent_activations_never_exceed_max_uses(self):
        import threading
        import time

        from django.db import connection

        course = Course.objects.create(name='Race Course', prefix='RC')
        plan = SubscriptionPlan.objects.create(
            course=course, product_type='qbank', name='QBank', price=500,
            duration_value=3, duration_unit='month',
        )
        coupon = Coupon.objects.create(
            code='RACE1', discount_type='percentage', discount_value=10, max_uses=1, max_uses_per_user=5,
        )
        purchases = []
        for i in range(2):
            user = User.objects.create_user(username=f'race{i}', email=f'race{i}@example.com', password='pw')
            purchases.append(Purchase.objects.create(
                user=user, kind='subscription', plan=plan, coupon=coupon,
                original_amount=500, final_amount=450, status='pending',
            ))

        outcomes = []
        lock = threading.Lock()

        def activate(purchase_id):
            for attempt_no in range(20):
                try:
                    if connection.vendor == 'sqlite':
                        with connection.cursor() as cur:
                            cur.execute('PRAGMA busy_timeout = 30000')
                    payment_service.activate(purchase_id)
                    with lock:
                        outcomes.append('approved')
                    return
                except payment_service.PaymentError:
                    with lock:
                        outcomes.append('rejected')
                    return
                except Exception as exc:  # noqa: BLE001 — SQLite lock contention retry, documented precedent
                    if 'locked' in str(exc).lower() and attempt_no < 19:
                        time.sleep(0.05)
                        continue
                    raise
                finally:
                    connection.close()

        threads = [threading.Thread(target=activate, args=(p.id,)) for p in purchases]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        coupon.refresh_from_db()
        # The actual invariant: the cap is never breached, regardless of how
        # the two threads interleaved.
        self.assertLessEqual(coupon.usage_count, 1)
        self.assertEqual(
            Purchase.objects.filter(coupon=coupon, status='approved').count(), coupon.usage_count,
        )


class PaymentIdempotencyTests(Phase9Base):
    """Repeated identical commerce events converge to one logical result."""

    def test_activating_twice_does_not_double_grant_or_double_count(self):
        purchase = Purchase.objects.create(
            user=self.student, kind='subscription', plan=self.plan,
            original_amount=500, final_amount=500, status='pending',
        )
        payment_service.activate(purchase.id)
        expiry_after_first = Subscription.objects.get(user=self.student, course=self.course).expires_at

        with self.assertRaises(payment_service.PaymentError):
            payment_service.activate(purchase.id)  # already decided

        self.assertEqual(Subscription.objects.filter(user=self.student, course=self.course).count(), 1)
        self.assertEqual(Subscription.objects.get(user=self.student, course=self.course).expires_at, expiry_after_first)
        self.assertEqual(PurchaseEntitlementGrant.objects.filter(purchase=purchase).count(), 1)

    def test_a_duplicate_payment_reference_cannot_be_approved_twice(self):
        first = Purchase.objects.create(
            user=self.student, kind='subscription', plan=self.plan, payment_reference='DUP-1',
            original_amount=500, final_amount=500, status='pending',
        )
        second = Purchase.objects.create(
            user=self.student, kind='subscription', plan=self.plan, payment_reference='DUP-1',
            original_amount=500, final_amount=500, status='pending',
        )
        payment_service.activate(first.id)
        with self.assertRaises(payment_service.PaymentError):
            payment_service.activate(second.id)


class CommerceAuthorizationTests(APITestCase):
    """Financial state transitions are admin-role-gated (Phase 1) and can
    never be driven from a student's request payload."""

    def setUp(self):
        self.student = User.objects.create_user(username='auth_student', email='auth_student@example.com', password='pw')
        self.other_student = User.objects.create_user(username='auth_other', email='auth_other@example.com', password='pw')
        self.editor = User.objects.create_user(
            username='auth_editor', email='auth_editor@example.com', password='pw', is_staff=True, admin_role='editor',
        )
        self.admin = User.objects.create_user(
            username='auth_admin', email='auth_admin@example.com', password='pw', is_staff=True, admin_role='admin',
        )
        self.course = Course.objects.create(name='Auth Course', prefix='AC')
        self.plan = SubscriptionPlan.objects.create(
            course=self.course, product_type='qbank', name='QBank', price=500,
            duration_value=3, duration_unit='month',
        )
        self.purchase = Purchase.objects.create(
            user=self.student, kind='subscription', plan=self.plan,
            original_amount=500, final_amount=500, status='pending',
        )
        payment_service.activate(self.purchase.id)

    def test_anonymous_cannot_refund(self):
        resp = self.client.post(f'/api/purchases/{self.purchase.id}/refund/', {'reason': 'x'})
        self.assertIn(resp.status_code, (401, 403))

    def test_student_cannot_refund_their_own_purchase(self):
        self.client.force_authenticate(user=self.student)
        resp = self.client.post(f'/api/purchases/{self.purchase.id}/refund/', {'reason': 'gimme'})
        self.assertEqual(resp.status_code, 403)
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.status, 'approved')

    def test_editor_staff_cannot_refund(self):
        """Financial decisions are IsAdminRoleOrAbove, not merely is_staff —
        Phase 1's fix, re-verified for the new endpoint."""
        self.client.force_authenticate(user=self.editor)
        resp = self.client.post(f'/api/purchases/{self.purchase.id}/refund/', {'reason': 'nope'})
        self.assertEqual(resp.status_code, 403)
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.status, 'approved')

    def test_admin_can_refund(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(f'/api/purchases/{self.purchase.id}/refund/', {'reason': 'Approved refund request'})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.status, 'refunded')
        self.assertEqual(self.purchase.refunded_by_id, self.admin.id)

    def test_refund_without_a_reason_is_rejected(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(f'/api/purchases/{self.purchase.id}/refund/', {})
        self.assertEqual(resp.status_code, 400)

    def test_student_cannot_set_purchase_status_through_the_create_payload(self):
        self.client.force_authenticate(user=self.student)
        resp = self.client.post('/api/purchases/', {
            'kind': 'subscription', 'plan_id': self.plan.id,
            'status': 'approved', 'final_amount': '1.00', 'discount_amount': '499.00',
        })
        self.assertEqual(resp.status_code, 201, resp.data)
        created = Purchase.objects.get(pk=resp.data['id'])
        self.assertEqual(created.status, 'unpaid')  # server-side, not client-declared
        self.assertEqual(created.final_amount, self.plan.price)  # price recomputed server-side

    def test_student_cannot_see_another_students_purchase(self):
        self.client.force_authenticate(user=self.other_student)
        resp = self.client.get(f'/api/purchases/{self.purchase.id}/')
        self.assertEqual(resp.status_code, 404)

    def test_student_cannot_read_another_students_purchase_audit_log(self):
        self.client.force_authenticate(user=self.other_student)
        resp = self.client.get(f'/api/purchases/{self.purchase.id}/audit-log/')
        self.assertIn(resp.status_code, (403, 404))


class RefundHistoricalIntegrityTests(Phase9Base):
    """Commerce state never rewrites exam history (Phase 7/8 protection)."""

    def test_refund_leaves_finalized_attempts_results_and_snapshots_intact(self):
        from academics.models import Option, Question, Subject
        from tests_app.lifecycle import finalize_attempt
        from tests_app.models import Answer, AttemptQuestionSnapshot, TestAttempt, TestQuestion

        purchase = _approved_subscription_purchase(self.student, self.plan)

        subject = Subject.objects.create(name='P9 Subject')
        question = Question.objects.create(subject=subject, text='Q?', marks=2, negative_marks=0)
        correct = Option.objects.create(question=question, text='Right', order=0, is_correct=True)
        exam = Test.objects.create(title='Paid Exam', exam_type='mock', is_draft=False, duration_minutes=60)
        exam.assigned_students.set([self.student])
        TestQuestion.objects.create(test=exam, question=question)

        attempt = TestAttempt.objects.create(user=self.student, test=exam)
        Answer.objects.create(attempt=attempt, question=question, selected_option=correct, is_correct=True)
        attempt = finalize_attempt(attempt, auto_submitted=False)
        score_before = attempt.score
        snapshot_count = AttemptQuestionSnapshot.objects.filter(attempt=attempt).count()
        self.assertEqual(snapshot_count, 1)

        payment_service.refund(purchase.id, 'Refund after the exam was taken')

        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'submitted')
        self.assertEqual(attempt.score, score_before)
        self.assertEqual(AttemptQuestionSnapshot.objects.filter(attempt=attempt).count(), snapshot_count)
        self.assertTrue(Answer.objects.filter(attempt=attempt).exists())


class CronSecretHardeningTests(APITestCase):
    """The cron endpoints fail closed when no secret is configured."""

    def test_blank_configured_secret_rejects_even_a_blank_header(self):
        from django.test import override_settings

        with override_settings(CRON_SECRET=''):
            resp = self.client.post('/api/cron/expire-stale-payments/', {}, HTTP_X_CRON_SECRET='')
            self.assertEqual(resp.status_code, 401)
            resp = self.client.post('/api/cron/expire-stale-payments/', {})
            self.assertEqual(resp.status_code, 401)

    def test_correct_secret_still_works(self):
        from django.test import override_settings

        with override_settings(CRON_SECRET='a-real-secret'):
            resp = self.client.post('/api/cron/expire-stale-payments/', {}, HTTP_X_CRON_SECRET='a-real-secret')
            self.assertEqual(resp.status_code, 200)

    def test_wrong_secret_is_rejected(self):
        from django.test import override_settings

        with override_settings(CRON_SECRET='a-real-secret'):
            resp = self.client.post('/api/cron/expire-stale-payments/', {}, HTTP_X_CRON_SECRET='guess')
            self.assertEqual(resp.status_code, 401)
