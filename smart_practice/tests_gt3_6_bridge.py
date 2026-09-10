"""Grand Test 3.0 / GT3-6 — smart_practice.grand_test_bridge.

Core rules under test:

1. resolve_source_scope()/create_session() (the ordinary, pre-existing
   entry points) remain BYTE-FOR-BYTE UNCHANGED — still reject
   exam_type='grand' exactly as before this phase (regression guard).
2. resolve_grand_test_source_scope() requires an APPEARED (participation
   'completed') Grand Test with a real submitted attempt — never a
   missed/upcoming/in-progress one.
3. create_grand_test_practice_session() is hardcoded to
   mode='source_weak_areas' — the one safe mode — and produces an
   ordinary SmartPracticeSession row.
4. diagnose_grand_test_performance() correctly identifies a weak subject
   only when there is enough attempted data, and counts unanswered
   questions even when no Answer row exists for them at all.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from academics.models import Chapter, Option, Question, Subject, Topic
from billing import payment_service
from billing.access import get_grand_test_access
from billing.models import Purchase
from tests_app.models import Answer, Test, TestAttempt, TestQuestion

from .access import SourceScopeError, resolve_source_scope
from .grand_test_bridge import (
    GrandTestRecommendationError,
    create_grand_test_practice_session,
    diagnose_grand_test_performance,
    resolve_grand_test_source_scope,
)
from .services import create_session

User = get_user_model()


def _mkq(subject, chapter=None, topic=None, text='Q'):
    q = Question.objects.create(subject=subject, chapter=chapter, topic=topic, text=text, marks=1, negative_marks=0)
    correct = Option.objects.create(question=q, text='Right', order=0, is_correct=True)
    wrong = Option.objects.create(question=q, text='Wrong', order=1, is_correct=False)
    return q, correct, wrong


def _grant_access(user, test, price=1000):
    purchase = Purchase.objects.create(
        user=user, kind='grand_test', grand_test=test, original_amount=price, final_amount=price, status='pending',
    )
    with patch('billing.payment_service._send_grand_test_email'):
        payment_service.activate(purchase.id)
    return get_grand_test_access(user, test)


class GrandTestBridgeFixture(APITestCase):
    """One Grand Test spanning two subjects: Physics (both wrong — weak)
    and Chemistry (both correct — strong), plus one question left
    completely untouched (no Answer row at all)."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt6_a', email='gt6_a@example.com', password='pw12345')

        self.physics = Subject.objects.create(name='GT6 Physics')
        self.phys_chapter = Chapter.objects.create(subject=self.physics, name='Mechanics')
        self.phys_topic = Topic.objects.create(chapter=self.phys_chapter, name='Kinematics')
        self.p1, self.p1c, self.p1w = _mkq(self.physics, self.phys_chapter, self.phys_topic, 'P1')
        self.p2, self.p2c, self.p2w = _mkq(self.physics, self.phys_chapter, self.phys_topic, 'P2')

        self.chemistry = Subject.objects.create(name='GT6 Chemistry')
        self.chem_chapter = Chapter.objects.create(subject=self.chemistry, name='Organic')
        self.chem_topic = Topic.objects.create(chapter=self.chem_chapter, name='Alkanes')
        self.c1, self.c1c, self.c1w = _mkq(self.chemistry, self.chem_chapter, self.chem_topic, 'C1')
        self.c2, self.c2c, self.c2w = _mkq(self.chemistry, self.chem_chapter, self.chem_topic, 'C2')

        # A 5th question the student never touches at all.
        self.untouched, self.uc, self.uw = _mkq(self.chemistry, self.chem_chapter, self.chem_topic, 'U1')

        now = timezone.now()
        self.test = Test.objects.create(
            title='GT6 Grand', exam_type='grand', is_pro=True, price=1000, max_attempts=1, is_draft=False,
            scheduled_start=now - timezone.timedelta(hours=2), scheduled_end=now - timezone.timedelta(minutes=1),
        )
        self.test.assigned_students.set([self.student])
        for i, q in enumerate([self.p1, self.p2, self.c1, self.c2, self.untouched]):
            TestQuestion.objects.create(test=self.test, question=q, order=i)

        self.attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='submitted', score=2)
        Answer.objects.create(attempt=self.attempt, question=self.p1, selected_option=self.p1w, is_correct=False)
        Answer.objects.create(attempt=self.attempt, question=self.p2, selected_option=self.p2w, is_correct=False)
        Answer.objects.create(attempt=self.attempt, question=self.c1, selected_option=self.c1c, is_correct=True)
        Answer.objects.create(attempt=self.attempt, question=self.c2, selected_option=self.c2c, is_correct=True)
        # self.untouched: deliberately no Answer row at all.

        _grant_access(self.student, self.test)
        self.client.force_authenticate(user=self.student)


class OrdinaryEntryPointsUnaffectedTests(GrandTestBridgeFixture):
    """Regression: the ordinary smart_practice entry points must still
    reject a Grand Test exactly as before this phase."""

    def test_resolve_source_scope_still_rejects_grand_test(self):
        with self.assertRaises(SourceScopeError) as ctx:
            resolve_source_scope(self.student, self.test.id)
        self.assertEqual(ctx.exception.code, 'grand_test_excluded')

    def test_create_session_still_rejects_grand_test(self):
        with self.assertRaises(SourceScopeError) as ctx:
            create_session(self.student, self.test.id, 'retry_mistakes')
        self.assertEqual(ctx.exception.code, 'grand_test_excluded')


class ResolveGrandTestSourceScopeTests(GrandTestBridgeFixture):
    def test_appeared_entitled_student_is_authorized(self):
        ctx = resolve_grand_test_source_scope(self.student, self.test)
        self.assertEqual(ctx.test.id, self.test.id)
        self.assertEqual(ctx.attempt.id, self.attempt.id)
        self.assertIn(self.physics.id, ctx.subject_ids)
        self.assertIn(self.chemistry.id, ctx.subject_ids)

    def test_non_grand_test_is_rejected(self):
        daily = Test.objects.create(title='Daily', exam_type='daily', is_draft=False)
        with self.assertRaises(GrandTestRecommendationError) as ctx:
            resolve_grand_test_source_scope(self.student, daily)
        self.assertEqual(ctx.exception.code, 'not_a_grand_test')

    def test_unentitled_student_is_rejected(self):
        other = User.objects.create_user(username='gt6_b', email='gt6_b@example.com', password='pw12345')
        with self.assertRaises(GrandTestRecommendationError) as ctx:
            resolve_grand_test_source_scope(other, self.test)
        self.assertEqual(ctx.exception.code, 'not_entitled')

    def test_missed_student_is_rejected_with_no_performance_data_code(self):
        """A missed student has no attempt to build a SourceContext from
        — never fabricated (GT3-6's own explicit rule)."""
        missed_student = User.objects.create_user(username='gt6_c', email='gt6_c@example.com', password='pw12345')
        self.test.assigned_students.add(missed_student)
        _grant_access(missed_student, self.test)
        with self.assertRaises(GrandTestRecommendationError) as ctx:
            resolve_grand_test_source_scope(missed_student, self.test)
        self.assertEqual(ctx.exception.code, 'missed_no_performance_data')

    def test_not_yet_appeared_student_is_rejected(self):
        upcoming = Test.objects.create(
            title='GT6 Upcoming', exam_type='grand', is_pro=True, price=1000, max_attempts=1, is_draft=False,
            scheduled_start=timezone.now() + timezone.timedelta(hours=1),
            scheduled_end=timezone.now() + timezone.timedelta(hours=3),
        )
        upcoming.assigned_students.set([self.student])
        _grant_access(self.student, upcoming)
        with self.assertRaises(GrandTestRecommendationError) as ctx:
            resolve_grand_test_source_scope(self.student, upcoming)
        self.assertEqual(ctx.exception.code, 'not_yet_appeared')


class CreateGrandTestPracticeSessionTests(GrandTestBridgeFixture):
    def test_session_is_created_with_source_weak_areas_mode_only(self):
        session = create_grand_test_practice_session(self.student, self.test)
        self.assertEqual(session.mode, 'source_weak_areas')
        self.assertEqual(session.source_test_id, self.test.id)
        self.assertEqual(session.source_attempt_id, self.attempt.id)
        self.assertGreater(session.question_count, 0)

    def test_endpoint_creates_a_real_session(self):
        resp = self.client.post('/api/student/smart-practice/grand-test-sessions/', {'test_id': self.test.id})
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['mode'], 'source_weak_areas')

    def test_endpoint_rejects_missed_student(self):
        missed_student = User.objects.create_user(username='gt6_d', email='gt6_d@example.com', password='pw12345')
        self.test.assigned_students.add(missed_student)
        _grant_access(missed_student, self.test)
        self.client.force_authenticate(user=missed_student)
        resp = self.client.post('/api/student/smart-practice/grand-test-sessions/', {'test_id': self.test.id})
        self.assertEqual(resp.data['code'], 'missed_no_performance_data')


class DiagnoseGrandTestPerformanceTests(GrandTestBridgeFixture):
    def test_weak_subject_identified_correctly(self):
        ctx = resolve_grand_test_source_scope(self.student, self.test)
        diagnosis = diagnose_grand_test_performance(ctx)
        self.assertIsNotNone(diagnosis.weak_subject)
        self.assertEqual(diagnosis.weak_subject['subject_id'], self.physics.id)
        self.assertEqual(diagnosis.weak_subject['accuracy'], 0.0)

    def test_unanswered_count_includes_a_question_with_no_answer_row_at_all(self):
        ctx = resolve_grand_test_source_scope(self.student, self.test)
        diagnosis = diagnose_grand_test_performance(ctx)
        self.assertEqual(diagnosis.unanswered_count, 1)
        self.assertEqual(diagnosis.correct_count, 2)
        self.assertEqual(diagnosis.incorrect_count, 2)
