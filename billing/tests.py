import io
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APITestCase

from billing import payment_service
from billing.models import Coupon, NotificationLog, PaymentAuditLog, PaymentMethod, Purchase, Subscription
from billing.payment_providers import ManualQRProvider
from courses.models import Course
from marketplace.models import CourseEnrollment, TeacherCourse
from tests_app.models import Test

User = get_user_model()


def _png_bytes():
    buf = io.BytesIO()
    Image.new('RGB', (2, 2), color='white').save(buf, format='PNG')
    buf.seek(0)
    return buf.read()


def _valid_screenshot(name='proof.png'):
    return SimpleUploadedFile(name, _png_bytes(), content_type='image/png')


@override_settings(MEDIA_GCS_PRIVATE_BUCKET='test-private-bucket')
class BillingTestCase(APITestCase):
    """Shared fixtures for every test below — a staff verifier, a student,
    a paid subscription plan, and an active payment method. GCS calls
    (upload_bytes/signed_url) are mocked per-test where needed — this override
    only ensures a non-empty bucket name is stored, matching production."""

    def setUp(self):
        cache.clear()  # DRF's throttle counters live in Django's cache, not the DB — TestCase resets the DB per test but not this
        self.staff = User.objects.create_user(
            username='staff1', email='staff1@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(username='student1', email='student1@example.com', password='pw12345')
        self.course = Course.objects.create(name='CEE-MD Ayurveda', prefix='AYU')
        self.plan = self._make_plan()
        self.method = PaymentMethod.objects.create(name='Fonepay', provider_type='fonepay')
        self.client.force_authenticate(user=self.student)

    def _make_plan(self, **overrides):
        from billing.models import SubscriptionPlan

        defaults = dict(course=self.course, product_type='qbank', name='3 Month QBank', price=500)
        defaults.update(overrides)
        return SubscriptionPlan.objects.create(**defaults)

    def _create_purchase(self, **overrides):
        resp = self.client.post('/api/purchases/', {'kind': 'subscription', 'plan_id': self.plan.id, **overrides})
        assert resp.status_code == 201, resp.data
        return resp.data

    def _submit(self, purchase_id, reference='TXN-001', **extra):
        with patch('billing.screenshot_storage.upload_bytes'):
            payload = {
                'payment_method': self.method.id, 'payment_reference': reference,
                'payment_screenshot': _valid_screenshot(), **extra,
            }
            return self.client.post(f'/api/purchases/{purchase_id}/submit-payment/', payload, format='multipart')


class SuccessfulSubmissionTests(BillingTestCase):
    def test_checkout_creates_order_awaiting_payment(self):
        data = self._create_purchase()
        self.assertEqual(data['status'], 'unpaid')
        self.assertEqual(data['final_amount'], '500.00')
        self.assertTrue(data['order_id'].startswith('HM-'))
        self.assertIsNotNone(data['expires_at'])

    def test_submit_payment_succeeds_and_moves_to_pending(self):
        purchase = self._create_purchase()
        resp = self._submit(purchase['id'])
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['status'], 'pending')
        self.assertTrue(resp.data['has_screenshot'])
        entry = PaymentAuditLog.objects.get(purchase_id=purchase['id'], action='submitted')
        self.assertEqual(entry.new_status, 'pending')
        self.assertTrue(NotificationLog.objects.filter(purchase_id=purchase['id'], notification_type='payment_submitted').exists())


class DuplicateReferenceTests(BillingTestCase):
    def test_duplicate_reference_on_pending_purchase_is_rejected(self):
        first = self._create_purchase()
        self._submit(first['id'], reference='TXN-SAME')

        second = self._create_purchase()
        resp = self._submit(second['id'], reference='TXN-SAME')

        self.assertEqual(resp.status_code, 400)
        self.assertIn('already used', resp.data['detail'])

    def test_duplicate_reference_on_approved_purchase_is_rejected(self):
        first = self._create_purchase()
        self._submit(first['id'], reference='TXN-APPROVED')
        payment_service.activate(first['id'], actor=self.staff)

        second = self._create_purchase()
        resp = self._submit(second['id'], reference='TXN-APPROVED')

        self.assertEqual(resp.status_code, 400)


class ClientCannotModifyAmountTests(BillingTestCase):
    def test_client_supplied_amount_is_ignored(self):
        resp = self.client.post('/api/purchases/', {
            'kind': 'subscription', 'plan_id': self.plan.id,
            'final_amount': '1', 'original_amount': '1', 'amount': '1',
        })
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['final_amount'], '500.00')

    def test_purchase_serializer_treats_amount_fields_as_read_only(self):
        purchase = self._create_purchase()
        resp = self.client.get(f'/api/purchases/{purchase["id"]}/')
        self.assertEqual(resp.data['final_amount'], '500.00')


class RejectionTests(BillingTestCase):
    def test_reject_requires_reason(self):
        purchase = self._create_purchase()
        self._submit(purchase['id'])
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(f'/api/purchases/{purchase["id"]}/reject/', {})
        self.assertEqual(resp.status_code, 400)

    def test_rejected_payment_does_not_activate_access(self):
        purchase = self._create_purchase()
        self._submit(purchase['id'])
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(f'/api/purchases/{purchase["id"]}/reject/', {'admin_note': 'Amount mismatch'})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'rejected')
        self.assertFalse(Subscription.objects.filter(user=self.student).exists())
        entry = PaymentAuditLog.objects.get(purchase_id=purchase['id'], action='rejected')
        self.assertEqual(entry.reason, 'Amount mismatch')


class ApprovalActivatesAccessTests(BillingTestCase):
    def test_approve_activates_subscription_audits_and_notifies(self):
        purchase = self._create_purchase()
        self._submit(purchase['id'])
        self.client.force_authenticate(user=self.staff)

        resp = self.client.post(f'/api/purchases/{purchase["id"]}/approve/', {})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'approved')
        sub = Subscription.objects.get(user=self.student, course=self.course, product_type='qbank')
        self.assertTrue(sub.is_current)
        entry = PaymentAuditLog.objects.get(purchase_id=purchase['id'], action='approved')
        self.assertEqual(entry.previous_status, 'pending')
        self.assertEqual(entry.actor, self.staff)
        self.assertTrue(NotificationLog.objects.filter(purchase_id=purchase['id'], notification_type='payment_approved').exists())

    def test_free_purchase_auto_approves_without_verification(self):
        free_plan = self._make_plan(name='Free QBank', price=0)
        resp = self.client.post('/api/purchases/', {'kind': 'subscription', 'plan_id': free_plan.id})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['status'], 'approved')
        self.assertTrue(Subscription.objects.filter(user=self.student, plan=free_plan).exists())


class ExpiredPaymentTests(BillingTestCase):
    def test_expired_order_blocks_submission(self):
        purchase = self._create_purchase()
        Purchase.objects.filter(pk=purchase['id']).update(expires_at=timezone.now() - timezone.timedelta(minutes=1))

        resp = self._submit(purchase['id'])

        self.assertEqual(resp.status_code, 400)
        self.assertIn('expired', resp.data['detail'])

    def test_cron_expires_only_truly_stale_orders(self):
        stale = self._create_purchase()
        Purchase.objects.filter(pk=stale['id']).update(expires_at=timezone.now() - timezone.timedelta(minutes=1))
        fresh = self._create_purchase()

        resp = self.client.post(
            '/api/cron/expire-stale-payments/', {}, HTTP_X_CRON_SECRET='dev-cron-secret-change-me',
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['expired'], 1)
        self.assertEqual(Purchase.objects.get(pk=stale['id']).status, 'expired')
        self.assertEqual(Purchase.objects.get(pk=fresh['id']).status, 'unpaid')

    def test_expire_is_idempotent(self):
        purchase = self._create_purchase()
        Purchase.objects.filter(pk=purchase['id']).update(expires_at=timezone.now() - timezone.timedelta(minutes=1))

        payment_service.expire(purchase['id'])
        payment_service.expire(purchase['id'])

        self.assertEqual(PaymentAuditLog.objects.filter(purchase_id=purchase['id'], action='expired').count(), 1)

    def test_expired_purchase_cannot_be_approved_without_a_fresh_order(self):
        purchase = self._create_purchase()
        Purchase.objects.filter(pk=purchase['id']).update(status='expired')

        with self.assertRaises(payment_service.PaymentError):
            payment_service.activate(purchase['id'], actor=self.staff)


class DuplicateApprovalTests(BillingTestCase):
    def test_second_approve_is_rejected(self):
        purchase = self._create_purchase()
        self._submit(purchase['id'])
        payment_service.activate(purchase['id'], actor=self.staff)

        with self.assertRaises(payment_service.PaymentError):
            payment_service.activate(purchase['id'], actor=self.staff)

    def test_coupon_usage_count_increments_exactly_once(self):
        coupon = Coupon.objects.create(code='SAVE10', discount_type='percentage', discount_value=10, auto_apply=False)
        resp = self.client.post('/api/purchases/', {
            'kind': 'subscription', 'plan_id': self.plan.id, 'coupon_code': 'SAVE10',
        })
        purchase_id = resp.data['id']
        self._submit(purchase_id)

        payment_service.activate(purchase_id, actor=self.staff)
        try:
            payment_service.activate(purchase_id, actor=self.staff)
        except payment_service.PaymentError:
            pass

        coupon.refresh_from_db()
        self.assertEqual(coupon.usage_count, 1)

    def test_referrer_wallet_credited_exactly_once_across_two_referred_purchases(self):
        referrer = User.objects.create_user(username='ref1', email='ref1@example.com', password='pw12345')
        referred_a = User.objects.create_user(
            username='refd_a', email='refd_a@example.com', password='pw12345', referred_by=referrer,
        )
        referred_b = User.objects.create_user(
            username='refd_b', email='refd_b@example.com', password='pw12345', referred_by=referrer,
        )
        start_balance = referrer.wallet_balance

        for student in (referred_a, referred_b):
            self.client.force_authenticate(user=student)
            purchase = self._create_purchase()
            self._submit(purchase['id'], reference=f'TXN-{student.username}')
            payment_service.activate(purchase['id'], actor=self.staff)

        referrer.refresh_from_db()
        self.assertEqual(referrer.wallet_balance, start_balance + 200)  # 100 per referred purchase, twice


class NonStaffAccessTests(BillingTestCase):
    def test_non_staff_cannot_approve(self):
        purchase = self._create_purchase()
        self._submit(purchase['id'])
        resp = self.client.post(f'/api/purchases/{purchase["id"]}/approve/', {})
        self.assertEqual(resp.status_code, 403)

    def test_non_staff_cannot_reject(self):
        purchase = self._create_purchase()
        self._submit(purchase['id'])
        resp = self.client.post(f'/api/purchases/{purchase["id"]}/reject/', {'admin_note': 'no'})
        self.assertEqual(resp.status_code, 403)


class MaliciousFileUploadTests(BillingTestCase):
    @patch('billing.screenshot_storage.upload_bytes')
    def test_non_image_file_renamed_as_jpg_is_rejected(self, mock_upload):
        purchase = self._create_purchase()
        fake = SimpleUploadedFile('receipt.jpg', b'#!/bin/sh\necho not-an-image', content_type='image/jpeg')
        resp = self.client.post(
            f'/api/purchases/{purchase["id"]}/submit-payment/',
            {'payment_method': self.method.id, 'payment_reference': 'TXN-BAD', 'payment_screenshot': fake},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 400)
        mock_upload.assert_not_called()

    @patch('billing.screenshot_storage.upload_bytes')
    def test_oversized_screenshot_is_rejected(self, mock_upload):
        purchase = self._create_purchase()
        big = SimpleUploadedFile('big.png', _png_bytes() + b'\x00' * (6 * 1024 * 1024), content_type='image/png')
        resp = self.client.post(
            f'/api/purchases/{purchase["id"]}/submit-payment/',
            {'payment_method': self.method.id, 'payment_reference': 'TXN-BIG', 'payment_screenshot': big},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 400)
        mock_upload.assert_not_called()

    @patch('billing.screenshot_storage.upload_bytes')
    def test_disallowed_but_valid_image_format_is_rejected(self, mock_upload):
        purchase = self._create_purchase()
        buf = io.BytesIO()
        Image.new('RGB', (2, 2)).save(buf, format='BMP')
        bmp = SimpleUploadedFile('proof.bmp', buf.getvalue(), content_type='image/bmp')
        resp = self.client.post(
            f'/api/purchases/{purchase["id"]}/submit-payment/',
            {'payment_method': self.method.id, 'payment_reference': 'TXN-BMP', 'payment_screenshot': bmp},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 400)
        mock_upload.assert_not_called()


class ScreenshotAccessTests(BillingTestCase):
    @patch('billing.screenshot_storage.signed_url', return_value='https://signed.example/proof.png')
    def test_owner_can_view_own_screenshot(self, mock_signed):
        purchase = self._create_purchase()
        self._submit(purchase['id'])
        resp = self.client.get(f'/api/purchases/{purchase["id"]}/screenshot/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['url'], 'https://signed.example/proof.png')

    @patch('billing.screenshot_storage.signed_url', return_value='https://signed.example/proof.png')
    def test_other_student_cannot_view_screenshot(self, mock_signed):
        purchase = self._create_purchase()
        self._submit(purchase['id'])
        other = User.objects.create_user(username='other1', email='other1@example.com', password='pw12345')
        self.client.force_authenticate(user=other)
        resp = self.client.get(f'/api/purchases/{purchase["id"]}/screenshot/')
        # 404, not 403 — PurchaseViewSet.get_queryset() already scopes a
        # non-staff user to their own purchases, so another student's order
        # doesn't even resolve via get_object(); this also avoids leaking
        # "this order exists" to a non-owner the way a 403 would.
        self.assertEqual(resp.status_code, 404)
        mock_signed.assert_not_called()

    @patch('billing.screenshot_storage.signed_url', return_value='https://signed.example/proof.png')
    def test_staff_can_view_any_screenshot(self, mock_signed):
        purchase = self._create_purchase()
        self._submit(purchase['id'])
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get(f'/api/purchases/{purchase["id"]}/screenshot/')
        self.assertEqual(resp.status_code, 200)


class ResubmissionFlowTests(BillingTestCase):
    def test_resubmission_round_trip(self):
        purchase = self._create_purchase()
        self._submit(purchase['id'], reference='TXN-TYPO')
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(
            f'/api/purchases/{purchase["id"]}/request-resubmission/', {'admin_note': 'Reference looks cropped, please resend.'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'resubmission_requested')

        self.client.force_authenticate(user=self.student)
        resp = self._submit(purchase['id'], reference='TXN-FIXED')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'pending')
        self.assertEqual(resp.data['admin_note'], '')


class TeacherCoursePurchaseTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.teacher = User.objects.create_user(username='teacher1', email='teacher1@example.com', password='pw12345')
        self.course_product = TeacherCourse.objects.create(
            teacher=self.teacher, title='Complete Human Anatomy', price=999, status='approved',
            access_duration_type='lifetime',
        )

    @patch('billing.screenshot_storage.upload_bytes')
    def test_purchase_activates_course_enrollment(self, mock_upload):
        resp = self.client.post('/api/purchases/', {'kind': 'teacher_course', 'teacher_course_id': self.course_product.id})
        self.assertEqual(resp.status_code, 201)
        purchase_id = resp.data['id']
        self.client.post(
            f'/api/purchases/{purchase_id}/submit-payment/',
            {'payment_method': self.method.id, 'payment_reference': 'TXN-COURSE', 'payment_screenshot': _valid_screenshot()},
            format='multipart',
        )
        payment_service.activate(purchase_id, actor=self.staff)

        enrollment = CourseEnrollment.objects.get(user=self.student, course=self.course_product)
        self.assertTrue(enrollment.is_active)
        self.assertIsNone(enrollment.expires_at)

    def test_cannot_repurchase_an_active_enrollment(self):
        CourseEnrollment.objects.create(user=self.student, course=self.course_product, source='admin_grant')
        resp = self.client.post('/api/purchases/', {'kind': 'teacher_course', 'teacher_course_id': self.course_product.id})
        self.assertEqual(resp.status_code, 400)


class PastYearQuestionsAccessTests(BillingTestCase):
    def test_pro_pyq_test_blocks_start_without_membership(self):
        pyq_test = Test.objects.create(title='IOM 2080', exam_type='pyq', is_pro=True, university='IOM')
        resp = self.client.post(f'/api/tests/{pyq_test.id}/start/', {})
        self.assertEqual(resp.status_code, 402)

    def test_pyq_membership_unlocks_pro_test(self):
        from billing.models import SubscriptionPlan

        pyq_plan = self._make_plan(product_type='pyq', name='PYQ 1 Year')
        pyq_test = Test.objects.create(title='IOM 2080', exam_type='pyq', is_pro=True, university='IOM')
        Subscription.objects.create(user=self.student, plan=pyq_plan, course=self.course, product_type='pyq')

        resp = self.client.post(f'/api/tests/{pyq_test.id}/start/', {})
        self.assertEqual(resp.status_code, 201)

    def test_free_pyq_test_never_blocked(self):
        pyq_test = Test.objects.create(title='IOM 2075 (free)', exam_type='pyq', is_pro=False, university='IOM')
        resp = self.client.post(f'/api/tests/{pyq_test.id}/start/', {})
        self.assertEqual(resp.status_code, 201)


class CouponMultiCourseTests(BillingTestCase):
    """Coupon.course was a single nullable FK ("blank = every course"); it's
    now a ManyToMany so one code can scope to several specific courses at
    once (e.g. CEE-MBBS + CEE-BDS but not others) instead of admins needing
    a separate code per course."""

    def setUp(self):
        super().setUp()
        self.other_course = Course.objects.create(name='CEE-MD Pharmacy', prefix='PHM')
        self.other_plan = self._make_plan(course=self.other_course, name='3 Month QBank (Pharmacy)')
        self.unscoped_course = Course.objects.create(name='CEE-BDS', prefix='BDS')
        self.unscoped_plan = self._make_plan(course=self.unscoped_course, name='3 Month QBank (BDS)')

    def test_coupon_scoped_to_two_courses_applies_to_both(self):
        coupon = Coupon.objects.create(code='TWOCOURSE', discount_type='percentage', discount_value=10)
        coupon.courses.add(self.course, self.other_course)

        self.assertTrue(coupon.applies_to_course(self.plan, None))
        self.assertTrue(coupon.applies_to_course(self.other_plan, None))

    def test_coupon_scoped_to_two_courses_does_not_apply_to_a_third(self):
        coupon = Coupon.objects.create(code='TWOCOURSE2', discount_type='percentage', discount_value=10)
        coupon.courses.add(self.course, self.other_course)

        self.assertFalse(coupon.applies_to_course(self.unscoped_plan, None))

    def test_coupon_with_no_courses_applies_everywhere(self):
        coupon = Coupon.objects.create(code='SITEWIDE', discount_type='percentage', discount_value=10)

        self.assertTrue(coupon.applies_to_course(self.plan, None))
        self.assertTrue(coupon.applies_to_course(self.other_plan, None))
        self.assertTrue(coupon.applies_to_course(self.unscoped_plan, None))

    def test_apply_coupon_endpoint_rejects_a_course_not_in_scope(self):
        coupon = Coupon.objects.create(code='TWOCOURSE3', discount_type='percentage', discount_value=10, applies_to='qbank')
        coupon.courses.add(self.course, self.other_course)

        resp = self.client.post('/api/coupons/apply/', {
            'code': 'TWOCOURSE3', 'kind': 'subscription', 'plan_id': self.unscoped_plan.id,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.data['valid'])

    def test_apply_coupon_endpoint_accepts_a_course_in_scope(self):
        coupon = Coupon.objects.create(code='TWOCOURSE4', discount_type='percentage', discount_value=10, applies_to='qbank')
        coupon.courses.add(self.course, self.other_course)

        resp = self.client.post('/api/coupons/apply/', {
            'code': 'TWOCOURSE4', 'kind': 'subscription', 'plan_id': self.other_plan.id,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['valid'])

    def test_deleting_a_course_leaves_coupon_scoped_to_the_rest(self):
        coupon = Coupon.objects.create(code='TWOCOURSE5', discount_type='percentage', discount_value=10)
        coupon.courses.add(self.course, self.other_course)

        self.other_course.delete()
        coupon.refresh_from_db()

        self.assertEqual(list(coupon.courses.values_list('id', flat=True)), [self.course.id])
        self.assertTrue(coupon.applies_to_course(self.plan, None))

    def test_serializer_exposes_course_names_for_multiple_courses(self):
        from billing.serializers import CouponSerializer

        coupon = Coupon.objects.create(code='TWOCOURSE6', discount_type='percentage', discount_value=10)
        coupon.courses.add(self.course, self.other_course)

        data = CouponSerializer(coupon).data
        self.assertEqual(set(data['course_names']), {self.course.name, self.other_course.name})
        self.assertEqual(set(data['courses']), {self.course.id, self.other_course.id})

    def test_pyq_is_a_valid_applies_to_choice(self):
        coupon = Coupon.objects.create(code='PYQCODE', discount_type='percentage', discount_value=10, applies_to='pyq')
        self.assertEqual(coupon.applies_to, 'pyq')
        self.assertTrue(coupon.applies_to_product('pyq'))
        self.assertFalse(coupon.applies_to_product('qbank'))
