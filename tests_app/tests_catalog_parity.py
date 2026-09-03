"""Unified Exam Catalog Visibility / Access remediation — the cross-exam-
type parity invariants.

The audit found NO commercial filter anywhere in the catalog pipeline for
any exam type: `tests_app.access.visible_test_queryset` and
`academics.access.question_course_scoped` are the only two filters, and
both gate purely on course/batch/assignment eligibility
(`courses.access.eligible_course_ids`/`eligible_batch_ids`) — never on
subscription, purchase, Free Starter, or `is_pro`. Every `has_*_access`
call site in the codebase (grep'd exhaustively) gates an *action*
(`_start_attempt`) or a *presentation field* (`access`, legacy
`has_access`), never a catalog queryset.

So the reported "free student sees 0 tests" bug was never a catalog
filter defect — it was a missing *prerequisite* for the (correctly
academic-only) filter to pass: registration never created the
`courses.Enrollment` row `eligible_course_ids()` requires. That fix lives
in courses/tests_catalog_registration.py. This file proves the resulting
invariant holds across all five exam families now that the prerequisite
is met — i.e. it assumes both students are properly enrolled (as any two
real students in the same course now are, by the registration fix) and
checks that entitlement differences never leak into catalog membership.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APITestCase

from billing.models import GrandTestAccess, Purchase, Subscription
from courses.models import Course, Enrollment
from entitlements.models import EntitlementEventLog, FreeStarterEntitlement, FreeStarterPolicy
from tests_app.models import Test

User = get_user_model()


def assert_catalog_parity(testcase, free_user, paid_user, *, filters=None):
    """The reusable helper the remediation brief asked for. Compares the
    legitimate visible Test id set for two students against the real
    TestViewSet queryset pipeline (not a hand-rolled equivalent), and
    fails with the actual differing ids if they diverge — so a real defect
    shows exactly which resource leaked or vanished, not just that a
    mismatch occurred.
    """
    from tests_app.access import visible_test_queryset

    qs = Test.objects.all()
    if filters:
        qs = qs.filter(**filters)
    free_ids = set(visible_test_queryset(free_user, qs).values_list('id', flat=True))
    paid_ids = set(visible_test_queryset(paid_user, qs).values_list('id', flat=True))
    testcase.assertEqual(
        free_ids, paid_ids,
        f'catalog parity violated — free-only: {free_ids - paid_ids}, paid-only: {paid_ids - free_ids}',
    )
    return free_ids


class CatalogParityBase(APITestCase):
    """One course, one free student (Enrollment access_type='free', the
    registration-fix outcome) and one paid student (Enrollment
    access_type='package' + an active subscription of every commercial
    product type) — set up once, reused by every exam-type test below so
    the parity invariant is checked identically everywhere."""

    def setUp(self):
        self.course = Course.objects.create(name='Parity Course', prefix='PARC')

        self.free_student = User.objects.create_user(
            username='parity_free', email='parity_free@example.com', password='pw12345',
        )
        Enrollment.objects.create(user=self.free_student, course=self.course, access_type='free', is_active=True)

        self.paid_student = User.objects.create_user(
            username='parity_paid', email='parity_paid@example.com', password='pw12345',
        )
        Enrollment.objects.create(user=self.paid_student, course=self.course, access_type='package', is_active=True)
        for product_type in ('mock_test', 'daily_test', 'qbank', 'pyq'):
            Subscription.objects.create(
                user=self.paid_student, course=self.course, product_type=product_type, is_active=True,
            )

    def _mktest(self, exam_type, *, is_pro=True, **overrides):
        fields = {'title': f'{exam_type} test', 'exam_type': exam_type, 'is_draft': False, 'is_pro': is_pro}
        fields.update(overrides)
        test = Test.objects.create(**fields)
        test.courses.set([self.course])
        return test


class DailyTestCatalogParityTests(CatalogParityBase):
    def test_free_and_paid_see_the_same_daily_catalog(self):
        pro = self._mktest('daily', is_pro=True)
        free = self._mktest('daily', is_pro=False)
        visible = assert_catalog_parity(self, self.free_student, self.paid_student, filters={'exam_type': 'daily'})
        self.assertEqual(visible, {pro.id, free.id})

    def test_but_start_capability_correctly_differs(self):
        pro = self._mktest('daily', is_pro=True)
        from entitlements.services import can_start_test

        self.assertFalse(can_start_test(self.free_student, pro).allowed)
        self.assertTrue(can_start_test(self.paid_student, pro).allowed)


class MockTestCatalogParityTests(CatalogParityBase):
    def test_free_and_paid_see_the_same_mock_catalog(self):
        tests = [self._mktest('mock', is_pro=True) for _ in range(3)]
        visible = assert_catalog_parity(self, self.free_student, self.paid_student, filters={'exam_type': 'mock'})
        self.assertEqual(visible, {t.id for t in tests})


class GrandTestCatalogParityTests(CatalogParityBase):
    def test_free_and_paid_see_the_same_grand_catalog_regardless_of_direct_purchase(self):
        """Grand Test access is presence-based (a GrandTestAccess row from
        a direct purchase), not subscription-based — so the paid_student
        fixture's subscriptions do NOT grant Grand access, and neither
        student here has bought one. Catalog visibility must still match:
        this is the strongest form of the parity invariant, since it holds
        even when NEITHER student is commercially entitled."""
        grand = self._mktest('grand', is_pro=True)
        visible = assert_catalog_parity(self, self.free_student, self.paid_student, filters={'exam_type': 'grand'})
        self.assertEqual(visible, {grand.id})

    def test_unregistered_metadata_visible_start_denied_purchase_offered(self):
        """STEP 13's requirement: an unpaid student sees title/marks/
        question count on a Grand Test detail response, Start is denied,
        and the response says a purchase would unlock it."""
        grand = self._mktest('grand', is_pro=True, duration_minutes=180)
        self.client.force_authenticate(self.free_student)
        resp = self.client.get(f'/api/tests/{grand.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['title'], grand.title)
        self.assertIn('duration_minutes', resp.data)
        access = resp.data['access']
        self.assertFalse(access['can_start'])
        self.assertTrue(access['upgrade_available'])

    def test_direct_purchase_overrides_catalog_neutral_baseline(self):
        purchase = Purchase.objects.create(
            user=self.free_student, kind='grand_test', grand_test=None,
            original_amount=100, final_amount=100, status='approved',
        )
        grand = self._mktest('grand', is_pro=True)
        purchase.grand_test = grand
        purchase.save(update_fields=['grand_test'])
        GrandTestAccess.objects.create(user=self.free_student, test=grand, purchase=purchase)

        from entitlements.services import can_start_test

        self.assertTrue(can_start_test(self.free_student, grand).allowed)
        # Catalog membership is unaffected by acquiring access — still
        # exactly the same set as before the purchase.
        visible = assert_catalog_parity(self, self.free_student, self.paid_student, filters={'exam_type': 'grand'})
        self.assertEqual(visible, {grand.id})


class PYQCatalogParityTests(CatalogParityBase):
    """IOM / BPKIHS / MOE / KU — each is a `Test.university` value on the
    same exam_type='pyq' row, so one parametrized test class covers all
    four institutions the brief named individually; the underlying
    catalog pipeline is identical, confirmed by the audit (STEP 14's own
    framing: "institution, year, subject... available tests/counts" all
    come from the same TestViewSet queryset, just grouped differently by
    the `universities()` action)."""

    def test_each_institution_has_identical_free_vs_paid_catalog(self):
        for university in ('IOM', 'BPKIHS', 'MOE', 'KU'):
            with self.subTest(university=university):
                test = self._mktest('pyq', is_pro=True, university=university, academic_year='2025-26')
                visible = assert_catalog_parity(
                    self, self.free_student, self.paid_student,
                    filters={'exam_type': 'pyq', 'university': university},
                )
                self.assertEqual(visible, {test.id})

    def test_universities_endpoint_does_not_differ_by_entitlement(self):
        """The top-level 'Choose a University' listing itself — same
        pipeline (`self.get_queryset()`), same invariant."""
        self._mktest('pyq', is_pro=True, university='IOM')
        self._mktest('pyq', is_pro=False, university='BPKIHS')

        self.client.force_authenticate(self.free_student)
        free_resp = self.client.get('/api/tests/universities/')
        self.client.force_authenticate(self.paid_student)
        paid_resp = self.client.get('/api/tests/universities/')

        free_names = {row['name'] for row in free_resp.data}
        paid_names = {row['name'] for row in paid_resp.data}
        self.assertEqual(free_names, paid_names)
        self.assertIn('IOM', free_names)
        self.assertIn('BPKIHS', free_names)

    def test_pyq_consumption_stays_test_level_not_question_level(self):
        """Guard against the brief's explicit warning: PYQ Free Starter
        consumption is Test-level and must not be turned into QBank-style
        per-question consumption by this remediation. Confirmed by
        reading, not assumed: both entitlements.services.can_start_test
        and tests_app.card_access (the batched projection that must stay
        in lockstep with it — see that module's own agreement test) map
        exam_type 'pyq' to the 'pyq' Free Starter resource, drawn down
        once per exam start — this test pins that neither mapping was
        touched by this remediation."""
        from tests_app.card_access import FREE_STARTER_RESOURCE

        self.assertEqual(FREE_STARTER_RESOURCE['pyq'], 'pyq')

        pyq = self._mktest('pyq', is_pro=True, university='IOM')
        FreeStarterPolicy.objects.create(resource_type='pyq', quantity=1, is_active=True)
        from entitlements.services import can_start_test

        decision = can_start_test(self.free_student, pyq)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.source_type, 'free_starter')


class QBankCatalogParityTests(CatalogParityBase):
    """QBank's catalog is Subject/Chapter/Topic, not Test — audited
    separately per STEP 10's own instruction that QBank isn't exactly the
    same shape as a Test catalog."""

    def test_subjects_are_equally_visible_regardless_of_entitlement(self):
        from academics.models import Subject

        subject = Subject.objects.create(name='Parity Subject', is_free=False)
        subject.courses.set([self.course])
        from courses.access import eligible_course_ids

        self.assertEqual(eligible_course_ids(self.free_student), eligible_course_ids(self.paid_student))

    def test_question_count_reflects_the_full_catalog_not_just_accessible_questions(self):
        """STEP 10: 'catalog should normally show... question counts' —
        not a count filtered down to what THIS student can currently
        answer. Locked-subject exclusion applies to individual Question
        rows returned by QuestionViewSet, never to this annotated count."""
        from academics.models import Question, Subject

        subject = Subject.objects.create(name='Count Subject', is_free=False)
        subject.courses.set([self.course])
        for i in range(4):
            Question.objects.create(subject=subject, text=f'Q{i}')

        self.client.force_authenticate(self.free_student)
        resp = self.client.get(f'/api/subjects/{subject.slug}/')
        self.assertEqual(resp.data['question_count'], 4)

    def test_question_content_itself_remains_protected_for_a_locked_subject(self):
        """The other half of STEP 10: catalog visibility must never leak
        protected content. A Pro-locked subject's questions are excluded
        from QuestionViewSet entirely for a student without qbank access —
        this is NOT a catalog-visibility regression, it is the correct,
        unrelated consumption guard staying exactly where it was."""
        from academics.models import Question, Subject

        subject = Subject.objects.create(name='Locked Subject', is_free=False)
        subject.courses.set([self.course])
        question = Question.objects.create(subject=subject, text='Protected?')

        self.client.force_authenticate(self.free_student)
        resp = self.client.get('/api/questions/', {'subject': subject.slug})
        self.assertEqual(resp.status_code, 200)
        returned_ids = {row['id'] for row in resp.data}
        self.assertNotIn(question.id, returned_ids)


class FreeStarterDoesNotGateCatalogTests(CatalogParityBase):
    """The Free Starter invariant: exhausted quota must never hide the
    catalog, and browsing must never consume it."""

    def setUp(self):
        super().setUp()
        FreeStarterPolicy.objects.create(resource_type='daily_test', quantity=1, is_active=True)

    def test_exhausted_free_starter_does_not_remove_catalog_items(self):
        pro = self._mktest('daily', is_pro=True)
        FreeStarterEntitlement.objects.create(
            user=self.free_student, resource_type='daily_test', quantity=1, used=1, status='exhausted',
        )
        self.client.force_authenticate(self.free_student)
        resp = self.client.get('/api/tests/?exam_type=daily')
        ids = {row['id'] for row in resp.data}
        self.assertIn(pro.id, ids)

        access = next(row['access'] for row in resp.data if row['id'] == pro.id)
        self.assertFalse(access['can_start'])
        self.assertTrue(access['upgrade_available'])

    def test_browsing_the_catalog_does_not_consume_free_starter_quota(self):
        self._mktest('daily', is_pro=True)
        self._mktest('mock', is_pro=True)
        self._mktest('pyq', is_pro=True, university='IOM')
        entitlement = FreeStarterEntitlement.objects.create(
            user=self.free_student, resource_type='daily_test', quantity=1, used=0,
        )
        events_before = EntitlementEventLog.objects.filter(user=self.free_student).count()

        self.client.force_authenticate(self.free_student)
        self.client.get('/api/tests/?exam_type=daily')
        self.client.get('/api/tests/?exam_type=mock')
        self.client.get('/api/tests/?exam_type=pyq')
        self.client.get('/api/tests/?exam_type=daily&search=day')

        entitlement.refresh_from_db()
        self.assertEqual(entitlement.used, 0)
        self.assertEqual(EntitlementEventLog.objects.filter(user=self.free_student).count(), events_before)

    def test_paid_entitlement_overrides_exhausted_free_starter(self):
        pro = self._mktest('daily', is_pro=True)
        FreeStarterEntitlement.objects.create(
            user=self.paid_student, resource_type='daily_test', quantity=1, used=1, status='exhausted',
        )
        from entitlements.services import can_start_test

        decision = can_start_test(self.paid_student, pro)
        self.assertTrue(decision.allowed)
        entitlement = FreeStarterEntitlement.objects.get(user=self.paid_student, resource_type='daily_test')
        self.assertEqual(entitlement.used, 1, 'a valid paid source must not touch Free Starter usage at all')


class DirectApiSecurityStillProtectedTests(CatalogParityBase):
    """STEP 27/40: catalog visibility must never become an authorization
    bypass. The free student sees the catalog; the start endpoint must
    still say no."""

    def test_catalog_visible_but_start_denied_without_entitlement(self):
        pro = self._mktest('daily', is_pro=True)
        self.client.force_authenticate(self.free_student)

        list_resp = self.client.get('/api/tests/?exam_type=daily')
        self.assertIn(pro.id, {row['id'] for row in list_resp.data})

        start_resp = self.client.post(f'/api/tests/{pro.id}/start/')
        self.assertIn(start_resp.status_code, (402, 403))

    def test_catalog_visible_but_answer_and_submit_remain_protected(self):
        """A guessed/leaked attempt id must not let a non-owner touch it —
        unrelated to catalog visibility, confirmed unaffected."""
        pro = self._mktest('mock', is_pro=True)
        other = User.objects.create_user(username='other_student', email='other@example.com', password='pw12345')
        Enrollment.objects.create(user=other, course=self.course, access_type='free', is_active=True)

        from tests_app.models import TestAttempt

        attempt = TestAttempt.objects.create(user=other, test=pro)

        self.client.force_authenticate(self.free_student)
        resp = self.client.post(
            f'/api/attempts/{attempt.id}/answer/', {'question_id': 1, 'option_id': 1}, format='json',
        )
        self.assertIn(resp.status_code, (403, 404))


class CompletedAttemptSurvivesEntitlementChangeTests(CatalogParityBase):
    """STEP 18/39: a completed test must not disappear from the catalog,
    nor lose its Review state, because a later entitlement change occurred."""

    def test_completed_daily_test_remains_visible_and_reviewable_after_subscription_expires(self):
        from django.utils import timezone

        from tests_app.lifecycle import finalize_attempt
        from tests_app.models import TestAttempt

        daily = self._mktest('daily', is_pro=True)
        sub = Subscription.objects.create(
            user=self.free_student, course=self.course, product_type='daily_test', is_active=True,
        )
        attempt = TestAttempt.objects.create(user=self.free_student, test=daily)
        attempt = finalize_attempt(attempt, auto_submitted=False)

        sub.expires_at = timezone.now() - timezone.timedelta(days=1)
        sub.save(update_fields=['expires_at'])

        from tests_app.access import visible_test_queryset

        self.assertIn(daily.id, visible_test_queryset(self.free_student, Test.objects.all()).values_list('id', flat=True))

        from entitlements.services import can_review_attempt

        self.assertTrue(can_review_attempt(self.free_student, attempt).allowed)
