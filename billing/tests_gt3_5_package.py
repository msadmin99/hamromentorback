"""Grand Test 3.0 / GT3-5 — Bulk Grand Test Marketplace: regression suite.

Core rule under test: ONE commercial purchase (kind='grand_test_package')
produces MULTIPLE, fully independent billing.GrandTestAccess rows — never
a single "N attempts" grant. Every included Grand Test then behaves
exactly per its own schedule/participation/review rules (GT3-2/GT3-4),
completely unaffected by the fact that it arrived via a package rather
than an individual purchase.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from billing import payment_service
from billing.access import get_grand_test_access
from billing.models import Coupon, GrandTestAccess, GrandTestPackage, Purchase, PurchaseEntitlementGrant
from tests_app.lifecycle import grand_test_participation_status
from tests_app.models import Test, TestAttempt

User = get_user_model()


def _mkgrand(title, start, end, price=Decimal('50')):
    return Test.objects.create(
        title=title, exam_type='grand', is_pro=True, price=price, is_draft=False,
        scheduled_start=start, scheduled_end=end,
    )


def _mkpackage(tests, price=Decimal('100'), is_active=True):
    package = GrandTestPackage.objects.create(name='Grand Test 5-Pack', price=price, is_active=is_active)
    package.tests.set(tests)
    return package


class SingleVsPackagePurchaseTests(APITestCase):
    """Tests 1-4 — the core commercial/entitlement-multiplicity rule."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt5_a', email='gt5_a@example.com', password='pw')
        self.client.force_authenticate(user=self.student)

    def test_1_single_grand_test_purchase_creates_exactly_one_entitlement(self):
        now = timezone.now()
        test = _mkgrand('GT-Solo', now + timezone.timedelta(days=1), now + timezone.timedelta(days=1, hours=3))
        test.assigned_students.add(self.student)
        resp = self.client.post('/api/purchases/', {'kind': 'grand_test', 'grand_test_id': test.id})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        purchase = Purchase.objects.get(pk=resp.data['id'])
        with patch('billing.payment_service._send_grand_test_email'):
            payment_service.activate(purchase.id, allow_unpaid=True)
        self.assertEqual(GrandTestAccess.objects.filter(user=self.student).count(), 1)

    def test_2_five_test_package_creates_exactly_five_independent_entitlements(self):
        now = timezone.now()
        tests = [
            _mkgrand(f'GT-{i}', now + timezone.timedelta(days=i), now + timezone.timedelta(days=i, hours=3))
            for i in range(1, 6)
        ]
        for t in tests:
            t.assigned_students.add(self.student)
        package = _mkpackage(tests)

        resp = self.client.post('/api/purchases/', {'kind': 'grand_test_package', 'grand_test_package_id': package.id})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        purchase = Purchase.objects.get(pk=resp.data['id'])
        self.assertEqual(float(purchase.final_amount), 100.0)  # the flat package price, not summed test prices
        with patch('billing.payment_service._send_grand_test_package_email'):
            payment_service.activate(purchase.id, allow_unpaid=True)

        accesses = GrandTestAccess.objects.filter(user=self.student)
        self.assertEqual(accesses.count(), 5)  # Test 2

    def test_3_all_five_entitlements_belong_to_the_correct_user(self):
        now = timezone.now()
        tests = [_mkgrand(f'GT-{i}', now + timezone.timedelta(days=i), now + timezone.timedelta(days=i, hours=3)) for i in range(1, 6)]
        package = _mkpackage(tests)
        purchase = Purchase.objects.create(
            user=self.student, kind='grand_test_package', grand_test_package=package,
            original_amount=100, final_amount=100, status='pending',
        )
        for t in tests:
            purchase.grand_test_package_items.create(test=t)
        with patch('billing.payment_service._send_grand_test_package_email'):
            payment_service.activate(purchase.id)
        for access in GrandTestAccess.objects.filter(test__in=tests):
            self.assertEqual(access.user_id, self.student.id)

    def test_4_all_five_reference_the_correct_grand_tests(self):
        now = timezone.now()
        tests = [_mkgrand(f'GT-{i}', now + timezone.timedelta(days=i), now + timezone.timedelta(days=i, hours=3)) for i in range(1, 6)]
        other_test = _mkgrand('Not included', now, now + timezone.timedelta(hours=3))
        package = _mkpackage(tests)
        purchase = Purchase.objects.create(
            user=self.student, kind='grand_test_package', grand_test_package=package,
            original_amount=100, final_amount=100, status='pending',
        )
        for t in tests:
            purchase.grand_test_package_items.create(test=t)
        with patch('billing.payment_service._send_grand_test_package_email'):
            payment_service.activate(purchase.id)
        entitled_test_ids = set(GrandTestAccess.objects.filter(user=self.student).values_list('test_id', flat=True))
        self.assertEqual(entitled_test_ids, {t.id for t in tests})
        self.assertNotIn(other_test.id, entitled_test_ids)


class IndependentScheduleAndStateTests(APITestCase):
    """Tests 5-10 — every included Grand Test keeps its own schedule,
    participation state, and review, completely independent of the others
    and of the package itself."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt5_b', email='gt5_b@example.com', password='pw')
        self.client.force_authenticate(user=self.student)
        now = timezone.now()
        # A realistic Rani-style 5-test series: 2 already closed (1 missed,
        # 1 appeared), 1 currently live, 2 still upcoming.
        self.gt_missed = _mkgrand('GT-I', now - timezone.timedelta(hours=5), now - timezone.timedelta(hours=2))
        self.gt_completed = _mkgrand('GT-II', now - timezone.timedelta(hours=4), now - timezone.timedelta(hours=1))
        self.gt_upcoming_a = _mkgrand('GT-III', now + timezone.timedelta(days=7), now + timezone.timedelta(days=7, hours=3))
        self.gt_upcoming_b = _mkgrand('GT-IV', now + timezone.timedelta(days=14), now + timezone.timedelta(days=14, hours=3))
        self.gt_live = _mkgrand('GT-V', now - timezone.timedelta(minutes=30), now + timezone.timedelta(hours=2))
        self.tests = [self.gt_missed, self.gt_completed, self.gt_upcoming_a, self.gt_upcoming_b, self.gt_live]
        for t in self.tests:
            t.assigned_students.add(self.student)
        self.package = _mkpackage(self.tests)
        purchase = Purchase.objects.create(
            user=self.student, kind='grand_test_package', grand_test_package=self.package,
            original_amount=100, final_amount=100, status='pending',
        )
        for t in self.tests:
            purchase.grand_test_package_items.create(test=t)
        with patch('billing.payment_service._send_grand_test_package_email'):
            payment_service.activate(purchase.id)
        # Rani appeared for GT-II before it closed.
        TestAttempt.objects.create(
            user=self.student, test=self.gt_completed, status='submitted', score=3,
            start_time=now - timezone.timedelta(hours=3, minutes=30),
        )

    def test_5_each_grand_test_retains_its_own_schedule(self):
        self.assertNotEqual(self.gt_missed.scheduled_start, self.gt_completed.scheduled_start)
        self.assertNotEqual(self.gt_upcoming_a.scheduled_start, self.gt_upcoming_b.scheduled_start)

    def test_6_upcoming_tests_cannot_be_started_early(self):
        resp = self.client.post(f'/api/tests/{self.gt_upcoming_a.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data['code'], 'grand_test_not_started')

    def test_7_one_missed_test_does_not_affect_other_entitlements(self):
        self.assertEqual(grand_test_participation_status(self.gt_missed, self.student), 'missed')
        # Every other entitlement in the package is completely unaffected:
        self.assertIsNotNone(get_grand_test_access(self.student, self.gt_completed))
        self.assertIsNotNone(get_grand_test_access(self.student, self.gt_upcoming_a))
        self.assertIsNotNone(get_grand_test_access(self.student, self.gt_upcoming_b))
        self.assertIsNotNone(get_grand_test_access(self.student, self.gt_live))
        self.assertEqual(grand_test_participation_status(self.gt_live, self.student), 'live')

    def test_8_one_completed_test_does_not_change_other_entitlements(self):
        self.assertEqual(grand_test_participation_status(self.gt_completed, self.student), 'completed')
        self.assertEqual(grand_test_participation_status(self.gt_missed, self.student), 'missed')
        self.assertEqual(grand_test_participation_status(self.gt_upcoming_a, self.student), 'upcoming')

    def test_9_appeared_test_works_with_gt3_4_review(self):
        attempt = TestAttempt.objects.get(test=self.gt_completed, user=self.student)
        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['review_status'], 'available')  # solutions_visibility defaults to 'auto'

    def test_10_missed_test_works_with_gt3_2_and_gt3_4_missed_review(self):
        resp = self.client.get(f'/api/tests/{self.gt_missed.id}/missed-review/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['review_type'], 'missed_review')
        self.assertFalse(TestAttempt.objects.filter(test=self.gt_missed, user=self.student).exists())

    def test_final_series_state_matches_the_grand_test_3_0_acceptance_scenario(self):
        states = {
            t.title: grand_test_participation_status(t, self.student)
            for t in [self.gt_missed, self.gt_completed, self.gt_upcoming_a, self.gt_upcoming_b, self.gt_live]
        }
        self.assertEqual(states['GT-I'], 'missed')
        self.assertEqual(states['GT-II'], 'completed')
        self.assertEqual(states['GT-III'], 'upcoming')
        self.assertEqual(states['GT-IV'], 'upcoming')
        self.assertEqual(states['GT-V'], 'live')


class SecurityTests(APITestCase):
    """Tests 11-13 — IDOR and payment-bypass protection."""

    def setUp(self):
        self.owner = User.objects.create_user(username='gt5_owner', email='gt5_owner@example.com', password='pw')
        self.other = User.objects.create_user(username='gt5_other', email='gt5_other@example.com', password='pw')
        now = timezone.now()
        self.tests = [_mkgrand(f'GT-{i}', now - timezone.timedelta(hours=1), now + timezone.timedelta(hours=2)) for i in range(1, 3)]
        for t in self.tests:
            t.assigned_students.add(self.owner, self.other)
        self.package = _mkpackage(self.tests, price=Decimal('80'))

    def test_11_non_entitled_user_cannot_use_package_or_order_ids_to_access_tests(self):
        purchase = Purchase.objects.create(
            user=self.owner, kind='grand_test_package', grand_test_package=self.package,
            original_amount=80, final_amount=80, status='pending',
        )
        for t in self.tests:
            purchase.grand_test_package_items.create(test=t)
        with patch('billing.payment_service._send_grand_test_package_email'):
            payment_service.activate(purchase.id)

        self.client.force_authenticate(user=self.other)
        # Knowing the purchase id, the package id, or the test id is not
        # entitlement — `other` never bought anything.
        resp = self.client.post(f'/api/tests/{self.tests[0].id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertEqual(resp.data['code'], 'purchase_required')
        self.assertFalse(GrandTestAccess.objects.filter(user=self.other).exists())

    def test_12_failed_payment_creates_no_entitlement(self):
        purchase = Purchase.objects.create(
            user=self.owner, kind='grand_test_package', grand_test_package=self.package,
            original_amount=80, final_amount=80, status='rejected',
        )
        for t in self.tests:
            purchase.grand_test_package_items.create(test=t)
        # 'rejected' is a terminal, non-approved status — activate() must
        # refuse to run at all (its own status guard, unmodified by GT3-5).
        with self.assertRaises(payment_service.PaymentError):
            payment_service.activate(purchase.id)
        self.assertFalse(GrandTestAccess.objects.filter(user=self.owner).exists())

    def test_13_duplicate_payment_callback_does_not_duplicate_entitlements(self):
        purchase = Purchase.objects.create(
            user=self.owner, kind='grand_test_package', grand_test_package=self.package,
            original_amount=80, final_amount=80, status='pending',
        )
        for t in self.tests:
            purchase.grand_test_package_items.create(test=t)
        with patch('billing.payment_service._send_grand_test_package_email'):
            payment_service.activate(purchase.id)
        self.assertEqual(GrandTestAccess.objects.filter(user=self.owner).count(), 2)

        # A repeated "successful payment" callback on the same purchase —
        # activate()'s own existing status guard (unmodified) rejects it.
        with self.assertRaises(payment_service.PaymentError):
            payment_service.activate(purchase.id)
        self.assertEqual(GrandTestAccess.objects.filter(user=self.owner).count(), 2)  # unchanged


class ExistingEntitlementAndDuplicationTests(TestCase):
    """Test 14 — already owning GT-I individually, then buying a package
    containing GT-I, must not create a second active access record."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt5_dup', email='gt5_dup@example.com', password='pw')

    def test_14_owning_one_test_then_buying_a_package_containing_it_does_not_duplicate(self):
        now = timezone.now()
        gt1 = _mkgrand('GT-I', now + timezone.timedelta(days=1), now + timezone.timedelta(days=1, hours=3))
        gt2 = _mkgrand('GT-II', now + timezone.timedelta(days=2), now + timezone.timedelta(days=2, hours=3))

        individual_purchase = Purchase.objects.create(
            user=self.student, kind='grand_test', grand_test=gt1, original_amount=50, final_amount=50, status='pending',
        )
        with patch('billing.payment_service._send_grand_test_email'):
            payment_service.activate(individual_purchase.id)
        original_access = GrandTestAccess.objects.get(user=self.student, test=gt1)
        original_password = original_access.password
        original_granted_at = original_access.granted_at

        package = _mkpackage([gt1, gt2], price=Decimal('80'))
        package_purchase = Purchase.objects.create(
            user=self.student, kind='grand_test_package', grand_test_package=package,
            original_amount=80, final_amount=80, status='pending',
        )
        package_purchase.grand_test_package_items.create(test=gt1)
        package_purchase.grand_test_package_items.create(test=gt2)
        with patch('billing.payment_service._send_grand_test_package_email') as mock_email:
            payment_service.activate(package_purchase.id)

        # Still exactly ONE access row for gt1 — the DB-level unique_together
        # already guarantees this; also confirmed untouched (not re-stamped):
        self.assertEqual(GrandTestAccess.objects.filter(user=self.student, test=gt1).count(), 1)
        gt1_access = GrandTestAccess.objects.get(user=self.student, test=gt1)
        self.assertEqual(gt1_access.password, original_password)
        self.assertEqual(gt1_access.granted_at, original_granted_at)
        # gt2 (genuinely new) IS activated normally:
        self.assertTrue(GrandTestAccess.objects.filter(user=self.student, test=gt2).exists())
        # Order history preserved for BOTH items, even the untouched one:
        self.assertEqual(package_purchase.grand_test_package_items.count(), 2)
        # Only the genuinely-new item triggered the confirmation email:
        emailed_tests = {a.test_id for a in mock_email.call_args[0][1]}
        self.assertEqual(emailed_tests, {gt2.id})


class CouponTests(APITestCase):
    """Test 15 + §45 — existing coupon engine, exercised against a package."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt5_coupon', email='gt5_coupon@example.com', password='pw')
        self.client.force_authenticate(user=self.student)
        now = timezone.now()
        self.tests = [_mkgrand(f'GT-{i}', now + timezone.timedelta(days=i), now + timezone.timedelta(days=i, hours=3)) for i in range(1, 3)]
        for t in self.tests:
            t.assigned_students.add(self.student)
        self.package = _mkpackage(self.tests, price=Decimal('100'))

    def _buy(self, coupon_code=''):
        payload = {'kind': 'grand_test_package', 'grand_test_package_id': self.package.id}
        if coupon_code:
            payload['coupon_code'] = coupon_code
        return self.client.post('/api/purchases/', payload)

    def test_no_coupon_charges_full_package_price(self):
        resp = self._buy()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(float(resp.data['final_amount']), 100.0)

    def test_valid_coupon_discounts_the_flat_package_price(self):
        Coupon.objects.create(code='PKG10', discount_type='percentage', discount_value=10, applies_to='grand_test')
        resp = self._buy('PKG10')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(float(resp.data['final_amount']), 90.0)
        self.assertEqual(float(resp.data['original_amount']), 100.0)  # the package's flat price, not summed tests

    def test_invalid_coupon_code_is_rejected(self):
        resp = self._buy('NOSUCHCODE')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'pricing_error')

    def test_expired_coupon_is_rejected(self):
        Coupon.objects.create(
            code='OLDPKG', discount_type='percentage', discount_value=10, applies_to='grand_test',
            expiry_date=(timezone.now() - timezone.timedelta(days=1)).date(),
        )
        resp = self._buy('OLDPKG')
        self.assertEqual(resp.status_code, 400)

    def test_coupon_not_applicable_to_grand_test_is_rejected(self):
        Coupon.objects.create(code='QBANKONLY', discount_type='percentage', discount_value=10, applies_to='qbank')
        resp = self._buy('QBANKONLY')
        self.assertEqual(resp.status_code, 400)

    def test_coupon_scoped_to_a_different_course_does_not_apply(self):
        from courses.models import Course

        other_course = Course.objects.create(name='Other Course', prefix='OTH')
        coupon = Coupon.objects.create(code='COURSESCOPED', discount_type='percentage', discount_value=10, applies_to='grand_test')
        coupon.courses.add(other_course)  # package's tests have no courses at all -> unscoped test = matches any coupon per applies_to_course's own leniency rule
        resp = self._buy('COURSESCOPED')
        # Unscoped tests (Test.courses blank) match any coupon by the
        # existing, unchanged applies_to_course leniency rule — confirming
        # GT3-5 didn't accidentally tighten this for packages.
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)


class RefundTests(TestCase):
    """Test §46 — the real, existing refund flow reverses every entitlement
    a package purchase actually granted, and nothing else."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt5_refund', email='gt5_refund@example.com', password='pw')

    def test_refunding_a_package_purchase_revokes_all_its_entitlements(self):
        now = timezone.now()
        tests = [_mkgrand(f'GT-{i}', now + timezone.timedelta(days=i), now + timezone.timedelta(days=i, hours=3)) for i in range(1, 4)]
        package = _mkpackage(tests, price=Decimal('120'))
        purchase = Purchase.objects.create(
            user=self.student, kind='grand_test_package', grand_test_package=package,
            original_amount=120, final_amount=120, status='pending',
        )
        for t in tests:
            purchase.grand_test_package_items.create(test=t)
        with patch('billing.payment_service._send_grand_test_package_email'):
            payment_service.activate(purchase.id)

        self.assertEqual(GrandTestAccess.objects.filter(user=self.student, revoked_at__isnull=True).count(), 3)
        self.assertEqual(PurchaseEntitlementGrant.objects.filter(purchase=purchase).count(), 3)

        payment_service.refund(purchase.id, 'Package refund test')

        for t in tests:
            self.assertIsNone(get_grand_test_access(self.student, t))  # revoked, per get_grand_test_access's own filter
        # Rows preserved (audit trail), just revoked:
        self.assertEqual(GrandTestAccess.objects.filter(test__in=tests, revoked_at__isnull=False).count(), 3)

    def test_refund_does_not_touch_an_individually_purchased_test_also_in_a_later_package(self):
        """A package refund must only reverse what THIS purchase granted —
        an entitlement the student got some other way stays untouched even
        if it's for one of the package's own tests."""
        now = timezone.now()
        gt1 = _mkgrand('GT-I', now + timezone.timedelta(days=1), now + timezone.timedelta(days=1, hours=3))
        gt2 = _mkgrand('GT-II', now + timezone.timedelta(days=2), now + timezone.timedelta(days=2, hours=3))
        individual_purchase = Purchase.objects.create(
            user=self.student, kind='grand_test', grand_test=gt1, original_amount=50, final_amount=50, status='pending',
        )
        with patch('billing.payment_service._send_grand_test_email'):
            payment_service.activate(individual_purchase.id)

        package = _mkpackage([gt1, gt2], price=Decimal('80'))
        package_purchase = Purchase.objects.create(
            user=self.student, kind='grand_test_package', grand_test_package=package,
            original_amount=80, final_amount=80, status='pending',
        )
        package_purchase.grand_test_package_items.create(test=gt1)
        package_purchase.grand_test_package_items.create(test=gt2)
        with patch('billing.payment_service._send_grand_test_package_email'):
            payment_service.activate(package_purchase.id)
        # gt1 was never re-granted by the package (§22 guard) — no grant row for it under package_purchase.
        self.assertFalse(PurchaseEntitlementGrant.objects.filter(purchase=package_purchase, grand_test_access__test=gt1).exists())

        payment_service.refund(package_purchase.id, 'Refund package only')

        self.assertIsNotNone(get_grand_test_access(self.student, gt1))  # untouched — the package never granted it
        self.assertIsNone(get_grand_test_access(self.student, gt2))  # this one WAS granted by the package, and IS reversed


class NonGrandTestMarketplaceUnaffectedTests(APITestCase):
    """Test 16 — existing subscription/combo/coupon purchase flows are
    completely unchanged by GT3-5."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt5_other_products', email='gt5_other_products@example.com', password='pw')
        self.client.force_authenticate(user=self.student)

    def test_subscription_purchase_flow_unaffected(self):
        from billing.models import SubscriptionPlan
        from courses.models import Course

        course = Course.objects.create(name='Unaffected Course', prefix='UNA')
        plan = SubscriptionPlan.objects.create(
            course=course, product_type='qbank', name='QBank Monthly', price=500,
            duration_value=1, duration_unit='months',
        )
        resp = self.client.post('/api/purchases/', {'kind': 'subscription', 'plan_id': plan.id})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(float(resp.data['final_amount']), 500.0)
