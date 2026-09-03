"""Unified Exam Catalog Visibility remediation — the root-cause fix.

Reproduces and fixes the reported bug: a non-paid student saw 0 Daily
Tests while a paid student saw the real catalog. Root cause (confirmed by
audit, not assumed): `courses.access.eligible_course_ids()` — the single
gate behind Test/Question/Video catalog visibility platform-wide — reads
ONLY `courses.Enrollment`. Registration collected a course choice
(`User.course`, a `Course.prefix`) but never turned it into an Enrollment
row. Only two paths ever created one: an admin manually approving an
`EnrollmentRequest`, or `billing.payment_service._ensure_enrollment()` on
a successful purchase. So a self-registered free student stayed
catalog-blind everywhere until an admin happened to enroll them — which
in practice mostly only happened as a side effect of paying.

No commercial filter exists anywhere in the catalog pipeline for any exam
type (confirmed by an exhaustive audit of every `has_*_access` call site
— none of them filter a queryset, only individual actions and
presentation fields). So this one fix — giving free registration the same
Enrollment `_ensure_enrollment` already gives a paid purchase — is the
complete root-cause fix for all five exam families, which is why this
file, not five per-exam-type files, is the test of it. Cross-exam-type
catalog *parity* (the observable effect of this fix) is tested in
tests_app/tests_catalog_parity.py.
"""
import importlib

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APITestCase

from accounts.serializers import RegisterSerializer
from courses.models import Course, Enrollment
from entitlements.models import EntitlementEventLog, FreeStarterEntitlement, FreeStarterPolicy

User = get_user_model()


class RegistrationCreatesEnrollmentTests(APITestCase):
    """The code-side half of the fix: accounts.serializers.RegisterSerializer."""

    def setUp(self):
        self.course = Course.objects.create(name='Reg Course', prefix='REGC')

    def _register(self, **overrides):
        data = {
            'name': 'Free Student', 'email': 'free.student@example.com', 'password': 'StrongPass123!',
            'program': '', 'course': self.course.prefix, 'referral_code': '', 'college': '',
        }
        data.update(overrides)
        serializer = RegisterSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        return serializer.save()

    def test_registering_with_a_valid_course_creates_a_free_enrollment(self):
        user = self._register()
        enrollment = Enrollment.objects.get(user=user, course=self.course)
        self.assertEqual(enrollment.access_type, 'free')
        self.assertTrue(enrollment.is_active)

    def test_this_is_the_exact_bug_reproduced_and_fixed(self):
        """Reproduces the reported symptom directly: same course, a real
        Test row scoped to it, compare a freshly-registered student's
        visible catalog before this fix existed (no Enrollment at all —
        simulated by deleting the one registration just created) against
        after. This is the literal "0 Daily Tests" bug."""
        from tests_app.access import visible_test_queryset
        from tests_app.models import Test

        test = Test.objects.create(title='Daily 1', exam_type='daily', is_draft=False)
        test.courses.set([self.course])

        user = self._register()
        # Pre-fix simulation: strip the Enrollment this registration just
        # created, reproducing exactly what every self-registered student
        # experienced before this fix.
        Enrollment.objects.filter(user=user, course=self.course).delete()
        pre_fix_catalog = list(visible_test_queryset(user, Test.objects.all()).values_list('id', flat=True))
        self.assertEqual(pre_fix_catalog, [], 'pre-fix: this is the reported bug — 0 visible tests')

        # Re-run the actual fix (register a second, identical student) and
        # confirm the same course/test now resolves correctly.
        user2 = self._register(email='free.student2@example.com')
        post_fix_catalog = list(visible_test_queryset(user2, Test.objects.all()).values_list('id', flat=True))
        self.assertEqual(post_fix_catalog, [test.id])

    def test_blank_course_does_not_create_an_enrollment_and_does_not_fail_registration(self):
        user = self._register(course='', email='nocourse@example.com')
        self.assertEqual(Enrollment.objects.filter(user=user).count(), 0)
        self.assertTrue(User.objects.filter(pk=user.pk).exists())

    def test_unknown_course_prefix_does_not_create_an_enrollment_and_does_not_fail_registration(self):
        """A stale/mistyped course value must degrade gracefully — same
        best-effort contract as the Free Starter provisioning immediately
        above it in the same method."""
        user = self._register(course='NOSUCHCOURSE', email='badcourse@example.com')
        self.assertEqual(Enrollment.objects.filter(user=user).count(), 0)
        self.assertTrue(User.objects.filter(pk=user.pk).exists())

    def test_does_not_regress_free_starter_provisioning(self):
        """This fix sits directly after Free Starter provisioning in the
        same method — must not have broken it, and must not itself consume
        anything Free Starter owns."""
        FreeStarterPolicy.objects.create(resource_type='mock_test', quantity=3, is_active=True)
        user = self._register()
        self.assertTrue(FreeStarterEntitlement.objects.filter(user=user).exists())
        # Enrollment is catalog membership only — it must never appear as a
        # Free Starter consumption event.
        self.assertEqual(EntitlementEventLog.objects.filter(user=user, event='consumed').count(), 0)

    def test_registration_creates_exactly_one_enrollment_not_a_duplicate(self):
        user = self._register()
        self.assertEqual(Enrollment.objects.filter(user=user, course=self.course).count(), 1)

    def test_grants_catalog_membership_only_not_any_commercial_entitlement(self):
        """The central invariant of this whole remediation, checked at its
        origin: registering (and thus gaining an Enrollment) must not by
        itself grant CanStart on a Pro resource. Enrollment is catalog
        visibility; entitlement is a completely separate axis."""
        from tests_app.models import Test

        test = Test.objects.create(title='Pro Daily', exam_type='daily', is_draft=False, is_pro=True)
        test.courses.set([self.course])
        user = self._register(email='cantstart@example.com')

        from entitlements.services import can_start_test

        decision = can_start_test(user, test)
        self.assertFalse(decision.allowed)


class BackfillEnrollmentFromRegisteredCourseMigrationTests(TestCase):
    """Direct test of the data-backfill migration fixing the same bug for
    students who registered BEFORE this code fix existed — mirrors
    BackfillEnrollmentFromSubscriptionMigrationTests' own structure
    exactly (courses/tests.py), the established precedent for this exact
    class of fix on the paid side."""

    def _run_backfill(self):
        migration_module = importlib.import_module(
            'courses.migrations.0008_backfill_enrollment_from_registered_course',
        )
        migration_module.backfill_enrollment_from_registered_course(apps, None)

    def test_backfill_creates_missing_enrollment_for_a_pre_fix_student(self):
        course = Course.objects.create(name='Backfill Course', prefix='BFCR')
        student = User.objects.create_user(
            username='pre_fix_student', email='pre_fix@example.com', password='pw12345', course='BFCR',
        )
        self.assertFalse(Enrollment.objects.filter(user=student, course=course).exists())

        self._run_backfill()

        enrollment = Enrollment.objects.get(user=student, course=course)
        self.assertEqual(enrollment.access_type, 'free')
        self.assertTrue(enrollment.is_active)
        self.assertTrue(enrollment.student_code.startswith('BFCR'))

    def test_backfill_does_not_duplicate_or_downgrade_an_existing_enrollment(self):
        """A student already enrolled some other way (admin-approved,
        or already paid) must be left completely untouched — this
        migration may only ADD what is missing, never alter what exists."""
        course = Course.objects.create(name='Backfill Course 2', prefix='BFCR2')
        student = User.objects.create_user(
            username='already_enrolled', email='already@example.com', password='pw12345', course='BFCR2',
        )
        Enrollment.objects.create(user=student, course=course, access_type='package', is_active=True)

        self._run_backfill()

        self.assertEqual(Enrollment.objects.filter(user=student, course=course).count(), 1)
        self.assertEqual(Enrollment.objects.get(user=student, course=course).access_type, 'package')

    def test_backfill_skips_staff_accounts(self):
        course = Course.objects.create(name='Backfill Course 3', prefix='BFCR3')
        staff = User.objects.create_user(
            username='staffer', email='staffer@example.com', password='pw12345', course='BFCR3', is_staff=True,
        )
        self._run_backfill()
        self.assertFalse(Enrollment.objects.filter(user=staff, course=course).exists())

    def test_backfill_skips_blank_or_unmatched_course_values(self):
        User.objects.create_user(username='blank_course', email='blank@example.com', password='pw12345', course='')
        User.objects.create_user(username='stale_course', email='stale@example.com', password='pw12345', course='NOPE')
        # Must not raise, and must not create any Enrollment for either.
        self._run_backfill()
        self.assertEqual(Enrollment.objects.filter(user__email__in=['blank@example.com', 'stale@example.com']).count(), 0)

    def test_backfill_never_touches_user_or_billing_data(self):
        """Purely additive, per its own docstring — confirmed rather than
        just asserted."""
        from billing.models import Purchase

        course = Course.objects.create(name='Backfill Course 4', prefix='BFCR4')
        student = User.objects.create_user(
            username='untouched', email='untouched@example.com', password='pw12345', course='BFCR4',
        )
        before_email, before_course_field = student.email, student.course
        purchases_before = Purchase.objects.count()

        self._run_backfill()

        student.refresh_from_db()
        self.assertEqual(student.email, before_email)
        self.assertEqual(student.course, before_course_field)
        self.assertEqual(Purchase.objects.count(), purchases_before)

    def test_backfill_is_idempotent(self):
        course = Course.objects.create(name='Backfill Course 5', prefix='BFCR5')
        student = User.objects.create_user(
            username='idempotent', email='idempotent@example.com', password='pw12345', course='BFCR5',
        )
        self._run_backfill()
        self._run_backfill()
        self.assertEqual(Enrollment.objects.filter(user=student, course=course).count(), 1)
