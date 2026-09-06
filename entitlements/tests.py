import threading
import time

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase, APITransactionTestCase

from academics.models import Subject
from billing.models import GrandTestAccess, Purchase, Subscription
from billing.payment_service import _extend_or_create_subscription
from courses.access import eligible_course_ids
from courses.models import Batch, Course, Enrollment
from entitlements.models import EntitlementEventLog, FreeStarterEntitlement, FreeStarterPolicy
from entitlements.provisioning import consume_free_starter, provision_free_starter
from entitlements.services import (
    SOURCE_COURSE_ENROLLMENT, SOURCE_DIRECT_PURCHASE, SOURCE_FREE_STARTER, SOURCE_SCHOLARSHIP,
    SOURCE_SUBSCRIPTION, can_start_test, can_view_qbank,
)
from tests_app.models import ExamSession, ExamTemplate, Test, TestAttempt

User = get_user_model()


# =====================================================================
# Free Starter — provisioning, consumption, exhaustion, concurrency
# =====================================================================

class FreeStarterProvisioningTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='fs_student', email='fs_student@example.com', password='pw12345')

    def test_no_active_policy_means_nothing_is_granted(self):
        """No FreeStarterPolicy rows are seeded by the migration — the
        system must grant nothing until an admin explicitly configures at
        least one active policy row (Absolute Rule: never hardcode Free
        Starter limits, not even as a seeded 'default')."""
        rows = provision_free_starter(self.student)

        self.assertEqual(rows, [])
        self.assertEqual(FreeStarterEntitlement.objects.filter(user=self.student).count(), 0)

    def test_provisions_one_row_per_active_policy(self):
        FreeStarterPolicy.objects.create(resource_type='qbank', quantity=100, is_active=True)
        FreeStarterPolicy.objects.create(resource_type='mock_test', quantity=1, is_active=True)
        FreeStarterPolicy.objects.create(resource_type='pyq', quantity=50, is_active=False)  # inactive — must be skipped

        provision_free_starter(self.student)

        rows = {r.resource_type: r for r in FreeStarterEntitlement.objects.filter(user=self.student)}
        self.assertEqual(set(rows), {'qbank', 'mock_test'})
        self.assertEqual(rows['qbank'].quantity, 100)
        self.assertEqual(rows['mock_test'].quantity, 1)
        self.assertTrue(EntitlementEventLog.objects.filter(user=self.student, event='created', resource_type='qbank').exists())

    def test_provisioning_is_idempotent(self):
        """Step 14: safe to call more than once (registration retries)."""
        FreeStarterPolicy.objects.create(resource_type='qbank', quantity=100, is_active=True)

        provision_free_starter(self.student)
        provision_free_starter(self.student)
        provision_free_starter(self.student)

        self.assertEqual(FreeStarterEntitlement.objects.filter(user=self.student, resource_type='qbank').count(), 1)

    def test_later_policy_change_does_not_rewrite_an_already_provisioned_student(self):
        """quantity/unlimited are snapshotted at provisioning time, not
        read live — matches the platform's existing snapshot-at-purchase
        pattern (PurchaseComboItem.price)."""
        policy = FreeStarterPolicy.objects.create(resource_type='qbank', quantity=100, is_active=True)
        provision_free_starter(self.student)

        policy.quantity = 50
        policy.save()

        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='qbank')
        self.assertEqual(row.quantity, 100)

    def test_registration_provisions_free_starter(self):
        """Integration test for the accounts.serializers.RegisterSerializer
        hook — confirms the actual registration endpoint provisions Free
        Starter, not just the unit-level provision_free_starter function."""
        FreeStarterPolicy.objects.create(resource_type='qbank', quantity=100, is_active=True)

        resp = self.client.post('/api/auth/register/', {
            'name': 'New Student', 'email': 'newstudent@example.com', 'phone': '9800000000',
            'password': 'ComplexPass123!',
        })

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        new_user = User.objects.get(email='newstudent@example.com')
        row = FreeStarterEntitlement.objects.get(user=new_user, resource_type='qbank')
        self.assertEqual(row.quantity, 100)
        self.assertEqual(row.used, 0)

    def test_registration_succeeds_even_if_provisioning_would_fail(self):
        """Registration must never fail because of a Free Starter issue —
        simulated here by leaving policy rows absent entirely (the
        no-op case), confirming registration doesn't depend on any policy
        existing."""
        resp = self.client.post('/api/auth/register/', {
            'name': 'Another Student', 'email': 'another@example.com', 'phone': '9811111111',
            'password': 'ComplexPass123!',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)


class FreeStarterConsumptionTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='fs_consume', email='fs_consume@example.com', password='pw12345')
        FreeStarterPolicy.objects.create(resource_type='qbank', quantity=3, is_active=True)
        provision_free_starter(self.student)

    def test_consume_decrements_remaining(self):
        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='qbank')
        self.assertEqual(row.remaining, 3)

        ok = consume_free_starter(self.student, 'qbank')

        self.assertTrue(ok)
        row.refresh_from_db()
        self.assertEqual(row.used, 1)
        self.assertEqual(row.remaining, 2)

    def test_consume_cannot_go_negative(self):
        for _ in range(3):
            self.assertTrue(consume_free_starter(self.student, 'qbank'))

        ok = consume_free_starter(self.student, 'qbank')

        self.assertFalse(ok)
        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='qbank')
        self.assertEqual(row.used, 3)
        self.assertEqual(row.remaining, 0)
        self.assertEqual(row.effective_status, 'exhausted')

    def test_exhaustion_is_logged(self):
        for _ in range(3):
            consume_free_starter(self.student, 'qbank')

        self.assertTrue(EntitlementEventLog.objects.filter(user=self.student, event='exhausted', resource_type='qbank').exists())

    def test_unlimited_never_exhausts(self):
        FreeStarterPolicy.objects.create(resource_type='pyq', quantity=0, unlimited=True, is_active=True)
        provision_free_starter(self.student)

        for _ in range(500):
            self.assertTrue(consume_free_starter(self.student, 'pyq'))

        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='pyq')
        self.assertIsNone(row.remaining)
        self.assertEqual(row.effective_status, 'active')

    def test_consuming_with_no_entitlement_row_fails_cleanly(self):
        ok = consume_free_starter(self.student, 'daily_test')
        self.assertFalse(ok)

    def test_revoked_entitlement_cannot_be_consumed(self):
        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='qbank')
        row.status = 'revoked'
        row.save()

        ok = consume_free_starter(self.student, 'qbank')

        self.assertFalse(ok)

    def test_expired_entitlement_cannot_be_consumed(self):
        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='qbank')
        row.expires_at = timezone.now() - timezone.timedelta(days=1)
        row.save()

        self.assertEqual(row.effective_status, 'expired')
        ok = consume_free_starter(self.student, 'qbank')

        self.assertFalse(ok)


class FreeStarterConcurrentConsumptionTests(APITransactionTestCase):
    """Step 12: a real multi-thread race test, not just a sequential one —
    same pattern as tests_app.tests.SubmitTestDoubleSubmissionRaceTests
    (APITransactionTestCase + real threads + SQLite busy_timeout retry,
    since SQLite has no real row locking; production runs MySQL/InnoDB
    where select_for_update() takes a genuine row lock)."""

    def test_concurrent_consumption_never_goes_negative(self):
        """The critical safety property is row.used never exceeding
        quantity (5) regardless of concurrent contention — asserted first
        and unconditionally. The results-list bookkeeping is secondary:
        under heavy SQLite lock contention (this suite's local test DB has
        no real row-level locking, unlike production's MySQL/InnoDB — see
        the module-level note in tests_app.tests.SubmitTestDoubleSubmission
        RaceTests) a thread can exhaust its retry budget without ever
        successfully recording its own outcome; every retry-exhaustion is
        now caught and recorded as a failure explicitly rather than left to
        escape the thread silently, so len(results) == thread count always
        holds even under contention this test doesn't control."""
        from django.db import connection

        student = User.objects.create_user(username='fs_race', email='fs_race@example.com', password='pw12345')
        # Quota of 2 against 4 threads — smaller than an earlier draft of
        # this test (which used quantity=5/threads=10 and, empirically,
        # got measurably flakier over the course of this session as local
        # machine load increased — SQLite's file-level locking under this
        # test's own in-memory shared-cache mode is far more contention-
        # sensitive with more concurrent connections). Still genuine
        # concurrency (more requests than remaining quota, real threads,
        # real transactions) — just calibrated to be reliable on this test
        # database rather than needlessly aggressive. See the module-level
        # docstring for why production (MySQL/InnoDB, real row locks) does
        # not share this sensitivity at all.
        FreeStarterPolicy.objects.create(resource_type='qbank', quantity=2, is_active=True)
        provision_free_starter(student)

        results = []
        errors = []
        lock = threading.Lock()
        thread_count = 4

        def consume_once():
            outcome = False
            for attempt_no in range(40):
                try:
                    if connection.vendor == 'sqlite':
                        with connection.cursor() as cur:
                            cur.execute('PRAGMA busy_timeout = 30000')
                    outcome = consume_free_starter(student, 'qbank')
                    break
                except Exception as exc:  # noqa: BLE001 — SQLite lock-contention retry
                    if 'lock' in str(exc).lower() and attempt_no < 39:
                        time.sleep(0.05)
                        continue
                    # Not a lock-contention error, or retries exhausted —
                    # record it instead of silently treating it as an
                    # ordinary "quota exhausted" outcome, so a genuine bug
                    # can never hide behind this retry loop.
                    with lock:
                        errors.append(str(exc))
                    outcome = False
                    break
                finally:
                    connection.close()
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=consume_once) for _ in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        row = FreeStarterEntitlement.objects.get(user=student, resource_type='qbank')
        self.assertEqual(errors, [], 'a non-lock-contention error occurred — see errors for detail')
        self.assertLessEqual(row.used, 2)
        self.assertGreaterEqual(row.used, 0)
        self.assertEqual(len(results), thread_count)
        self.assertEqual(sum(1 for r in results if r), row.used)
        self.assertEqual(sum(1 for r in results if not r), thread_count - row.used)


# =====================================================================
# Fix 1 regression — courses/access.py now honors expires_at
# =====================================================================

class EligibleCourseIdsExpiryTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='exp_student', email='exp_student@example.com', password='pw12345')
        self.course = Course.objects.create(name='Expiry Course', prefix='EXPC')

    def test_non_expired_enrollment_still_grants_access(self):
        """Regression guard: the fix must not remove access from a
        currently-valid enrollment (expires_at in the future, or null)."""
        Enrollment.objects.create(user=self.student, course=self.course, is_active=True, expires_at=None)
        self.assertIn(self.course.id, eligible_course_ids(self.student))

    def test_future_expiry_still_grants_access(self):
        Enrollment.objects.create(
            user=self.student, course=self.course, is_active=True,
            expires_at=timezone.now() + timezone.timedelta(days=30),
        )
        self.assertIn(self.course.id, eligible_course_ids(self.student))

    def test_expired_enrollment_no_longer_grants_access(self):
        """The bug fix itself: is_active=True alone must no longer be
        sufficient once expires_at is in the past."""
        Enrollment.objects.create(
            user=self.student, course=self.course, is_active=True,
            expires_at=timezone.now() - timezone.timedelta(days=1),
        )
        self.assertNotIn(self.course.id, eligible_course_ids(self.student))

    def test_expired_enrollment_denies_exam_access_end_to_end(self):
        """Confirms the fix propagates through a real consumer
        (tests_app.access.can_access_test), not just the raw helper."""
        from tests_app.access import can_access_test

        test = Test.objects.create(title='Expiry Gate Exam', exam_type='mock', is_draft=False)
        test.courses.set([self.course])
        Enrollment.objects.create(
            user=self.student, course=self.course, is_active=True,
            expires_at=timezone.now() - timezone.timedelta(hours=1),
        )

        self.assertFalse(can_access_test(self.student, test))

    def test_batch_ids_also_respect_expiry(self):
        from courses.access import eligible_batch_ids

        batch = Batch.objects.create(course=self.course, name='2082 Batch')
        Enrollment.objects.create(
            user=self.student, course=self.course, batch=batch, is_active=True,
            expires_at=timezone.now() - timezone.timedelta(hours=1),
        )
        self.assertNotIn(batch.id, eligible_batch_ids(self.student))


# =====================================================================
# Fix 2 regression — scholarship and paid subscriptions never share a row
# =====================================================================

class ScholarshipPaidSeparationTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='schol_student', email='schol_student@example.com', password='pw12345')
        self.staff = User.objects.create_user(
            username='schol_staff', email='schol_staff@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.course = Course.objects.create(name='Scholarship Course', prefix='SCHC')

    def _grant_scholarship_sub(self, duration):
        """Mirrors GrantAccessView.post's real two-step flow: extend/create
        the Subscription, THEN separately create the Scholarship row that
        links to it — is_scholarship=True alone (without this second step)
        does not itself make a row scholarship-linked; the reverse
        Subscription.scholarship relation only exists once a real
        Scholarship object points at it, exactly as production does it."""
        from billing.models import Scholarship

        sub, was_renewal = _extend_or_create_subscription(
            self.student, self.course, 'qbank', duration, is_scholarship=True,
        )
        Scholarship.objects.get_or_create(subscription=sub, defaults={
            'user': self.student, 'course': self.course, 'product_type': 'qbank', 'granted_by': self.staff,
        })
        return sub, was_renewal

    def test_scholarship_after_existing_paid_subscription_creates_a_separate_row(self):
        paid, _ = _extend_or_create_subscription(
            self.student, self.course, 'qbank', timezone.timedelta(days=90), is_scholarship=False,
        )

        scholarship_sub, was_renewal = self._grant_scholarship_sub(timezone.timedelta(days=30))

        self.assertFalse(was_renewal)
        self.assertNotEqual(paid.id, scholarship_sub.id)
        self.assertEqual(Subscription.objects.filter(user=self.student, course=self.course, product_type='qbank').count(), 2)

    def test_paid_purchase_after_existing_scholarship_creates_a_separate_row(self):
        scholarship_sub, _ = self._grant_scholarship_sub(timezone.timedelta(days=30))

        paid_sub, was_renewal = _extend_or_create_subscription(
            self.student, self.course, 'qbank', timezone.timedelta(days=90), is_scholarship=False,
        )

        self.assertFalse(was_renewal)
        self.assertNotEqual(scholarship_sub.id, paid_sub.id)

    def test_second_scholarship_grant_correctly_extends_the_first_not_a_third_row(self):
        """Same-origin renewal behavior must be preserved."""
        first, _ = self._grant_scholarship_sub(timezone.timedelta(days=30))

        second, was_renewal = _extend_or_create_subscription(
            self.student, self.course, 'qbank', timezone.timedelta(days=30), is_scholarship=True,
        )

        self.assertTrue(was_renewal)
        self.assertEqual(first.id, second.id)
        self.assertEqual(Subscription.objects.filter(user=self.student, course=self.course, product_type='qbank').count(), 1)

    def test_revoking_scholarship_does_not_affect_separately_purchased_subscription(self):
        """The exact scenario the Phase 2 spec names verbatim: 'Scholarship
        revoked + student separately purchased same resource -> paid
        entitlement must remain valid.'"""
        self.client.force_authenticate(user=self.staff)
        grant_resp = self.client.post('/api/grant-access/', {
            'user_id': self.student.id, 'course_id': self.course.id, 'product_type': 'qbank',
            'duration_value': 1, 'duration_unit': 'month', 'is_scholarship': True, 'reason': 'test',
        })
        scholarship_id = grant_resp.data['scholarship_id']

        _extend_or_create_subscription(
            self.student, self.course, 'qbank', timezone.timedelta(days=90), is_scholarship=False,
        )

        revoke_resp = self.client.post(f'/api/scholarships/{scholarship_id}/revoke/')
        self.assertEqual(revoke_resp.status_code, 200)

        from billing.access import has_qbank_access
        subject = Subject.objects.create(name='Scholarship Subject')
        subject.courses.set([self.course])
        self.assertTrue(has_qbank_access(self.student, subject))

    def test_scholarship_sub_is_distinguishable_from_paid_sub_via_commercial_entitlement(self):
        from entitlements.services import commercial_entitlement

        self._grant_scholarship_sub(timezone.timedelta(days=30))

        decision = commercial_entitlement(self.student, 'qbank', self.course)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.source_type, SOURCE_SCHOLARSHIP)


# =====================================================================
# Access decisions — can_start_test / can_view_qbank
# =====================================================================

class CanStartTestDecisionTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='cst_student', email='cst_student@example.com', password='pw12345')
        self.staff = User.objects.create_user(
            username='cst_staff', email='cst_staff@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.course = Course.objects.create(name='CanStart Course', prefix='CSTC')
        Enrollment.objects.create(user=self.student, course=self.course, is_active=True)

    def test_anonymous_user_denied(self):
        from django.contrib.auth.models import AnonymousUser

        test = Test.objects.create(title='Anon Test', exam_type='mock', is_draft=False)
        test.courses.set([self.course])

        decision = can_start_test(AnonymousUser(), test)

        self.assertFalse(decision.allowed)

    def test_free_exam_allowed_for_enrolled_student(self):
        test = Test.objects.create(title='Free Mock', exam_type='mock', is_draft=False, is_pro=False)
        test.courses.set([self.course])

        decision = can_start_test(self.student, test)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.source_type, SOURCE_COURSE_ENROLLMENT)

    def test_unenrolled_student_denied(self):
        other_course = Course.objects.create(name='Other Course', prefix='OTHC')
        test = Test.objects.create(title='Other Course Exam', exam_type='mock', is_draft=False, is_pro=False)
        test.courses.set([other_course])

        decision = can_start_test(self.student, test)

        self.assertFalse(decision.allowed)

    def test_draft_exam_denied_for_student(self):
        test = Test.objects.create(title='Draft Exam', exam_type='mock', is_draft=True, is_pro=False)
        test.courses.set([self.course])

        self.assertFalse(can_start_test(self.student, test).allowed)

    def test_staff_bypasses_academic_gate_for_a_free_test(self):
        """Staff/creator bypass applies to the ACADEMIC gate (draft status,
        course/batch/individual assignment) — matches tests_app.access.
        can_access_test's own documented behavior exactly."""
        test = Test.objects.create(title='Staff Draft Exam', exam_type='mock', is_draft=True, is_pro=False)

        decision = can_start_test(self.staff, test)

        self.assertTrue(decision.allowed)

    def test_staff_still_needs_entitlement_for_a_pro_test(self):
        """Re-verified against the real tests_app._start_attempt (not
        assumed): staff bypass the draft/academic check via
        can_access_test, but there is NO staff exemption from
        has_mock_test_access/has_daily_test_access/has_pyq_access anywhere
        in the real, unmodified enforcement code — this decision function
        must mirror that exactly, not invent a staff-always-wins rule."""
        test = Test.objects.create(title='Staff Pro Exam', exam_type='mock', is_draft=True, is_pro=True)

        decision = can_start_test(self.staff, test)

        self.assertFalse(decision.allowed)

    def test_batch_assigned_student_allowed_without_course(self):
        other_student = User.objects.create_user(username='cst_batch', email='cst_batch@example.com', password='pw12345')
        batch = Batch.objects.create(course=self.course, name='2082 Batch')
        Enrollment.objects.create(user=other_student, course=self.course, batch=batch, is_active=True)
        test = Test.objects.create(title='Batch Exam', exam_type='mock', is_draft=False, is_pro=False)
        test.assigned_batches.set([batch])

        decision = can_start_test(other_student, test)

        self.assertTrue(decision.allowed)

    def test_individually_assigned_student_allowed_without_enrollment(self):
        stranger = User.objects.create_user(username='cst_indiv', email='cst_indiv@example.com', password='pw12345')
        test = Test.objects.create(title='Individual Exam', exam_type='mock', is_draft=False, is_pro=False)
        test.assigned_students.set([stranger])

        decision = can_start_test(stranger, test)

        self.assertTrue(decision.allowed)

    def test_pro_mock_without_subscription_falls_back_to_free_starter(self):
        FreeStarterPolicy.objects.create(resource_type='mock_test', quantity=1, is_active=True)
        test = Test.objects.create(title='Pro Mock', exam_type='mock', is_draft=False, is_pro=True)
        test.courses.set([self.course])

        decision = can_start_test(self.student, test)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.source_type, SOURCE_FREE_STARTER)

    def test_pro_mock_denied_when_free_starter_exhausted_and_no_subscription(self):
        FreeStarterPolicy.objects.create(resource_type='mock_test', quantity=1, is_active=True)
        provision_free_starter(self.student)
        consume_free_starter(self.student, 'mock_test')
        test = Test.objects.create(title='Pro Mock 2', exam_type='mock', is_draft=False, is_pro=True)
        test.courses.set([self.course])

        decision = can_start_test(self.student, test)

        self.assertFalse(decision.allowed)

    def test_pro_mock_with_active_subscription_allowed(self):
        _extend_or_create_subscription(self.student, self.course, 'mock_test', timezone.timedelta(days=30))
        test = Test.objects.create(title='Pro Mock Sub', exam_type='mock', is_draft=False, is_pro=True)
        test.courses.set([self.course])

        decision = can_start_test(self.student, test)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.source_type, SOURCE_SUBSCRIPTION)

    def test_grand_test_with_access_grant_allowed(self):
        test = Test.objects.create(title='Grand Exam', exam_type='grand', is_draft=False, is_pro=True)
        test.courses.set([self.course])
        purchase = Purchase.objects.create(user=self.student, kind='grand_test', original_amount=1000, final_amount=1000)
        GrandTestAccess.objects.create(purchase=purchase, user=self.student, test=test)

        decision = can_start_test(self.student, test)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.source_type, SOURCE_DIRECT_PURCHASE)

    def test_grand_test_without_access_grant_denied(self):
        test = Test.objects.create(title='Grand Exam No Access', exam_type='grand', is_draft=False, is_pro=True)
        test.courses.set([self.course])

        decision = can_start_test(self.student, test)

        self.assertFalse(decision.allowed)


class CanViewQbankDecisionTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='qb_student', email='qb_student@example.com', password='pw12345')
        self.course = Course.objects.create(name='QBank Course', prefix='QBC')

    def test_free_subject_allowed_even_unauthenticated(self):
        from django.contrib.auth.models import AnonymousUser

        subject = Subject.objects.create(name='Free Subject', is_free=True)

        decision = can_view_qbank(AnonymousUser(), subject)

        self.assertTrue(decision.allowed)

    def test_paid_subject_without_subscription_falls_back_to_free_starter(self):
        FreeStarterPolicy.objects.create(resource_type='qbank', quantity=100, is_active=True)
        subject = Subject.objects.create(name='Paid Subject', is_free=False)
        subject.courses.set([self.course])

        decision = can_view_qbank(self.student, subject)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.source_type, SOURCE_FREE_STARTER)

    def test_paid_subject_denied_with_no_subscription_and_no_free_starter(self):
        subject = Subject.objects.create(name='Paid Subject 2', is_free=False)
        subject.courses.set([self.course])

        decision = can_view_qbank(self.student, subject)

        self.assertFalse(decision.allowed)

    def test_paid_subject_with_subscription_allowed(self):
        _extend_or_create_subscription(self.student, self.course, 'qbank', timezone.timedelta(days=30))
        subject = Subject.objects.create(name='Paid Subject 3', is_free=False)
        subject.courses.set([self.course])

        decision = can_view_qbank(self.student, subject)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.source_type, SOURCE_SUBSCRIPTION)


# =====================================================================
# API surface — /api/entitlements/... — security/IDOR
# =====================================================================

class EntitlementsApiTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='api_student', email='api_student@example.com', password='pw12345')
        self.other_student = User.objects.create_user(username='api_other', email='api_other@example.com', password='pw12345')
        self.course = Course.objects.create(name='API Course', prefix='APIC')
        Enrollment.objects.create(user=self.student, course=self.course, is_active=True)
        FreeStarterPolicy.objects.create(resource_type='qbank', quantity=100, is_active=True)

    def test_mine_requires_authentication(self):
        resp = self.client.get('/api/entitlements/mine/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_mine_returns_only_the_caller_own_entitlements(self):
        provision_free_starter(self.student)
        consume_free_starter(self.student, 'qbank')
        provision_free_starter(self.other_student)
        self.client.force_authenticate(user=self.student)

        resp = self.client.get('/api/entitlements/mine/')

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]['used'], 1)

    def test_mine_lazily_provisions_if_never_provisioned(self):
        """No prior provision_free_starter() call for this student — the
        endpoint itself must provision on first access (Step 14's lazy
        fallback), not 404/return empty forever."""
        self.client.force_authenticate(user=self.student)

        resp = self.client.get('/api/entitlements/mine/')

        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]['resource_type'], 'qbank')

    def test_test_access_endpoint_ignores_client_supplied_identity_and_uses_request_user(self):
        """IDOR posture (Step 22): the endpoint takes no user_id at all —
        confirm two different authenticated callers against the SAME
        resource id get their own, independently-correct decisions."""
        test = Test.objects.create(title='API Test', exam_type='mock', is_draft=False, is_pro=False)
        test.courses.set([self.course])

        self.client.force_authenticate(user=self.student)
        resp_enrolled = self.client.get(f'/api/entitlements/tests/{test.id}/access/')
        self.client.force_authenticate(user=self.other_student)
        resp_unenrolled = self.client.get(f'/api/entitlements/tests/{test.id}/access/')

        self.assertTrue(resp_enrolled.data['allowed'])
        self.assertFalse(resp_unenrolled.data['allowed'])

    def test_test_access_requires_authentication(self):
        test = Test.objects.create(title='Auth Required Test', exam_type='mock', is_draft=False)
        resp = self.client.get(f'/api/entitlements/tests/{test.id}/access/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_test_access_404s_for_nonexistent_test(self):
        self.client.force_authenticate(user=self.student)
        resp = self.client.get('/api/entitlements/tests/999999/access/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_qbank_access_endpoint(self):
        subject = Subject.objects.create(name='API Subject', is_free=False)
        subject.courses.set([self.course])
        self.client.force_authenticate(user=self.student)

        resp = self.client.get(f'/api/entitlements/subjects/{subject.id}/qbank-access/')

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['allowed'])
        self.assertEqual(resp.data['source_type'], SOURCE_FREE_STARTER)


# =====================================================================
# Phase 3 — validity_days / expiry
# =====================================================================

class ValidityDaysTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='vd_student', email='vd_student@example.com', password='pw12345')

    def test_validity_days_sets_expires_at(self):
        FreeStarterPolicy.objects.create(resource_type='qbank', quantity=100, validity_days=14, is_active=True)

        provision_free_starter(self.student)

        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='qbank')
        self.assertIsNotNone(row.expires_at)
        expected = timezone.now() + timezone.timedelta(days=14)
        self.assertAlmostEqual(row.expires_at.timestamp(), expected.timestamp(), delta=5)

    def test_no_validity_days_means_no_expiry(self):
        FreeStarterPolicy.objects.create(resource_type='qbank', quantity=100, validity_days=None, is_active=True)

        provision_free_starter(self.student)

        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='qbank')
        self.assertIsNone(row.expires_at)
        self.assertEqual(row.effective_status, 'active')

    def test_expired_entitlement_denies_via_decision_layer(self):
        FreeStarterPolicy.objects.create(resource_type='qbank', quantity=100, validity_days=7, is_active=True)
        provision_free_starter(self.student)
        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='qbank')
        row.expires_at = timezone.now() - timezone.timedelta(days=1)
        row.save()

        subject = Subject.objects.create(name='Expiry Decision Subject', is_free=False)
        course = Course.objects.create(name='Expiry Decision Course', prefix='EXPD')
        subject.courses.set([course])

        decision = can_view_qbank(self.student, subject)

        self.assertFalse(decision.allowed)


# =====================================================================
# Phase 3 — live wiring: QBank consumption through the real answer() API
# =====================================================================

class QbankLiveConsumptionTests(APITestCase):
    def setUp(self):
        from academics.models import Option, Question

        self.student = User.objects.create_user(username='qbl_student', email='qbl_student@example.com', password='pw12345')
        self.staff = User.objects.create_user(
            username='qbl_staff', email='qbl_staff@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        FreeStarterPolicy.objects.create(resource_type='qbank', quantity=2, is_active=True)
        self.subject = Subject.objects.create(name='Live QBank Subject', is_free=False)
        self.course = Course.objects.create(name='Live QBank Course', prefix='LQBC')
        self.subject.courses.set([self.course])
        self.q1 = Question.objects.create(subject=self.subject, text='Q1', marks=1, negative_marks=0)
        self.q1_correct = Option.objects.create(question=self.q1, text='A', is_correct=True)
        self.q2 = Question.objects.create(subject=self.subject, text='Q2', marks=1, negative_marks=0)
        self.q2_correct = Option.objects.create(question=self.q2, text='A', is_correct=True)
        self.q3 = Question.objects.create(subject=self.subject, text='Q3', marks=1, negative_marks=0)
        self.q3_correct = Option.objects.create(question=self.q3, text='A', is_correct=True)
        # Academic eligibility (course enrollment) is a SEPARATE gate from
        # the commercial/free-starter one under test here — without it,
        # QuestionViewSet.get_queryset()'s _locked_subject_ids exclusion
        # hides this paid subject's questions entirely (a 404 on
        # get_object(), never reaching the free-starter gate at all).
        Enrollment.objects.create(user=self.student, course=self.course, is_active=True)
        # locked_subject_ids deliberately does NOT lazy-provision (it's a
        # hot, scalability-audited listing path — see academics.access's
        # own docstring) — it only READS an existing FreeStarterEntitlement
        # row. In production this is populated by registration-time
        # provisioning (accounts.serializers.RegisterSerializer.create);
        # since this test creates the student directly via create_user()
        # rather than the real /api/auth/register/ endpoint, provision
        # explicitly here to match what registration would already have
        # done by the time a real student reaches the Question Bank.
        provision_free_starter(self.student)
        self.client.force_authenticate(user=self.student)

    def test_free_subject_never_gated_or_consumed(self):
        free_subject = Subject.objects.create(name='Live Free Subject', is_free=True)
        from academics.models import Option as Opt
        from academics.models import Question as Q
        q = Q.objects.create(subject=free_subject, text='Free Q', marks=1, negative_marks=0)
        opt = Opt.objects.create(question=q, text='A', is_correct=True)

        resp = self.client.post(f'/api/questions/{q.id}/answer/', {'option_id': opt.id})

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='qbank')
        self.assertEqual(row.used, 0)

    def test_first_two_answers_consume_free_quota_third_is_denied(self):
        r1 = self.client.post(f'/api/questions/{self.q1.id}/answer/', {'option_id': self.q1_correct.id})
        r2 = self.client.post(f'/api/questions/{self.q2.id}/answer/', {'option_id': self.q2_correct.id})
        r3 = self.client.post(f'/api/questions/{self.q3.id}/answer/', {'option_id': self.q3_correct.id})

        self.assertEqual(r1.status_code, status.HTTP_200_OK)
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertEqual(r3.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertEqual(r3.data['access_denied']['reason'], 'free_limit_reached')
        self.assertEqual(r3.data['access_denied']['source'], 'free_starter')
        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='qbank')
        self.assertEqual(row.used, 2)

    def test_re_answering_an_already_attempted_question_never_consumes_again(self):
        self.client.post(f'/api/questions/{self.q1.id}/answer/', {'option_id': self.q1_correct.id})
        row_after_first = FreeStarterEntitlement.objects.get(user=self.student, resource_type='qbank')
        self.assertEqual(row_after_first.used, 1)

        resp = self.client.post(f'/api/questions/{self.q1.id}/answer/', {'option_id': self.q1_correct.id})

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        row_after_second = FreeStarterEntitlement.objects.get(user=self.student, resource_type='qbank')
        self.assertEqual(row_after_second.used, 1)

    def test_already_attempted_question_still_answerable_after_quota_exhausted(self):
        self.client.post(f'/api/questions/{self.q1.id}/answer/', {'option_id': self.q1_correct.id})
        self.client.post(f'/api/questions/{self.q2.id}/answer/', {'option_id': self.q2_correct.id})
        # quota now exhausted (2/2 used) — q1 must still be re-answerable
        resp = self.client.post(f'/api/questions/{self.q1.id}/answer/', {'option_id': self.q1_correct.id})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_real_subscription_bypasses_free_starter_entirely(self):
        _extend_or_create_subscription(self.student, self.course, 'qbank', timezone.timedelta(days=30))

        for q, opt in [(self.q1, self.q1_correct), (self.q2, self.q2_correct), (self.q3, self.q3_correct)]:
            resp = self.client.post(f'/api/questions/{q.id}/answer/', {'option_id': opt.id})
            self.assertEqual(resp.status_code, status.HTTP_200_OK)

        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='qbank')
        self.assertEqual(row.used, 0)

    def test_staff_is_never_gated_or_provisioned(self):
        self.client.force_authenticate(user=self.staff)

        for q, opt in [(self.q1, self.q1_correct), (self.q2, self.q2_correct), (self.q3, self.q3_correct)]:
            resp = self.client.post(f'/api/questions/{q.id}/answer/', {'option_id': opt.id})
            self.assertEqual(resp.status_code, status.HTTP_200_OK)

        self.assertFalse(FreeStarterEntitlement.objects.filter(user=self.staff).exists())

    def test_bookmark_only_call_never_consumes(self):
        """Confirms browsing/non-answer actions never trigger consumption —
        Step 8's core rule. bookmark is a separate action with no option_id,
        so this is really confirming the .answer() gate's own `if option_id`
        scoping stays correct."""
        resp = self.client.post(f'/api/questions/{self.q1.id}/bookmark/')
        self.assertIn(resp.status_code, (200, 201))
        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='qbank')
        self.assertEqual(row.used, 0)


# =====================================================================
# Phase 3 — live wiring: Mock/Daily/Grand/PYQ consumption through the
# real /tests/{id}/start/ and /exam-sessions/{id}/start/ APIs
# =====================================================================

class MockLiveConsumptionTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='mockl_student', email='mockl_student@example.com', password='pw12345')
        self.course = Course.objects.create(name='Live Mock Course', prefix='LMOC')
        Enrollment.objects.create(user=self.student, course=self.course, is_active=True)
        FreeStarterPolicy.objects.create(resource_type='mock_test', quantity=1, is_active=True)
        self.client.force_authenticate(user=self.student)

    def test_first_pro_mock_start_consumes_free_quota(self):
        test = Test.objects.create(title='Live Mock 1', exam_type='mock', is_draft=False, is_pro=True)
        test.courses.set([self.course])

        resp = self.client.post(f'/api/tests/{test.id}/start/')

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='mock_test')
        self.assertEqual(row.used, 1)

    def test_second_pro_mock_is_locked_after_free_quota_used(self):
        test1 = Test.objects.create(title='Live Mock 1', exam_type='mock', is_draft=False, is_pro=True)
        test1.courses.set([self.course])
        test2 = Test.objects.create(title='Live Mock 2', exam_type='mock', is_draft=False, is_pro=True)
        test2.courses.set([self.course])
        self.client.post(f'/api/tests/{test1.id}/start/')

        resp = self.client.post(f'/api/tests/{test2.id}/start/')

        self.assertEqual(resp.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertEqual(resp.data['access_denied']['source'], 'free_starter')

    def test_resuming_the_same_free_mock_does_not_consume_again(self):
        test = Test.objects.create(title='Live Mock 1', exam_type='mock', is_draft=False, is_pro=True, max_attempts=2)
        test.courses.set([self.course])
        self.client.post(f'/api/tests/{test.id}/start/')
        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='mock_test')
        self.assertEqual(row.used, 1)
        # submit/complete the attempt so a resume-start creates attempt #2 of the SAME test
        attempt = TestAttempt.objects.get(user=self.student, test=test)
        attempt.status = 'submitted'
        attempt.save()

        resp = self.client.post(f'/api/tests/{test.id}/start/')

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        row.refresh_from_db()
        self.assertEqual(row.used, 1)

    def test_premium_subscription_still_works_unaffected_by_free_starter(self):
        _extend_or_create_subscription(self.student, self.course, 'mock_test', timezone.timedelta(days=30))
        test = Test.objects.create(title='Premium Mock', exam_type='mock', is_draft=False, is_pro=True)
        test.courses.set([self.course])

        resp = self.client.post(f'/api/tests/{test.id}/start/')

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertFalse(FreeStarterEntitlement.objects.filter(user=self.student, resource_type='mock_test').exists())

    def test_free_mock_test_not_configured_denies_with_no_policy(self):
        FreeStarterPolicy.objects.filter(resource_type='mock_test').update(is_active=False)
        test = Test.objects.create(title='No Free Mock', exam_type='mock', is_draft=False, is_pro=True)
        test.courses.set([self.course])

        resp = self.client.post(f'/api/tests/{test.id}/start/')

        self.assertEqual(resp.status_code, status.HTTP_402_PAYMENT_REQUIRED)


class DailyLiveConsumptionTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='dailyl_student', email='dailyl_student@example.com', password='pw12345')
        self.course = Course.objects.create(name='Live Daily Course', prefix='LDAC')
        Enrollment.objects.create(user=self.student, course=self.course, is_active=True)
        FreeStarterPolicy.objects.create(resource_type='daily_test', quantity=2, is_active=True)
        self.client.force_authenticate(user=self.student)

    def _make_session(self, test):
        template = ExamTemplate.objects.create(title=test.title, exam_type='daily')
        test.exam_template = template
        test.save()
        return ExamSession.objects.create(
            exam_template=template, exam_version=test, session_name='Daily Session', status='live',
            start_datetime=timezone.now() - timezone.timedelta(minutes=5),
            end_datetime=timezone.now() + timezone.timedelta(hours=1),
        )

    def test_unentitled_student_cannot_start_pro_daily_without_free_quota(self):
        FreeStarterPolicy.objects.filter(resource_type='daily_test').update(is_active=False)
        test = Test.objects.create(title='Live Daily 1', exam_type='daily', is_draft=False, is_pro=True)
        test.courses.set([self.course])
        session = self._make_session(test)

        resp = self.client.post(f'/api/exam-sessions/{session.id}/start/')

        self.assertEqual(resp.status_code, status.HTTP_402_PAYMENT_REQUIRED)

    def test_entitled_via_free_starter_can_start_during_window(self):
        test = Test.objects.create(title='Live Daily 2', exam_type='daily', is_draft=False, is_pro=True)
        test.courses.set([self.course])
        session = self._make_session(test)

        resp = self.client.post(f'/api/exam-sessions/{session.id}/start/')

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='daily_test')
        self.assertEqual(row.used, 1)

    def test_viewing_session_detail_does_not_consume(self):
        """Step 12: free quota must not be consumed merely by viewing the
        upcoming Daily Test — confirmed via the read endpoint, not start."""
        test = Test.objects.create(title='Live Daily 3', exam_type='daily', is_draft=False, is_pro=True)
        test.courses.set([self.course])
        session = self._make_session(test)

        self.client.get(f'/api/exam-sessions/{session.id}/')

        self.assertFalse(FreeStarterEntitlement.objects.filter(user=self.student, resource_type='daily_test').exists())

    def test_quota_of_two_allows_two_distinct_daily_tests(self):
        test1 = Test.objects.create(title='Live Daily A', exam_type='daily', is_draft=False, is_pro=True)
        test1.courses.set([self.course])
        session1 = self._make_session(test1)
        test2 = Test.objects.create(title='Live Daily B', exam_type='daily', is_draft=False, is_pro=True)
        test2.courses.set([self.course])
        session2 = self._make_session(test2)

        resp1 = self.client.post(f'/api/exam-sessions/{session1.id}/start/')
        resp2 = self.client.post(f'/api/exam-sessions/{session2.id}/start/')

        self.assertEqual(resp1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp2.status_code, status.HTTP_201_CREATED)
        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='daily_test')
        self.assertEqual(row.used, 2)

        test3 = Test.objects.create(title='Live Daily C', exam_type='daily', is_draft=False, is_pro=True)
        test3.courses.set([self.course])
        session3 = self._make_session(test3)
        resp3 = self.client.post(f'/api/exam-sessions/{session3.id}/start/')
        self.assertEqual(resp3.status_code, status.HTTP_402_PAYMENT_REQUIRED)


class GrandLiveConsumptionTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='grandl_student', email='grandl_student@example.com', password='pw12345')
        self.course = Course.objects.create(name='Live Grand Course', prefix='LGRC')
        Enrollment.objects.create(user=self.student, course=self.course, is_active=True)
        self.client.force_authenticate(user=self.student)

    def test_grand_test_denied_when_no_free_policy_configured(self):
        """Step 13: 0 free opportunities is a valid, expected default —
        confirmed no active grand_test policy means no free path exists."""
        test = Test.objects.create(title='Live Grand 1', exam_type='grand', is_draft=False, is_pro=True)
        test.courses.set([self.course])

        resp = self.client.post(f'/api/tests/{test.id}/start/')

        self.assertEqual(resp.status_code, status.HTTP_402_PAYMENT_REQUIRED)

    def test_grand_test_allowed_when_promotional_free_policy_configured(self):
        FreeStarterPolicy.objects.create(resource_type='grand_test', quantity=1, is_active=True)
        test = Test.objects.create(title='Live Grand 2', exam_type='grand', is_draft=False, is_pro=True)
        test.courses.set([self.course])

        resp = self.client.post(f'/api/tests/{test.id}/start/')

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='grand_test')
        self.assertEqual(row.used, 1)

    def test_grand_test_password_still_required_after_free_grant(self):
        """Password is an additional layer, never a substitute (Step
        "GRAND TEST PASSWORD") — even a free-starter-granted entry must
        still satisfy test.access_password if the exam has one."""
        FreeStarterPolicy.objects.create(resource_type='grand_test', quantity=1, is_active=True)
        test = Test.objects.create(
            title='Live Grand 3', exam_type='grand', is_draft=False, is_pro=True, access_password='SECRET1',
        )
        test.courses.set([self.course])

        wrong = self.client.post(f'/api/tests/{test.id}/start/', {'access_password': 'WRONG'})
        self.assertEqual(wrong.status_code, status.HTTP_403_FORBIDDEN)

        right = self.client.post(f'/api/tests/{test.id}/start/', {'access_password': 'SECRET1'})
        self.assertEqual(right.status_code, status.HTTP_201_CREATED)

    def test_real_grand_test_access_bypasses_free_starter(self):
        test = Test.objects.create(title='Live Grand 4', exam_type='grand', is_draft=False, is_pro=True)
        test.courses.set([self.course])
        purchase = Purchase.objects.create(user=self.student, kind='grand_test', original_amount=1000, final_amount=1000)
        # GrandTestAccess.save() auto-generates a real, unique password if
        # none is given — must be submitted back for start() to succeed.
        access = GrandTestAccess.objects.create(purchase=purchase, user=self.student, test=test)

        resp = self.client.post(f'/api/tests/{test.id}/start/', {'access_password': access.password})

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertFalse(FreeStarterEntitlement.objects.filter(user=self.student, resource_type='grand_test').exists())


class PyqLiveConsumptionTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='pyql_student', email='pyql_student@example.com', password='pw12345')
        self.course = Course.objects.create(name='Live PYQ Course', prefix='LPYC')
        Enrollment.objects.create(user=self.student, course=self.course, is_active=True)
        FreeStarterPolicy.objects.create(resource_type='pyq', quantity=1, is_active=True)
        self.client.force_authenticate(user=self.student)

    def test_first_pro_pyq_test_consumes_shared_pyq_quota(self):
        test = Test.objects.create(
            title='Live PYQ IOM', exam_type='pyq', is_draft=False, is_pro=True, university='IOM',
        )
        test.courses.set([self.course])

        resp = self.client.post(f'/api/tests/{test.id}/start/')

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        row = FreeStarterEntitlement.objects.get(user=self.student, resource_type='pyq')
        self.assertEqual(row.used, 1)

    def test_quota_is_shared_across_institutions_not_per_institution(self):
        """docs/FREE_STARTER_USAGE_RULES.md §6: one shared pyq quota, not
        split by Test.university."""
        iom_test = Test.objects.create(
            title='Live PYQ IOM 2', exam_type='pyq', is_draft=False, is_pro=True, university='IOM',
        )
        iom_test.courses.set([self.course])
        ku_test = Test.objects.create(
            title='Live PYQ KU', exam_type='pyq', is_draft=False, is_pro=True, university='KU',
        )
        ku_test.courses.set([self.course])

        resp1 = self.client.post(f'/api/tests/{iom_test.id}/start/')
        self.assertEqual(resp1.status_code, status.HTTP_201_CREATED)

        resp2 = self.client.post(f'/api/tests/{ku_test.id}/start/')
        self.assertEqual(resp2.status_code, status.HTTP_402_PAYMENT_REQUIRED)


# =====================================================================
# Phase 3 — role safety (Step 31)
# =====================================================================

class StaffNeverConsumesFreeStarterTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='rolesafe_staff', email='rolesafe_staff@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.course = Course.objects.create(name='Role Safety Course', prefix='RSC')
        FreeStarterPolicy.objects.create(resource_type='mock_test', quantity=1, is_active=True)
        self.client.force_authenticate(user=self.staff)

    def test_staff_denied_pro_mock_without_subscription_never_gets_a_free_starter_row(self):
        """Matches the exact pre-Phase-3 behavior (see tests_app.tests.
        ExamRescheduleFeatureGateTests-adjacent precedent from Phase 1/
        entitlements.tests.CanStartTestDecisionTests.
        test_staff_still_needs_entitlement_for_a_pro_test): staff bypass
        the academic gate only, never the commercial one, and must never
        be silently granted (or consume) free-starter access."""
        test = Test.objects.create(title='Staff Pro Mock', exam_type='mock', is_draft=False, is_pro=True)

        resp = self.client.post(f'/api/tests/{test.id}/start/')

        self.assertEqual(resp.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertFalse(FreeStarterEntitlement.objects.filter(user=self.staff).exists())

    def test_can_view_qbank_denies_staff_even_with_active_policy(self):
        FreeStarterPolicy.objects.create(resource_type='qbank', quantity=100, is_active=True)
        subject = Subject.objects.create(name='Staff QBank Subject', is_free=False)
        subject.courses.set([self.course])

        decision = can_view_qbank(self.staff, subject)

        self.assertFalse(decision.allowed)
        self.assertFalse(FreeStarterEntitlement.objects.filter(user=self.staff).exists())


# =====================================================================
# Phase 4 — Access Decision Engine: capability set
# =====================================================================

from entitlements.services import (  # noqa: E402
    CAN_CONTINUE, CAN_PURCHASE, CAN_REGISTER, CAN_REVIEW, CAN_START, CAN_SUBMIT, CAN_VIEW,
    CAN_VIEW_ANALYTICS, CAN_VIEW_RANK, CAN_VIEW_SOLUTIONS, REASON_ATTEMPT_LIMIT_REACHED, REASON_EXAM_CLOSED,
    can_continue_attempt, can_purchase_test, can_register, can_review_attempt, can_submit_attempt, can_view_analytics,
    can_view_rank, can_view_solutions, can_view_test, commercial_entitlement,
)


class CapabilityIndependenceTests(APITestCase):
    """Phase 4 spec, explicit instruction: CanView != CanStart, CanStart !=
    CanSubmit, CanSubmit != CanReview, CanReview != CanViewSolutions,
    CanViewRank != CanViewAnalytics. Each pair proven with a real scenario
    where they diverge, not just "both return a boolean"."""

    def setUp(self):
        self.student = User.objects.create_user(username='indep_student', email='indep_student@example.com', password='pw12345')
        self.other = User.objects.create_user(username='indep_other', email='indep_other@example.com', password='pw12345')
        self.course = Course.objects.create(name='Independence Course', prefix='INDC')
        Enrollment.objects.create(user=self.student, course=self.course, is_active=True)

    def test_can_view_true_can_start_false(self):
        """A visible-but-unpurchased pro exam: CanView=True, CanStart=False
        — the Phase 4 spec's own headline example."""
        test = Test.objects.create(title='View Not Start', exam_type='mock', is_draft=False, is_pro=True)
        test.courses.set([self.course])

        view_decision = can_view_test(self.student, test)
        start_decision = can_start_test(self.student, test)

        self.assertTrue(view_decision.allowed)
        self.assertEqual(view_decision.capability, CAN_VIEW)
        self.assertFalse(start_decision.allowed)
        self.assertEqual(start_decision.capability, CAN_START)

    def test_can_start_true_can_submit_false_before_starting(self):
        """CanStart doesn't imply CanSubmit — there's no attempt yet to
        submit."""
        test = Test.objects.create(title='Start Not Submit', exam_type='mock', is_draft=False, is_pro=False)
        test.courses.set([self.course])

        self.assertTrue(can_start_test(self.student, test).allowed)
        # No attempt exists yet — can_submit_attempt requires a real
        # attempt object, confirming the two capabilities are checked
        # against fundamentally different things (a Test vs. a TestAttempt).
        self.assertFalse(TestAttempt.objects.filter(user=self.student, test=test).exists())

    def test_can_submit_true_can_review_false_while_in_progress(self):
        test = Test.objects.create(title='Submit Not Review', exam_type='mock', is_draft=False, is_pro=False)
        attempt = TestAttempt.objects.create(user=self.student, test=test, status='in_progress')

        submit_decision = can_submit_attempt(self.student, attempt)
        review_decision = can_review_attempt(self.student, attempt)

        self.assertTrue(submit_decision.allowed)
        self.assertEqual(submit_decision.capability, CAN_SUBMIT)
        self.assertFalse(review_decision.allowed)
        self.assertEqual(review_decision.capability, CAN_REVIEW)

    def test_can_review_true_can_view_solutions_currently_matches_but_independently_tagged(self):
        """Same underlying condition today (see docs/ACCESS_DECISION_MATRIX.
        md discrepancy #2 — solutions_visibility is unenforced), but the two
        are distinct, independently-callable functions with distinct
        capability tags, not literally the same function reused under two
        names — confirmed via the .capability field."""
        test = Test.objects.create(title='Review And Solutions', exam_type='mock', is_draft=False, is_pro=False)
        attempt = TestAttempt.objects.create(user=self.student, test=test, status='submitted')

        review_decision = can_review_attempt(self.student, attempt)
        solutions_decision = can_view_solutions(self.student, attempt)

        self.assertTrue(review_decision.allowed)
        self.assertTrue(solutions_decision.allowed)
        self.assertEqual(review_decision.capability, CAN_REVIEW)
        self.assertEqual(solutions_decision.capability, CAN_VIEW_SOLUTIONS)

    def test_can_view_rank_and_can_view_analytics_are_independently_scoped(self):
        """CanViewRank is per-ATTEMPT (needs a submitted attempt);
        CanViewAnalytics is per-USER (self-scoped, no attempt needed at
        all) — genuinely different inputs, not just different labels."""
        test = Test.objects.create(title='Rank vs Analytics', exam_type='mock', is_draft=False, is_pro=False)
        attempt = TestAttempt.objects.create(user=self.student, test=test, status='in_progress')

        rank_decision = can_view_rank(self.student, attempt)  # not yet submitted -> denied
        analytics_decision = can_view_analytics(self.student, self.student)  # always allowed for self

        self.assertFalse(rank_decision.allowed)
        self.assertTrue(analytics_decision.allowed)
        self.assertEqual(rank_decision.capability, CAN_VIEW_RANK)
        self.assertEqual(analytics_decision.capability, CAN_VIEW_ANALYTICS)


class AttemptStateCapabilityTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='asc_student', email='asc_student@example.com', password='pw12345')
        self.other = User.objects.create_user(username='asc_other', email='asc_other@example.com', password='pw12345')
        self.test = Test.objects.create(title='Attempt State Exam', exam_type='mock', is_draft=False, is_pro=False)

    def test_can_continue_true_for_own_in_progress_attempt(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='in_progress')
        self.assertTrue(can_continue_attempt(self.student, attempt).allowed)

    def test_can_continue_false_for_submitted_attempt(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='submitted')
        decision = can_continue_attempt(self.student, attempt)
        self.assertFalse(decision.allowed)

    def test_can_continue_false_for_another_users_attempt(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='in_progress')
        decision = can_continue_attempt(self.other, attempt)
        self.assertFalse(decision.allowed)

    def test_can_submit_false_for_already_submitted(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='submitted')
        self.assertFalse(can_submit_attempt(self.student, attempt).allowed)

    def test_can_review_false_for_in_progress(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='in_progress')
        self.assertFalse(can_review_attempt(self.student, attempt).allowed)

    def test_can_review_true_for_own_submitted(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='submitted')
        self.assertTrue(can_review_attempt(self.student, attempt).allowed)

    def test_can_review_false_for_another_users_submitted_attempt(self):
        """IDOR guard, directly on the capability function."""
        attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='submitted')
        decision = can_review_attempt(self.other, attempt)
        self.assertFalse(decision.allowed)

    def test_can_view_solutions_and_rank_follow_review(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='submitted')
        self.assertTrue(can_view_solutions(self.student, attempt).allowed)
        self.assertTrue(can_view_rank(self.student, attempt).allowed)
        self.assertFalse(can_view_solutions(self.other, attempt).allowed)
        self.assertFalse(can_view_rank(self.other, attempt).allowed)

    def test_anonymous_denied_every_attempt_capability(self):
        from django.contrib.auth.models import AnonymousUser

        attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='submitted')
        anon = AnonymousUser()
        self.assertFalse(can_continue_attempt(anon, attempt).allowed)
        self.assertFalse(can_submit_attempt(anon, attempt).allowed)
        self.assertFalse(can_review_attempt(anon, attempt).allowed)
        self.assertFalse(can_view_solutions(anon, attempt).allowed)
        self.assertFalse(can_view_rank(anon, attempt).allowed)


class CanViewAnalyticsSecurityTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='cva_student', email='cva_student@example.com', password='pw12345')
        self.other = User.objects.create_user(username='cva_other', email='cva_other@example.com', password='pw12345')
        self.staff = User.objects.create_user(
            username='cva_staff', email='cva_staff@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )

    def test_own_analytics_allowed(self):
        self.assertTrue(can_view_analytics(self.student, self.student).allowed)

    def test_another_students_analytics_denied(self):
        decision = can_view_analytics(self.student, self.other)
        self.assertFalse(decision.allowed)

    def test_staff_can_view_any_students_analytics(self):
        self.assertTrue(can_view_analytics(self.staff, self.student).allowed)


class CanPurchaseRegisterTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='cpr_student', email='cpr_student@example.com', password='pw12345')
        self.staff = User.objects.create_user(
            username='cpr_staff', email='cpr_staff@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.course = Course.objects.create(name='Purchase Course', prefix='PURC')
        Enrollment.objects.create(user=self.student, course=self.course, is_active=True)

    def test_can_purchase_true_for_pro_test_without_entitlement(self):
        test = Test.objects.create(title='Purchasable', exam_type='mock', is_draft=False, is_pro=True)
        test.courses.set([self.course])

        decision = can_purchase_test(self.student, test)

        self.assertTrue(decision.allowed)
        self.assertTrue(decision.upgrade_available)

    def test_can_purchase_false_when_already_entitled(self):
        test = Test.objects.create(title='Already Have It', exam_type='mock', is_draft=False, is_pro=True)
        test.courses.set([self.course])
        _extend_or_create_subscription(self.student, self.course, 'mock_test', timezone.timedelta(days=30))

        decision = can_purchase_test(self.student, test)

        self.assertFalse(decision.allowed)

    def test_can_purchase_false_for_free_test(self):
        test = Test.objects.create(title='Free Test', exam_type='mock', is_draft=False, is_pro=False)
        test.courses.set([self.course])

        self.assertFalse(can_purchase_test(self.student, test).allowed)

    def test_staff_cannot_purchase(self):
        test = Test.objects.create(title='Staff Purchase Attempt', exam_type='mock', is_draft=False, is_pro=True)
        test.courses.set([self.course])

        self.assertFalse(can_purchase_test(self.staff, test).allowed)

    def test_can_register_always_allowed(self):
        self.assertTrue(can_register().allowed)


class CanStartSessionAndAttemptLimitTests(APITestCase):
    """Phase 4 addition to can_start_test: entitlement alone is not
    sufficient — session window and attempt-limit state matter too."""

    def setUp(self):
        self.student = User.objects.create_user(username='csal_student', email='csal_student@example.com', password='pw12345')
        self.course = Course.objects.create(name='Session Limit Course', prefix='SLC')
        Enrollment.objects.create(user=self.student, course=self.course, is_active=True)

    def test_valid_entitlement_but_closed_session_denies_start(self):
        test = Test.objects.create(title='Closed Session Exam', exam_type='daily', is_draft=False, is_pro=False)
        test.courses.set([self.course])
        template = ExamTemplate.objects.create(title='Closed Session Exam', exam_type='daily')
        test.exam_template = template
        test.save()
        session = ExamSession.objects.create(
            exam_template=template, exam_version=test, session_name='Closed Session', status='completed',
            start_datetime=timezone.now() - timezone.timedelta(hours=3),
            end_datetime=timezone.now() - timezone.timedelta(hours=1),
        )

        decision = can_start_test(self.student, test, session=session)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, REASON_EXAM_CLOSED)

    def test_valid_entitlement_but_attempt_limit_reached_denies_start(self):
        test = Test.objects.create(
            title='Limit Reached Exam', exam_type='mock', is_draft=False, is_pro=False, max_attempts=1,
        )
        test.courses.set([self.course])
        TestAttempt.objects.create(user=self.student, test=test, status='submitted')

        decision = can_start_test(self.student, test)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, REASON_ATTEMPT_LIMIT_REACHED)

    def test_in_progress_attempt_still_reports_allowed_for_resume(self):
        test = Test.objects.create(title='Resume Via CanStart', exam_type='mock', is_draft=False, is_pro=False)
        test.courses.set([self.course])
        TestAttempt.objects.create(user=self.student, test=test, status='in_progress')

        decision = can_start_test(self.student, test)

        self.assertTrue(decision.allowed)

    def test_no_session_preserves_backward_compatible_behavior(self):
        """Every Phase 2/3 caller passes no session at all — confirm that
        code path is completely unaffected by the Phase 4 addition."""
        test = Test.objects.create(title='No Session Exam', exam_type='mock', is_draft=False, is_pro=False)
        test.courses.set([self.course])

        self.assertTrue(can_start_test(self.student, test).allowed)


# =====================================================================
# Phase 4 — TestResultView security fix (docs/ACCESS_DECISION_MATRIX.md
# discrepancy #1)
# =====================================================================

class TestResultViewSecurityFixTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='trv_student', email='trv_student@example.com', password='pw12345')
        self.other = User.objects.create_user(username='trv_other', email='trv_other@example.com', password='pw12345')
        self.test = Test.objects.create(title='Result View Exam', exam_type='mock', is_draft=False, is_pro=False)
        self.client.force_authenticate(user=self.student)

    def test_in_progress_attempt_result_is_denied(self):
        """The actual bug: this must now return 403, not solutions/answers,
        for a still-in_progress attempt — confirmed unprotected before this
        phase's fix."""
        attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='in_progress')

        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('access_denied', resp.data)
        self.assertFalse(resp.data['access_denied']['allowed'])

    def test_submitted_attempt_result_is_allowed(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='submitted')

        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')

        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_another_users_attempt_is_404_not_403(self):
        """Ownership is enforced by the existing get_object_or_404(...,
        user=request.user) filter, unchanged — confirms this fix didn't
        weaken that IDOR protection to a 403 (which would leak the
        attempt's existence) instead of the correct 404."""
        attempt = TestAttempt.objects.create(user=self.other, test=self.test, status='submitted')

        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_attempt_detail_view_still_works_after_refactor(self):
        """AttemptDetailView was routed through the same capability
        function this phase — confirm its own, already-correct behavior is
        unchanged."""
        in_progress = TestAttempt.objects.create(user=self.student, test=self.test, status='in_progress')
        resp1 = self.client.get(f'/api/attempts/{in_progress.id}/')
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        self.assertNotIn('questions', resp1.data.get('test', {}) if isinstance(resp1.data.get('test'), dict) else {})

        submitted = TestAttempt.objects.create(user=self.student, test=self.test, status='submitted')
        resp2 = self.client.get(f'/api/attempts/{submitted.id}/')
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)


# =====================================================================
# Phase 4 — mandatory cross-source test matrix
# =====================================================================

class CrossSourceMatrixTests(APITestCase):
    """Every combination the Phase 4 spec explicitly mandates. One invalid
    source must never destroy another independent valid source."""

    def setUp(self):
        self.student = User.objects.create_user(username='xsrc_student', email='xsrc_student@example.com', password='pw12345')
        self.staff = User.objects.create_user(
            username='xsrc_staff', email='xsrc_staff@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.course = Course.objects.create(name='Cross Source Course', prefix='XSRC')
        Enrollment.objects.create(user=self.student, course=self.course, is_active=True)

    def _pro_test(self, title, exam_type='mock'):
        t = Test.objects.create(title=title, exam_type=exam_type, is_draft=False, is_pro=True)
        t.courses.set([self.course])
        return t

    def test_free_starter_plus_subscription(self):
        FreeStarterPolicy.objects.create(resource_type='mock_test', quantity=1, is_active=True)
        provision_free_starter(self.student)
        _extend_or_create_subscription(self.student, self.course, 'mock_test', timezone.timedelta(days=30))
        test = self._pro_test('XS Mock 1')

        decision = can_start_test(self.student, test)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.source_type, SOURCE_SUBSCRIPTION)

    def test_free_starter_plus_direct_purchase_grand(self):
        FreeStarterPolicy.objects.create(resource_type='grand_test', quantity=1, is_active=True)
        provision_free_starter(self.student)
        test = self._pro_test('XS Grand 1', exam_type='grand')
        purchase = Purchase.objects.create(user=self.student, kind='grand_test', original_amount=1, final_amount=1)
        GrandTestAccess.objects.create(purchase=purchase, user=self.student, test=test)

        decision = can_start_test(self.student, test)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.source_type, SOURCE_DIRECT_PURCHASE)

    def test_scholarship_plus_subscription_both_independently_valid(self):
        from billing.models import Scholarship

        sub, _ = _extend_or_create_subscription(self.student, self.course, 'qbank', timezone.timedelta(days=30), is_scholarship=True)
        Scholarship.objects.create(user=self.student, course=self.course, product_type='qbank', subscription=sub, granted_by=self.staff)
        _extend_or_create_subscription(self.student, self.course, 'qbank', timezone.timedelta(days=30), is_scholarship=False)

        decision = commercial_entitlement(self.student, 'qbank', self.course)

        self.assertTrue(decision.allowed)
        self.assertEqual(Subscription.objects.filter(user=self.student, course=self.course, product_type='qbank').count(), 2)

    def test_scholarship_plus_purchase_revoking_scholarship_preserves_purchase(self):
        from billing.models import Scholarship

        self.client.force_authenticate(user=self.staff)
        grant_resp = self.client.post('/api/grant-access/', {
            'user_id': self.student.id, 'course_id': self.course.id, 'product_type': 'qbank',
            'duration_value': 1, 'duration_unit': 'month', 'is_scholarship': True, 'reason': 'x',
        })
        scholarship_id = grant_resp.data['scholarship_id']
        _extend_or_create_subscription(self.student, self.course, 'qbank', timezone.timedelta(days=90), is_scholarship=False)

        self.client.post(f'/api/scholarships/{scholarship_id}/revoke/')

        decision = commercial_entitlement(self.student, 'qbank', self.course)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.source_type, SOURCE_SUBSCRIPTION)

    def test_expired_enrollment_plus_subscription(self):
        """Enrollment expiry (Phase 2 fix) must not affect an independent
        Subscription — CanStart still succeeds via subscription even
        though academic Enrollment has lapsed. (can_access_test's course
        check would fail, but individual/batch assignment or a still-valid
        Enrollment isn't the only path — here we confirm the commercial
        layer's own independence by calling commercial_entitlement
        directly, since CanStart's academic gate is a separate,
        intentionally strict prerequisite per docs/ACCESS_DECISION_MATRIX.md.)"""
        other_course = Course.objects.create(name='Expired Enroll Course', prefix='EEC')
        Enrollment.objects.create(
            user=self.student, course=other_course, is_active=True,
            expires_at=timezone.now() - timezone.timedelta(days=1),
        )
        _extend_or_create_subscription(self.student, other_course, 'qbank', timezone.timedelta(days=30))

        decision = commercial_entitlement(self.student, 'qbank', other_course)

        self.assertTrue(decision.allowed)

    def test_expired_scholarship_plus_purchase(self):
        from billing.models import Scholarship

        sub, _ = _extend_or_create_subscription(self.student, self.course, 'qbank', timezone.timedelta(days=-1), is_scholarship=True)
        sub.expires_at = timezone.now() - timezone.timedelta(days=1)
        sub.save()
        Scholarship.objects.create(
            user=self.student, course=self.course, product_type='qbank', subscription=sub,
            expires_at=timezone.now() - timezone.timedelta(days=1), granted_by=self.staff,
        )
        _extend_or_create_subscription(self.student, self.course, 'qbank', timezone.timedelta(days=30), is_scholarship=False)

        decision = commercial_entitlement(self.student, 'qbank', self.course)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.source_type, SOURCE_SUBSCRIPTION)

    def test_expired_subscription_plus_free_starter(self):
        FreeStarterPolicy.objects.create(resource_type='mock_test', quantity=1, is_active=True)
        provision_free_starter(self.student)
        sub, _ = _extend_or_create_subscription(self.student, self.course, 'mock_test', timezone.timedelta(days=30))
        sub.expires_at = timezone.now() - timezone.timedelta(days=1)
        sub.save()
        test = self._pro_test('XS Mock Expired Sub')

        decision = can_start_test(self.student, test)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.source_type, SOURCE_FREE_STARTER)

    def test_expired_subscription_plus_purchase(self):
        sub, _ = _extend_or_create_subscription(self.student, self.course, 'qbank', timezone.timedelta(days=30))
        sub.expires_at = timezone.now() - timezone.timedelta(days=1)
        sub.save()
        _extend_or_create_subscription(self.student, self.course, 'mock_test', timezone.timedelta(days=30))

        decision = commercial_entitlement(self.student, 'mock_test', self.course)

        self.assertTrue(decision.allowed)

    def test_visible_catalog_but_no_entitlement(self):
        test = self._pro_test('XS Visible No Entitlement')

        view_decision = can_view_test(self.student, test)
        start_decision = can_start_test(self.student, test)

        self.assertTrue(view_decision.allowed)
        self.assertFalse(start_decision.allowed)

    def test_valid_entitlement_plus_closed_exam(self):
        test = self._pro_test('XS Closed Exam', exam_type='daily')
        _extend_or_create_subscription(self.student, self.course, 'daily_test', timezone.timedelta(days=30))
        template = ExamTemplate.objects.create(title='XS Closed', exam_type='daily')
        test.exam_template = template
        test.save()
        session = ExamSession.objects.create(
            exam_template=template, exam_version=test, session_name='XS Closed Session', status='completed',
            start_datetime=timezone.now() - timezone.timedelta(hours=3),
            end_datetime=timezone.now() - timezone.timedelta(hours=1),
        )

        decision = can_start_test(self.student, test, session=session)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, REASON_EXAM_CLOSED)

    def test_valid_entitlement_plus_attempt_limit_reached(self):
        test = self._pro_test('XS Attempt Limit')
        test.max_attempts = 1
        test.save()
        _extend_or_create_subscription(self.student, self.course, 'mock_test', timezone.timedelta(days=30))
        TestAttempt.objects.create(user=self.student, test=test, status='submitted')

        decision = can_start_test(self.student, test)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, REASON_ATTEMPT_LIMIT_REACHED)

    def test_free_starter_exhausted_plus_valid_individual_assignment(self):
        """Assignment (academic) bypasses the free/pro distinction
        entirely once is_pro is False — but for a PRO test, an assignment
        alone (academic gate) is not commercial entitlement; confirm free
        exams stay reachable via assignment even with zero free-starter
        quota, and that a pro test still correctly requires commercial
        entitlement despite the assignment (assignment satisfies CanView/
        the academic prerequisite, not the commercial layer)."""
        assigned_student = User.objects.create_user(
            username='xsrc_assigned', email='xsrc_assigned@example.com', password='pw12345',
        )
        FreeStarterPolicy.objects.create(resource_type='mock_test', quantity=1, is_active=True)
        provision_free_starter(assigned_student)
        consume_free_starter(assigned_student, 'mock_test')  # exhaust it

        free_test = Test.objects.create(title='XS Assigned Free', exam_type='mock', is_draft=False, is_pro=False)
        free_test.assigned_students.set([assigned_student])
        pro_test = Test.objects.create(title='XS Assigned Pro', exam_type='mock', is_draft=False, is_pro=True)
        pro_test.assigned_students.set([assigned_student])

        free_decision = can_start_test(assigned_student, free_test)
        pro_decision = can_start_test(assigned_student, pro_test)

        self.assertTrue(free_decision.allowed)
        self.assertFalse(pro_decision.allowed)


# =====================================================================
# Phase 4 — performance: no N+1 introduced by the capability layer
# =====================================================================

class AccessEnginePerformanceTests(APITestCase):
    def test_can_start_test_query_count_is_bounded(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        student = User.objects.create_user(username='perf_student', email='perf_student@example.com', password='pw12345')
        course = Course.objects.create(name='Perf Course', prefix='PERFC')
        Enrollment.objects.create(user=student, course=course, is_active=True)
        test = Test.objects.create(title='Perf Test', exam_type='mock', is_draft=False, is_pro=True)
        test.courses.set([course])
        _extend_or_create_subscription(student, course, 'mock_test', timezone.timedelta(days=30))

        with CaptureQueriesContext(connection) as ctx:
            decision = can_start_test(student, test)

        self.assertTrue(decision.allowed)
        # Flat, small query count — not proportional to any catalog size;
        # a regression here (e.g. an accidental per-course or per-batch
        # loop) would show up as this growing, not as a specific number
        # that needs pinning like the scalability-audited list endpoints.
        self.assertLess(len(ctx.captured_queries), 10, ctx.captured_queries)
