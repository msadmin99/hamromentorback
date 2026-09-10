"""Phase 10 — Student UX: the server-authoritative card contract.

The frontend renders `test.access`; these tests are what make that
contract trustworthy. The most important one is
`CardAccessMatchesCanStartTestTests`, which pins the batched card
resolver to Phase 4's canonical `can_start_test()` across the entitlement
matrix — if the two ever disagree, that test fails rather than the UI
quietly lying to a student.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from academics.models import Option, Question, Subject
from billing.models import GrandTestAccess, Purchase, Subscription, SubscriptionPlan
from courses.models import Course, Enrollment
from entitlements.models import FreeStarterEntitlement, FreeStarterPolicy
from entitlements.services import can_start_test
from tests_app.card_access import StudentEntitlementSnapshot, resolve_card_access
from tests_app.models import Answer, ExamSession, ExamTemplate, Test, TestAttempt, TestQuestion

User = get_user_model()


def _resolve(user, test, session=None):
    attempts = list(test.attempts.filter(user=user)) if user.is_authenticated else []
    return resolve_card_access(test, StudentEntitlementSnapshot(user), attempts, session=session)


class Phase10Base(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='ux_student', email='ux_student@example.com', password='pw')
        self.course = Course.objects.create(name='UX Course', prefix='UXC')
        Enrollment.objects.create(user=self.student, course=self.course, is_active=True)
        self.subject = Subject.objects.create(name='UX Subject', is_free=False)
        self.subject.courses.set([self.course])

    def _test(self, exam_type='mock', **overrides):
        fields = {
            'title': f'{exam_type} test', 'exam_type': exam_type, 'is_draft': False,
            'duration_minutes': 60, 'max_attempts': 1,
        }
        fields.update(overrides)
        test = Test.objects.create(**fields)
        test.courses.set([self.course])
        question = Question.objects.create(subject=self.subject, text='Q?', marks=1, negative_marks=0)
        Option.objects.create(question=question, text='A', order=0, is_correct=True)
        TestQuestion.objects.create(test=test, question=question)
        return test

    def _subscribe(self, product_type='mock_test', days=30):
        return Subscription.objects.create(
            user=self.student, course=self.course, product_type=product_type,
            expires_at=timezone.now() + timezone.timedelta(days=days), is_active=True,
        )

    def _free_starter(self, resource_type='mock_test', quantity=1, used=0):
        FreeStarterPolicy.objects.update_or_create(
            resource_type=resource_type, defaults={'quantity': quantity, 'is_active': True},
        )
        return FreeStarterEntitlement.objects.update_or_create(
            user=self.student, resource_type=resource_type,
            defaults={'quantity': quantity, 'used': used, 'status': 'active'},
        )[0]


class CardStateTests(Phase10Base):
    """The states the UI renders, each from the real decision rather than
    a price check."""

    def test_free_test_is_startable(self):
        test = self._test(is_pro=False)
        block = _resolve(self.student, test)
        self.assertEqual(block['state'], 'start')
        self.assertTrue(block['can_start'])
        self.assertEqual(block['reason_code'], '')

    def test_pro_test_without_any_entitlement_is_locked_with_an_upgrade_path(self):
        test = self._test(is_pro=True)
        block = _resolve(self.student, test)
        self.assertEqual(block['state'], 'locked')
        self.assertFalse(block['can_start'])
        self.assertEqual(block['reason_code'], 'purchase_required')
        self.assertTrue(block['upgrade_available'])

    def test_pro_test_with_an_active_subscription_is_startable(self):
        """The exact case the old `is_pro && ...` frontend rule got wrong:
        a paying student was shown a padlock."""
        test = self._test(is_pro=True)
        self._subscribe('mock_test')
        block = _resolve(self.student, test)
        self.assertEqual(block['state'], 'start')
        self.assertEqual(block['source'], 'subscription')

    def test_pro_test_with_free_starter_available_is_startable(self):
        test = self._test(is_pro=True)
        self._free_starter('mock_test', quantity=1, used=0)
        block = _resolve(self.student, test)
        self.assertEqual(block['state'], 'start')
        self.assertEqual(block['source'], 'free_starter')

    def test_exhausted_free_starter_reports_free_limit_reached_not_purchase_required(self):
        """'You used your free test' and 'you never had access' are
        different messages — the UI can only tell them apart if the
        backend does."""
        test = self._test(is_pro=True)
        self._free_starter('mock_test', quantity=1, used=1)
        block = _resolve(self.student, test)
        self.assertEqual(block['state'], 'locked')
        self.assertEqual(block['reason_code'], 'free_limit_reached')
        self.assertTrue(block['upgrade_available'])

    def test_in_progress_attempt_yields_continue(self):
        test = self._test(is_pro=False)
        attempt = TestAttempt.objects.create(user=self.student, test=test)
        block = _resolve(self.student, test)
        self.assertEqual(block['state'], 'continue')
        self.assertTrue(block['can_continue'])
        self.assertEqual(block['in_progress_attempt_id'], attempt.id)

    def test_completed_attempt_yields_review_with_the_attempt_to_open(self):
        test = self._test(is_pro=False, max_attempts=1)
        attempt = TestAttempt.objects.create(
            user=self.student, test=test, status='submitted', score=8, end_time=timezone.now(),
        )
        block = _resolve(self.student, test)
        self.assertEqual(block['state'], 'review')
        self.assertTrue(block['can_review'])
        self.assertEqual(block['latest_attempt_id'], attempt.id)

    def test_attempts_exhausted_without_a_submitted_attempt(self):
        """max_attempts used up by attempts that never produced a
        reviewable result — distinct from 'review'."""
        test = self._test(is_pro=False, max_attempts=1)
        TestAttempt.objects.create(user=self.student, test=test, status='abandoned')
        block = _resolve(self.student, test)
        self.assertEqual(block['state'], 'attempts_exhausted')
        self.assertEqual(block['reason_code'], 'attempt_limit_reached')

    def test_remaining_attempts_are_reported(self):
        test = self._test(is_pro=False, max_attempts=3)
        TestAttempt.objects.create(user=self.student, test=test, status='submitted', score=1)
        block = _resolve(self.student, test)
        self.assertEqual(block['attempts_left'], 2)

    def test_anonymous_visitor_sees_a_locked_pro_card_not_an_error(self):
        from django.contrib.auth.models import AnonymousUser

        test = self._test(is_pro=True)
        block = _resolve(AnonymousUser(), test)
        self.assertEqual(block['state'], 'locked')
        self.assertTrue(block['upgrade_available'])


class SessionAwareCardStateTests(Phase10Base):
    """Phase 6 session windows drive upcoming/closed — from server time,
    not the browser clock, and not from the legacy Test.scheduled_* fields
    the old `card_status` used."""

    def _session(self, test, start, end):
        template = ExamTemplate.objects.create(title=test.title, exam_type=test.exam_type)
        test.exam_template = template
        test.save(update_fields=['exam_template'])
        return ExamSession.objects.create(
            exam_template=template, exam_version=test, session_name='S',
            start_datetime=start, end_datetime=end, status='scheduled',
        )

    def test_session_not_yet_open_is_upcoming(self):
        test = self._test(exam_type='daily', is_pro=False)
        now = timezone.now()
        session = self._session(test, now + timezone.timedelta(hours=2), now + timezone.timedelta(hours=6))
        block = _resolve(self.student, test, session=session)
        self.assertEqual(block['state'], 'upcoming')
        self.assertEqual(block['reason_code'], 'exam_not_open')
        self.assertFalse(block['can_start'])

    def test_open_session_is_startable(self):
        test = self._test(exam_type='daily', is_pro=False)
        now = timezone.now()
        session = self._session(test, now - timezone.timedelta(hours=1), now + timezone.timedelta(hours=2))
        block = _resolve(self.student, test, session=session)
        self.assertEqual(block['state'], 'start')

    def test_closed_session_never_attempted_is_closed(self):
        test = self._test(exam_type='daily', is_pro=False)
        now = timezone.now()
        session = self._session(test, now - timezone.timedelta(hours=5), now - timezone.timedelta(hours=1))
        block = _resolve(self.student, test, session=session)
        self.assertEqual(block['state'], 'closed')
        self.assertEqual(block['reason_code'], 'exam_closed')

    def test_closed_session_with_a_completed_attempt_stays_reviewable(self):
        """A closed window must not hide a result the student earned."""
        test = self._test(exam_type='daily', is_pro=False)
        now = timezone.now()
        session = self._session(test, now - timezone.timedelta(hours=5), now - timezone.timedelta(hours=1))
        attempt = TestAttempt.objects.create(
            user=self.student, test=test, session=session, status='submitted', score=5, end_time=now,
        )
        block = _resolve(self.student, test, session=session)
        self.assertEqual(block['state'], 'review')
        self.assertEqual(block['latest_attempt_id'], attempt.id)


class GrandTestScheduleCardStateTests(Phase10Base):
    """Release Candidate audit fix — a Grand Test scheduled through the
    simpler Test.scheduled_start/scheduled_end fallback (no real
    ExamSession) was previously invisible to resolve_card_access: the
    card showed 'Start Test' on an upcoming or already-missed exam. The
    fix reuses grand_test_participation_status (the same canonical
    function _start_attempt enforces against), so card and server never
    disagree."""

    def _grand(self, start, end, entitled=True):
        test = self._test(exam_type='grand', is_pro=True, scheduled_start=start, scheduled_end=end)
        if entitled:
            purchase = Purchase.objects.create(
                user=self.student, kind='grand_test', grand_test=test,
                original_amount=50, final_amount=50, status='approved',
            )
            GrandTestAccess.objects.create(user=self.student, test=test, purchase=purchase)
        return test

    def test_upcoming_grand_test_is_not_startable(self):
        now = timezone.now()
        test = self._grand(now + timezone.timedelta(hours=2), now + timezone.timedelta(hours=5))
        block = _resolve(self.student, test)
        self.assertEqual(block['state'], 'upcoming')
        self.assertEqual(block['reason_code'], 'exam_not_open')
        self.assertFalse(block['can_start'])

    def test_missed_grand_test_reports_missed_not_start(self):
        now = timezone.now()
        test = self._grand(now - timezone.timedelta(hours=5), now - timezone.timedelta(hours=1))
        block = _resolve(self.student, test)
        self.assertEqual(block['state'], 'missed')
        self.assertEqual(block['reason_code'], 'exam_missed')
        self.assertFalse(block['can_start'])

    def test_live_grand_test_is_startable(self):
        now = timezone.now()
        test = self._grand(now - timezone.timedelta(hours=1), now + timezone.timedelta(hours=2))
        block = _resolve(self.student, test)
        self.assertEqual(block['state'], 'start')
        self.assertTrue(block['can_start'])

    def test_missed_grand_test_with_a_completed_attempt_stays_reviewable(self):
        now = timezone.now()
        test = self._grand(now - timezone.timedelta(hours=5), now - timezone.timedelta(hours=1))
        TestAttempt.objects.create(user=self.student, test=test, status='submitted', score=3, end_time=now)
        block = _resolve(self.student, test)
        # An existing attempt outranks a "missed" schedule verdict — the
        # student did participate; grand_test_participation_status returns
        # 'completed', not 'missed', so this falls through to review.
        self.assertEqual(block['state'], 'review')

    def test_unscheduled_grand_test_is_unaffected(self):
        test = self._grand(None, None)
        block = _resolve(self.student, test)
        self.assertEqual(block['state'], 'start')

    def test_upcoming_grand_test_without_entitlement_still_shows_schedule_first(self):
        """Matches every other scheduled exam type's precedent
        (_session_block runs before the entitlement branch): schedule
        state is shown regardless of whether the student has bought in."""
        now = timezone.now()
        test = self._grand(now + timezone.timedelta(hours=2), now + timezone.timedelta(hours=5), entitled=False)
        block = _resolve(self.student, test)
        self.assertEqual(block['state'], 'upcoming')


class CardAccessMatchesCanStartTestTests(Phase10Base):
    """The guard rail: the batched card resolver and Phase 4's canonical
    `can_start_test()` must agree on startability for every entitlement
    shape. If someone changes one without the other, this fails."""

    def _assert_agrees(self, test, label):
        canonical = can_start_test(self.student, test)
        block = _resolve(self.student, test)
        # `continue` is can_start_test's own "already in progress — resume"
        # allowed case, so it counts as agreement on startability.
        card_allows = block['can_start'] or block['can_continue']
        self.assertEqual(
            card_allows, canonical.allowed,
            f'{label}: card says {block["state"]} (allowed={card_allows}), '
            f'can_start_test says allowed={canonical.allowed} ({canonical.reason_code})',
        )

    def test_free_test(self):
        self._assert_agrees(self._test(is_pro=False), 'free test')

    def test_pro_no_entitlement(self):
        self._assert_agrees(self._test(is_pro=True), 'pro, nothing')

    def test_pro_with_subscription(self):
        test = self._test(is_pro=True)
        self._subscribe('mock_test')
        self._assert_agrees(test, 'pro + subscription')

    def test_pro_with_free_starter(self):
        test = self._test(is_pro=True)
        self._free_starter('mock_test', quantity=1, used=0)
        self._assert_agrees(test, 'pro + free starter')

    def test_pro_with_exhausted_free_starter(self):
        test = self._test(is_pro=True)
        self._free_starter('mock_test', quantity=1, used=1)
        self._assert_agrees(test, 'pro + exhausted free starter')

    def test_daily_pro_with_subscription(self):
        test = self._test(exam_type='daily', is_pro=True)
        self._subscribe('daily_test')
        self._assert_agrees(test, 'daily pro + daily subscription')

    def test_pyq_pro_without_entitlement(self):
        self._assert_agrees(self._test(exam_type='pyq', is_pro=True), 'pyq pro, nothing')

    def test_grand_pro_without_entitlement(self):
        self._assert_agrees(self._test(exam_type='grand', is_pro=True), 'grand pro, nothing')

    def test_grand_pro_with_purchase(self):
        test = self._test(exam_type='grand', is_pro=True)
        purchase = Purchase.objects.create(
            user=self.student, kind='grand_test', grand_test=test,
            original_amount=100, final_amount=100, status='approved',
        )
        GrandTestAccess.objects.create(user=self.student, test=test, purchase=purchase)
        self._assert_agrees(test, 'grand pro + purchase')

    def test_grand_pro_with_free_starter(self):
        """Regression for the Phase 10 correction: can_start_test used to
        deny this while _start_attempt allowed it, so the card would have
        shown a paywall on an exam the student could actually start."""
        test = self._test(exam_type='grand', is_pro=True)
        self._free_starter('grand_test', quantity=1, used=0)
        self._assert_agrees(test, 'grand pro + free starter')
        self.assertEqual(_resolve(self.student, test)['state'], 'start')

    def test_attempt_limit_reached(self):
        test = self._test(is_pro=False, max_attempts=1)
        TestAttempt.objects.create(user=self.student, test=test, status='submitted', score=1)
        self._assert_agrees(test, 'attempt limit reached')

    def test_in_progress_attempt(self):
        test = self._test(is_pro=False, max_attempts=2)
        TestAttempt.objects.create(user=self.student, test=test)
        self._assert_agrees(test, 'in progress')


class CrossSourceCardTests(Phase10Base):
    """One expired/exhausted source must never make the card claim the
    student has lost access they still hold elsewhere."""

    def test_exhausted_free_starter_plus_active_subscription_still_starts(self):
        test = self._test(is_pro=True)
        self._free_starter('mock_test', quantity=1, used=1)
        self._subscribe('mock_test')
        block = _resolve(self.student, test)
        self.assertEqual(block['state'], 'start')
        self.assertEqual(block['source'], 'subscription')

    def test_expired_subscription_plus_free_starter_still_starts(self):
        test = self._test(is_pro=True)
        Subscription.objects.create(
            user=self.student, course=self.course, product_type='mock_test',
            expires_at=timezone.now() - timezone.timedelta(days=1), is_active=True,
        )
        self._free_starter('mock_test', quantity=1, used=0)
        block = _resolve(self.student, test)
        self.assertEqual(block['state'], 'start')
        self.assertEqual(block['source'], 'free_starter')

    def test_refunded_grand_test_purchase_no_longer_starts(self):
        """Phase 9 revocation surfaces correctly on the card."""
        test = self._test(exam_type='grand', is_pro=True)
        purchase = Purchase.objects.create(
            user=self.student, kind='grand_test', grand_test=test,
            original_amount=100, final_amount=100, status='approved',
        )
        access = GrandTestAccess.objects.create(user=self.student, test=test, purchase=purchase)
        self.assertEqual(_resolve(self.student, test)['state'], 'start')

        access.revoked_at = timezone.now()
        access.save(update_fields=['revoked_at'])

        self.assertEqual(_resolve(self.student, test)['state'], 'locked')


class CardAccessApiTests(APITestCase):
    """The contract as the frontend actually receives it, plus the
    query-count bound that makes it safe to put on a catalog page."""

    def setUp(self):
        self.student = User.objects.create_user(username='api_student', email='api_student@example.com', password='pw')
        self.course = Course.objects.create(name='API Course', prefix='APC')
        Enrollment.objects.create(user=self.student, course=self.course, is_active=True)
        self.subject = Subject.objects.create(name='API Subject', is_free=False)
        self.subject.courses.set([self.course])
        self.client.force_authenticate(user=self.student)

    def _make_tests(self, count, **overrides):
        made = []
        for i in range(count):
            fields = {'title': f'Test {i}', 'exam_type': 'mock', 'is_draft': False, 'duration_minutes': 60}
            fields.update(overrides)
            test = Test.objects.create(**fields)
            test.courses.set([self.course])
            question = Question.objects.create(subject=self.subject, text=f'Q{i}', marks=1, negative_marks=0)
            Option.objects.create(question=question, text='A', order=0, is_correct=True)
            TestQuestion.objects.create(test=test, question=question)
            made.append(test)
        return made

    def test_list_endpoint_exposes_the_access_block(self):
        self._make_tests(1, is_pro=True)
        resp = self.client.get('/api/tests/?exam_type=mock')
        self.assertEqual(resp.status_code, 200)
        rows = resp.data['results'] if isinstance(resp.data, dict) else resp.data
        block = rows[0]['access']
        self.assertEqual(block['state'], 'locked')
        self.assertEqual(block['reason_code'], 'purchase_required')
        self.assertIn('can_start', block)
        self.assertIn('upgrade_available', block)

    def test_access_block_reflects_a_subscription(self):
        self._make_tests(1, is_pro=True)
        Subscription.objects.create(
            user=self.student, course=self.course, product_type='mock_test',
            expires_at=timezone.now() + timezone.timedelta(days=30), is_active=True,
        )
        resp = self.client.get('/api/tests/?exam_type=mock')
        rows = resp.data['results'] if isinstance(resp.data, dict) else resp.data
        self.assertEqual(rows[0]['access']['state'], 'start')

    def _count_queries(self, fn):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            fn()
        return len(ctx)

    def test_query_count_does_not_grow_with_the_number_of_cards(self):
        """The whole reason the resolver is batched: entitlement cost is
        per-request, not per-card. Five cards must not cost five times what
        one costs — asserted as a relationship between two real requests
        rather than a pinned magic number, so it keeps holding as the
        surrounding queryset evolves."""
        self._make_tests(1, is_pro=True)
        one_card = self._count_queries(lambda: self.client.get('/api/tests/?exam_type=mock'))

        self._make_tests(4, is_pro=True)
        five_cards = self._count_queries(lambda: self.client.get('/api/tests/?exam_type=mock'))

        self.assertEqual(
            one_card, five_cards,
            f'query count grew with card count ({one_card} → {five_cards}) — the entitlement '
            'snapshot is no longer being shared across the page',
        )


class PreviewAsStudentTests(APITestCase):
    """Phase 10 plan bullet 3 — an admin sees the catalog as one student
    sees it, read-only, without impersonating them or writing anything to
    their account."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username='p10_admin', email='p10_admin@example.com', password='pw', is_staff=True, admin_role='admin',
        )
        self.editor = User.objects.create_user(
            username='p10_editor', email='p10_editor@example.com', password='pw', is_staff=True, admin_role='editor',
        )
        self.student = User.objects.create_user(username='p10_stu', email='p10_stu@example.com', password='pw')
        self.other_student = User.objects.create_user(username='p10_stu2', email='p10_stu2@example.com', password='pw')

        self.course = Course.objects.create(name='Preview Course', prefix='PVC')
        Enrollment.objects.create(user=self.student, course=self.course, is_active=True)
        self.subject = Subject.objects.create(name='Preview Subject', is_free=False)
        self.subject.courses.set([self.course])

        self.test = Test.objects.create(
            title='Preview Test', exam_type='mock', is_draft=False, is_pro=True, duration_minutes=60,
        )
        self.test.courses.set([self.course])
        question = Question.objects.create(subject=self.subject, text='Q?', marks=1, negative_marks=0)
        Option.objects.create(question=question, text='A', order=0, is_correct=True)
        TestQuestion.objects.create(test=self.test, question=question)

    def _card(self, resp):
        rows = resp.data['results'] if isinstance(resp.data, dict) else resp.data
        return next((r for r in rows if r['id'] == self.test.id), None)

    def test_admin_preview_shows_the_students_locked_state(self):
        self.client.force_authenticate(user=self.admin)
        card = self._card(self.client.get(f'/api/tests/?exam_type=mock&preview_as={self.student.id}'))
        self.assertIsNotNone(card, 'the previewed student can see this test, so the admin preview should too')
        self.assertEqual(card['access']['state'], 'locked')
        self.assertEqual(card['access']['reason_code'], 'purchase_required')

    def test_admin_preview_reflects_the_students_subscription_not_the_admins_bypass(self):
        Subscription.objects.create(
            user=self.student, course=self.course, product_type='mock_test',
            expires_at=timezone.now() + timezone.timedelta(days=30), is_active=True,
        )
        self.client.force_authenticate(user=self.admin)
        card = self._card(self.client.get(f'/api/tests/?exam_type=mock&preview_as={self.student.id}'))
        self.assertEqual(card['access']['state'], 'start')
        self.assertEqual(card['access']['source'], 'subscription')

    def test_preview_never_writes_to_the_previewed_students_account(self):
        """The plan's hard requirement. The normal entitlement path lazily
        provisions a FreeStarterEntitlement; preview must not."""
        FreeStarterPolicy.objects.update_or_create(
            resource_type='mock_test', defaults={'quantity': 1, 'is_active': True},
        )
        self.assertEqual(FreeStarterEntitlement.objects.filter(user=self.student).count(), 0)

        self.client.force_authenticate(user=self.admin)
        self.client.get(f'/api/tests/?exam_type=mock&preview_as={self.student.id}')

        self.assertEqual(
            FreeStarterEntitlement.objects.filter(user=self.student).count(), 0,
            'previewing a student provisioned free-starter rows against their account',
        )
        self.assertFalse(TestAttempt.objects.filter(user=self.student).exists())

    def test_a_non_admin_staff_account_cannot_preview(self):
        self.client.force_authenticate(user=self.editor)
        resp = self.client.get(f'/api/tests/?exam_type=mock&preview_as={self.student.id}')
        # Silently ignored — the editor just gets their own view, not an error.
        self.assertEqual(resp.status_code, 200)

    def test_a_student_cannot_preview_another_student(self):
        """The parameter must never become a way to read someone else's
        entitlement state."""
        Subscription.objects.create(
            user=self.other_student, course=self.course, product_type='mock_test',
            expires_at=timezone.now() + timezone.timedelta(days=30), is_active=True,
        )
        Enrollment.objects.create(user=self.other_student, course=self.course, is_active=True)
        self.client.force_authenticate(user=self.student)

        card = self._card(self.client.get(f'/api/tests/?exam_type=mock&preview_as={self.other_student.id}'))

        # Resolved as the requesting student (locked), never as the target.
        self.assertEqual(card['access']['state'], 'locked')

    def test_preview_of_an_unknown_or_staff_user_falls_back_to_the_admins_own_view(self):
        self.client.force_authenticate(user=self.admin)
        for target in ('999999', 'not-a-number', str(self.editor.id)):
            resp = self.client.get(f'/api/tests/?exam_type=mock&preview_as={target}')
            self.assertEqual(resp.status_code, 200, target)

    def test_preview_is_get_only(self):
        """A write must never be resolved through someone else's context."""
        from tests_app.preview import resolve_preview_user

        class FakeRequest:
            method = 'POST'
            query_params = {'preview_as': str(self.student.id)}
            user = self.admin

        self.assertIsNone(resolve_preview_user(FakeRequest()))
