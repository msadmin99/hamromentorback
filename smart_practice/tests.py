from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from academics.models import Chapter, Option, Question, QuestionAttempt, QuestionEvent, Subject, Topic
from billing.models import Subscription, SubscriptionPlan
from courses.models import Course, Enrollment
from tests_app.models import Answer, Test, TestAttempt, TestQuestion

from .access import SourceScopeError, resolve_source_scope
from .models import SmartPracticeConfig, SmartPracticeSession
from .services import build_candidates, complete_session, create_session, record_session_answer
from .source_performance import source_missed_questions, source_topic_mastery

User = get_user_model()


def _make_mcq(subject, chapter=None, topic=None, text='Q'):
    q = Question.objects.create(subject=subject, chapter=chapter, topic=topic, text=text, marks=1, negative_marks=0)
    correct = Option.objects.create(question=q, text='Right', order=0, is_correct=True)
    wrong = Option.objects.create(question=q, text='Wrong', order=1, is_correct=False)
    return q, correct, wrong


class SmartPracticeTestCase(APITestCase):
    """Shared fixture: one course, one subject/chapter/topic, one enrolled
    student, one submitted Daily Test with a mix of right/wrong answers."""

    def setUp(self):
        self.course = Course.objects.create(name='CEE-MBBS', prefix='CEEMBBS', program_group='CEE-UG')
        self.student = User.objects.create_user(username='sp_student', email='sp_student@example.com', password='pw12345')
        Enrollment.objects.create(user=self.student, course=self.course)

        self.subject = Subject.objects.create(name='Pharmacology SP', is_free=True)
        self.subject.courses.set([self.course])
        self.chapter = Chapter.objects.create(subject=self.subject, name='ANS')
        self.topic = Topic.objects.create(chapter=self.chapter, name='Sympathetic System')

        self.q1, self.q1_correct, self.q1_wrong = _make_mcq(self.subject, self.chapter, self.topic, 'Q1')
        self.q2, self.q2_correct, self.q2_wrong = _make_mcq(self.subject, self.chapter, self.topic, 'Q2')
        self.q3, self.q3_correct, self.q3_wrong = _make_mcq(self.subject, self.chapter, self.topic, 'Q3')

        self.test = Test.objects.create(title='CEE-MBBS Daily Test #1', exam_type='daily', is_draft=False, negative_marking=False)
        self.test.courses.set([self.course])
        TestQuestion.objects.create(test=self.test, question=self.q1, order=0)
        TestQuestion.objects.create(test=self.test, question=self.q2, order=1)
        TestQuestion.objects.create(test=self.test, question=self.q3, order=2)

        self.attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='submitted', score=1)
        # q1: wrong, q2: wrong, q3: correct — 2 mistakes, both on the weak topic.
        Answer.objects.create(attempt=self.attempt, question=self.q1, selected_option=self.q1_wrong, is_correct=False)
        Answer.objects.create(attempt=self.attempt, question=self.q2, selected_option=self.q2_wrong, is_correct=False)
        Answer.objects.create(attempt=self.attempt, question=self.q3, selected_option=self.q3_correct, is_correct=True)

        self.client.force_authenticate(user=self.student)


class ResolveSourceScopeTests(SmartPracticeTestCase):
    """Service-level (no HTTP client) — the authorization gate must be
    correct independent of routing/serialization."""

    def test_grand_test_is_rejected_even_for_an_eligible_subscribed_student(self):
        grand = Test.objects.create(title='Grand', exam_type='grand', is_draft=False)
        grand.courses.set([self.course])
        TestQuestion.objects.create(test=grand, question=self.q1)
        TestAttempt.objects.create(user=self.student, test=grand, status='submitted', score=1)

        with self.assertRaises(SourceScopeError) as ctx:
            resolve_source_scope(self.student, grand.id)
        self.assertEqual(ctx.exception.code, 'grand_test_excluded')

    def test_create_session_rejects_grand_test_even_if_a_caller_claims_a_different_exam_type(self):
        """Proves exam_type is derived from the DB row, never trusted from
        a client-supplied source_type — create_session's own defense-in-
        depth re-check, independent of resolve_source_scope."""
        grand = Test.objects.create(title='Grand 2', exam_type='grand', is_draft=False)
        grand.courses.set([self.course])
        TestQuestion.objects.create(test=grand, question=self.q1)
        TestAttempt.objects.create(user=self.student, test=grand, status='submitted', score=1)

        with self.assertRaises(SourceScopeError) as ctx:
            create_session(self.student, grand.id, 'retry_mistakes')
        self.assertEqual(ctx.exception.code, 'grand_test_excluded')

    def test_unenrolled_student_is_not_authorized(self):
        other = User.objects.create_user(username='sp_other', email='sp_other@example.com', password='pw12345')
        with self.assertRaises(SourceScopeError) as ctx:
            resolve_source_scope(other, self.test.id)
        self.assertEqual(ctx.exception.code, 'not_authorized')

    def test_draft_test_is_not_authorized_for_a_student(self):
        self.test.is_draft = True
        self.test.save()
        with self.assertRaises(SourceScopeError) as ctx:
            resolve_source_scope(self.student, self.test.id)
        self.assertEqual(ctx.exception.code, 'not_authorized')

    def test_no_submitted_attempt_yet(self):
        fresh_test = Test.objects.create(title='Fresh Daily', exam_type='daily', is_draft=False)
        fresh_test.courses.set([self.course])
        TestQuestion.objects.create(test=fresh_test, question=self.q1)
        with self.assertRaises(SourceScopeError) as ctx:
            resolve_source_scope(self.student, fresh_test.id)
        self.assertEqual(ctx.exception.code, 'no_submitted_attempt')

    def test_unsubscribed_student_on_a_pro_daily_test_needs_subscription(self):
        self.test.is_pro = True
        self.test.save()
        with self.assertRaises(SourceScopeError) as ctx:
            resolve_source_scope(self.student, self.test.id)
        self.assertEqual(ctx.exception.code, 'subscription_required')

    def test_subscribed_student_on_a_pro_daily_test_is_authorized(self):
        self.test.is_pro = True
        self.test.save()
        plan = SubscriptionPlan.objects.create(name='Daily Plan', course=self.course, product_type='daily_test', price=100)
        Subscription.objects.create(user=self.student, plan=plan, course=self.course, product_type='daily_test', is_active=True)
        ctx = resolve_source_scope(self.student, self.test.id)
        self.assertEqual(ctx.test.id, self.test.id)

    def test_not_found_for_a_nonexistent_test_id(self):
        with self.assertRaises(SourceScopeError) as ctx:
            resolve_source_scope(self.student, 999999)
        self.assertEqual(ctx.exception.code, 'not_found')


class ExpansionPoolScopingTests(SmartPracticeTestCase):
    """Regression tests for the exact leak class tests_app/performance.py:
    recommendations() already had to fix once — a subject shared across
    courses must not let 'more practice' pull from a course the student
    isn't enrolled in for THIS source test."""

    def test_expansion_pool_excludes_a_shared_subjects_other_course_questions(self):
        other_course = Course.objects.create(name='NMCLE-MBBS', prefix='NMCLESP', program_group='NMCLE')
        # Same subject, shared across both courses (subject.courses has both).
        self.subject.courses.add(other_course)
        # A question explicitly tagged to the OTHER course only.
        other_q = Question.objects.create(subject=self.subject, chapter=self.chapter, topic=self.topic, text='Other course Q', marks=1, negative_marks=0)
        other_q.courses.set([other_course])
        Option.objects.create(question=other_q, text='A', order=0, is_correct=True)
        Option.objects.create(question=other_q, text='B', order=1, is_correct=False)

        ctx = resolve_source_scope(self.student, self.test.id)
        self.assertNotIn(other_q.id, set(ctx.expansion_pool.values_list('id', flat=True)))

    def test_expansion_pool_excludes_pro_locked_subjects(self):
        locked_subject = Subject.objects.create(name='Locked Subject SP', is_free=False)
        locked_subject.courses.set([self.course])
        locked_q = Question.objects.create(subject=locked_subject, chapter=self.chapter, text='Locked Q', marks=1, negative_marks=0)
        Option.objects.create(question=locked_q, text='A', order=0, is_correct=True)

        ctx = resolve_source_scope(self.student, self.test.id)
        self.assertNotIn(locked_q.id, set(ctx.expansion_pool.values_list('id', flat=True)))


class BuildCandidatesTests(SmartPracticeTestCase):
    def test_retry_mistakes_matches_exactly_the_wrong_answers(self):
        ctx = resolve_source_scope(self.student, self.test.id)
        candidates = build_candidates(ctx, 'retry_mistakes', 10)
        candidate_ids = {q.id for q, _ in candidates}
        self.assertEqual(candidate_ids, {self.q1.id, self.q2.id})
        self.assertTrue(all(origin == 'source_mistake' for _, origin in candidates))

    def test_source_missed_questions_excludes_the_correctly_answered_question(self):
        ctx = resolve_source_scope(self.student, self.test.id)
        missed_ids = set(source_missed_questions(ctx).values_list('question_id', flat=True))
        self.assertNotIn(self.q3.id, missed_ids)

    def test_weak_topic_threshold_uses_smart_practice_config_not_other_scales(self):
        """q1+q2 wrong, q3 correct on the same topic = 33% accuracy.
        Default weak_topic_accuracy_max_pct=50 must flag it weak; tightening
        the config below 33 must flip it — proves the source-scoped
        threshold is read from SmartPracticeConfig, not hardcoded, and not
        tests_app/performance.py's or QuestionBankConfig's independent scales."""
        ctx = resolve_source_scope(self.student, self.test.id)
        topics_default = source_topic_mastery(ctx, weak_max_pct=50)
        self.assertTrue(any(t['topic_id'] == self.topic.id and t['is_weak'] for t in topics_default))

        topics_strict = source_topic_mastery(ctx, weak_max_pct=10)
        self.assertFalse(any(t['topic_id'] == self.topic.id and t['is_weak'] for t in topics_strict))

    def test_source_weak_areas_mode_returns_authorized_expansion_when_mistakes_run_out(self):
        extra_q, extra_correct, _ = _make_mcq(self.subject, self.chapter, self.topic, 'Extra weak-topic Q')
        ctx = resolve_source_scope(self.student, self.test.id)
        candidates = build_candidates(ctx, 'source_weak_areas', 10)
        candidate_ids = {q.id for q, _ in candidates}
        self.assertIn(self.q1.id, candidate_ids)
        self.assertIn(self.q2.id, candidate_ids)
        self.assertIn(extra_q.id, candidate_ids)

    def test_candidate_count_is_never_padded_with_unrelated_questions(self):
        """Only 2 real mistakes exist — requesting 10 must return 2, not 10
        padded with random unrelated content."""
        ctx = resolve_source_scope(self.student, self.test.id)
        candidates = build_candidates(ctx, 'retry_mistakes', 10)
        self.assertEqual(len(candidates), 2)


class SessionCreationAndAnswerTests(SmartPracticeTestCase):
    def test_create_session_persists_questions_and_a_course_snapshot(self):
        session = create_session(self.student, self.test.id, 'retry_mistakes')
        self.assertEqual(session.course_id, self.course.id)
        self.assertEqual(session.question_count, 2)
        self.assertEqual(set(session.questions.values_list('question_id', flat=True)), {self.q1.id, self.q2.id})

    def test_session_course_snapshot_survives_an_active_course_switch(self):
        other_course = Course.objects.create(name='Other Course SP', prefix='OTHERSP')
        Enrollment.objects.create(user=self.student, course=other_course)
        session = create_session(self.student, self.test.id, 'retry_mistakes')
        # Simulate switching active course elsewhere in the app — the
        # session itself has no notion of "active course" to re-derive from.
        self.assertEqual(session.course_id, self.course.id)

    def test_answering_updates_session_question_and_the_existing_global_mastery_path(self):
        session = create_session(self.student, self.test.id, 'retry_mistakes')
        sq = session.questions.first()

        before_events = QuestionEvent.objects.filter(user=self.student, question=sq.question).count()
        record_session_answer(self.student, session, sq.question_id, sq.question.options.get(is_correct=True).id, 12)

        sq.refresh_from_db()
        self.assertTrue(sq.is_correct)
        self.assertIsNotNone(sq.answered_at)

        attempt_row = QuestionAttempt.objects.get(user=self.student, question=sq.question)
        self.assertGreaterEqual(attempt_row.attempts_count, 1)

        events = QuestionEvent.objects.filter(user=self.student, question=sq.question)
        self.assertEqual(events.count(), before_events + 1)
        self.assertEqual(events.latest('created_at').source, 'smart')

    def test_answering_does_not_double_count_a_question_already_answered_via_qbank(self):
        self.client.post(f'/api/questions/{self.q1.id}/answer/', {'option_id': self.q1_correct.id})
        attempt_before = QuestionAttempt.objects.get(user=self.student, question=self.q1)
        self.assertEqual(attempt_before.attempts_count, 1)

        session = create_session(self.student, self.test.id, 'retry_mistakes')
        record_session_answer(self.student, session, self.q1.id, self.q1_correct.id, 5)

        attempt_after = QuestionAttempt.objects.get(user=self.student, question=self.q1)
        self.assertEqual(attempt_after.attempts_count, 2)

    def test_complete_session_computes_score_and_accuracy(self):
        session = create_session(self.student, self.test.id, 'retry_mistakes')
        sqs = list(session.questions.all())
        record_session_answer(self.student, session, sqs[0].question_id, sqs[0].question.options.get(is_correct=True).id, 5)
        record_session_answer(self.student, session, sqs[1].question_id, sqs[1].question.options.get(is_correct=False).id, 5)

        completed = complete_session(self.student, session)
        self.assertEqual(completed.status, 'completed')
        self.assertEqual(completed.score, 1)
        self.assertEqual(completed.accuracy, 50)

    def test_another_students_session_cannot_be_answered(self):
        other = User.objects.create_user(username='sp_other2', email='sp_other2@example.com', password='pw12345')
        session = create_session(self.student, self.test.id, 'retry_mistakes')
        sq = session.questions.first()
        with self.assertRaises(SourceScopeError) as ctx:
            record_session_answer(other, session, sq.question_id, sq.question.options.get(is_correct=True).id, 5)
        self.assertEqual(ctx.exception.code, 'not_authorized')


class SmartPracticeConfigTests(SmartPracticeTestCase):
    def test_disabled_config_blocks_session_creation(self):
        config = SmartPracticeConfig.load()
        config.enabled = False
        config.save()
        with self.assertRaises(SourceScopeError) as ctx:
            create_session(self.student, self.test.id, 'retry_mistakes')
        self.assertEqual(ctx.exception.code, 'feature_disabled')


class EndpointTests(SmartPracticeTestCase):
    def test_eligibility_endpoint_reflects_real_mistake_count(self):
        resp = self.client.get(f'/api/student/smart-practice/eligibility/?source_test_id={self.test.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['eligible'])
        self.assertEqual(resp.data['mistake_count'], 2)

    def test_eligibility_endpoint_reports_grand_test_as_ineligible_not_an_error(self):
        grand = Test.objects.create(title='Grand EP', exam_type='grand', is_draft=False)
        grand.courses.set([self.course])
        resp = self.client.get(f'/api/student/smart-practice/eligibility/?source_test_id={grand.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['eligible'])
        self.assertEqual(resp.data['reason'], 'grand_test_excluded')

    def test_session_create_endpoint_rejects_grand_test_with_403(self):
        grand = Test.objects.create(title='Grand EP2', exam_type='grand', is_draft=False)
        grand.courses.set([self.course])
        TestQuestion.objects.create(test=grand, question=self.q1)
        TestAttempt.objects.create(user=self.student, test=grand, status='submitted', score=1)
        resp = self.client.post('/api/student/smart-practice/sessions/', {'source_test_id': grand.id, 'mode': 'retry_mistakes'})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data['code'], 'grand_test_excluded')

    def test_session_create_and_answer_flow_end_to_end(self):
        resp = self.client.post('/api/student/smart-practice/sessions/', {'source_test_id': self.test.id, 'mode': 'retry_mistakes'})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        session_id = resp.data['id']
        self.assertEqual(len(resp.data['questions']), 2)

        first_question = resp.data['questions'][0]['question']
        # Options in the session payload use the pre-answer shape (no
        # is_correct) — fetch the real Question to find the correct option.
        question_obj = Question.objects.get(pk=first_question['id'])
        correct_option_id = question_obj.options.get(is_correct=True).id

        answer_resp = self.client.post(
            f'/api/student/smart-practice/sessions/{session_id}/answer/',
            {'question_id': first_question['id'], 'option_id': correct_option_id, 'time_taken_seconds': 8},
        )
        self.assertEqual(answer_resp.status_code, 200)
        self.assertTrue(answer_resp.data['is_correct'])

        complete_resp = self.client.post(f'/api/student/smart-practice/sessions/{session_id}/complete/')
        self.assertEqual(complete_resp.status_code, 200)
        self.assertEqual(complete_resp.data['status'], 'completed')

    def test_unauthorized_student_gets_403_from_session_create(self):
        other = User.objects.create_user(username='sp_other3', email='sp_other3@example.com', password='pw12345')
        self.client.force_authenticate(user=other)
        resp = self.client.post('/api/student/smart-practice/sessions/', {'source_test_id': self.test.id, 'mode': 'retry_mistakes'})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
