from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from academics.models import Option, Question, QuestionAttempt, QuestionEvent, Subject
from core.models import DeletionAuditLog
from tests_app.models import ExamSession, ExamTemplate, Test, TestAttempt, TestQuestion

User = get_user_model()


class TestDeleteTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='staff1', email='staff1@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(username='student1', email='student1@example.com', password='pw12345')
        self.client.force_authenticate(user=self.staff)

    def test_blocked_when_test_has_student_attempts(self):
        test = Test.objects.create(title='Mock Test 1', exam_type='mock')
        TestAttempt.objects.create(user=self.student, test=test, status='submitted')

        resp = self.client.delete(f'/api/tests/{test.id}/')

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(Test.objects.filter(id=test.id).exists())
        entry = DeletionAuditLog.objects.get(resource_type='Test', resource_id=str(test.id))
        self.assertEqual(entry.result, 'failure')

    def test_blocked_when_version_has_a_session_even_without_attempts(self):
        """Regression test for the PROTECT edge case found during the audit:
        a Test version with a session (but zero attempts, and not the sole
        version under its template) used to reach super().destroy() and hit
        an unhandled ProtectedError -> 500. It must now return a clean 400."""
        template = ExamTemplate.objects.create(title='CEE Mock #1', exam_type='mock')
        version_one = Test.objects.create(title='CEE Mock #1 v1', exam_type='mock', exam_template=template, version_number=1)
        version_two = Test.objects.create(title='CEE Mock #1 v2', exam_type='mock', exam_template=template, version_number=2)
        now = timezone.now()
        ExamSession.objects.create(
            exam_template=template, exam_version=version_two,
            session_name='Session 1', start_datetime=now, end_datetime=now,
        )

        # version_one has no session of its own and is not the sole version,
        # so the *old* buggy guard would have let this reach super().destroy()
        # unguarded. Only version_two (which has the session) should be blocked.
        resp_v1 = self.client.delete(f'/api/tests/{version_one.id}/')
        self.assertEqual(resp_v1.status_code, status.HTTP_204_NO_CONTENT)

        resp_v2 = self.client.delete(f'/api/tests/{version_two.id}/')
        self.assertEqual(resp_v2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(Test.objects.filter(id=version_two.id).exists())

    def test_permanent_delete_succeeds_for_unused_test(self):
        test = Test.objects.create(title='Draft Mock', exam_type='mock')

        resp = self.client.delete(f'/api/tests/{test.id}/')

        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Test.objects.filter(id=test.id).exists())
        entry = DeletionAuditLog.objects.get(resource_type='Test')
        self.assertEqual(entry.result, 'success')


class ExamSessionDeleteTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='staff1', email='staff1@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(username='student1', email='student1@example.com', password='pw12345')
        self.client.force_authenticate(user=self.staff)
        self.template = ExamTemplate.objects.create(title='CEE Mock #1', exam_type='mock')
        self.version = Test.objects.create(title='CEE Mock #1 v1', exam_type='mock', exam_template=self.template)
        now = timezone.now()
        self.session = ExamSession.objects.create(
            exam_template=self.template, exam_version=self.version,
            session_name='Session 1', start_datetime=now, end_datetime=now,
        )

    def test_blocked_when_session_has_attempts(self):
        TestAttempt.objects.create(user=self.student, test=self.version, session=self.session, status='submitted')

        resp = self.client.delete(f'/api/exam-sessions/{self.session.id}/')

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(ExamSession.objects.filter(id=self.session.id).exists())
        entry = DeletionAuditLog.objects.get(resource_type='ExamSession', resource_id=str(self.session.id))
        self.assertEqual(entry.result, 'failure')

    def test_permanent_delete_succeeds_for_session_with_no_attempts(self):
        resp = self.client.delete(f'/api/exam-sessions/{self.session.id}/')

        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ExamSession.objects.filter(id=self.session.id).exists())
        entry = DeletionAuditLog.objects.get(resource_type='ExamSession')
        self.assertEqual(entry.result, 'success')


class SubmitTestFeedsQuestionPerformanceTests(APITestCase):
    """Test submission must feed academics.QuestionAttempt/QuestionEvent
    platform-wide (the Smart Question Bank's core architecture decision:
    Weak/Mastered/Mistake Bank reflect Daily/Mock/Grand/PYQ activity too,
    not just QBank practice) — additively, without changing scoring."""

    def setUp(self):
        self.student = User.objects.create_user(username='student1', email='student1@example.com', password='pw12345')
        self.subject = Subject.objects.create(name='Physics')
        self.question = Question.objects.create(subject=self.subject, text='2+2=?', marks=1, negative_marks=0)
        self.correct = Option.objects.create(question=self.question, text='4', is_correct=True)
        self.wrong = Option.objects.create(question=self.question, text='5', is_correct=False)
        self.test = Test.objects.create(title='Mock Test 1', exam_type='mock', negative_marking=False)
        TestQuestion.objects.create(test=self.test, question=self.question)
        self.attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='in_progress')
        self.client.force_authenticate(user=self.student)

    def test_submitting_a_test_records_question_performance(self):
        self.client.post(f'/api/attempts/{self.attempt.id}/answer/', {'question_id': self.question.id, 'option_id': self.correct.id})
        resp = self.client.post(f'/api/attempts/{self.attempt.id}/submit/')

        self.assertEqual(resp.status_code, 200)
        qa = QuestionAttempt.objects.get(user=self.student, question=self.question)
        self.assertEqual(qa.attempts_count, 1)
        self.assertEqual(qa.correct_count, 1)
        event = QuestionEvent.objects.get(user=self.student, question=self.question)
        self.assertEqual(event.source, 'test')
        self.assertTrue(event.is_correct)

    def test_changing_the_answer_before_submitting_does_not_overcount(self):
        """SubmitAnswerView is update_or_create and can be hit repeatedly
        while the student is still deciding — only the final submit should
        count as one real attempt."""
        self.client.post(f'/api/attempts/{self.attempt.id}/answer/', {'question_id': self.question.id, 'option_id': self.wrong.id})
        self.client.post(f'/api/attempts/{self.attempt.id}/answer/', {'question_id': self.question.id, 'option_id': self.correct.id})
        self.client.post(f'/api/attempts/{self.attempt.id}/answer/', {'question_id': self.question.id, 'option_id': self.wrong.id})
        self.client.post(f'/api/attempts/{self.attempt.id}/submit/')

        qa = QuestionAttempt.objects.get(user=self.student, question=self.question)
        self.assertEqual(qa.attempts_count, 1)
        self.assertEqual(qa.incorrect_count, 1)
        self.assertEqual(QuestionEvent.objects.filter(user=self.student, question=self.question).count(), 1)

    def test_unanswered_questions_are_not_recorded(self):
        self.client.post(f'/api/attempts/{self.attempt.id}/submit/')
        self.assertFalse(QuestionAttempt.objects.filter(user=self.student, question=self.question).exists())

    def test_qbank_practice_and_test_attempts_accumulate_on_the_same_row(self):
        from academics.services import record_question_result

        record_question_result(self.student, self.question, True, source='qbank')
        self.client.post(f'/api/attempts/{self.attempt.id}/answer/', {'question_id': self.question.id, 'option_id': self.wrong.id})
        self.client.post(f'/api/attempts/{self.attempt.id}/submit/')

        qa = QuestionAttempt.objects.get(user=self.student, question=self.question)
        self.assertEqual(qa.attempts_count, 2)
        self.assertEqual(qa.correct_count, 1)
        self.assertEqual(qa.incorrect_count, 1)
        sources = set(QuestionEvent.objects.filter(user=self.student, question=self.question).values_list('source', flat=True))
        self.assertEqual(sources, {'qbank', 'test'})


class KpiOverviewQuestionsTodayTests(APITestCase):
    """kpi_overview()'s questions_today — powers the Home page Daily Goal
    widget. Must count distinct questions from QuestionEvent (platform-wide:
    QBank practice + every test type) regardless of the overview's own
    date_from/date_to window, and never count yesterday's activity."""

    def setUp(self):
        self.student = User.objects.create_user(username='student1', email='student1@example.com', password='pw12345')
        self.subject = Subject.objects.create(name='Physics')
        self.q1 = Question.objects.create(subject=self.subject, text='Q1')
        self.q2 = Question.objects.create(subject=self.subject, text='Q2')
        self.client.force_authenticate(user=self.student)

    def test_counts_distinct_questions_answered_today_only(self):
        from academics.services import record_question_result

        record_question_result(self.student, self.q1, True, source='qbank')
        record_question_result(self.student, self.q1, False, source='test')  # same question again today
        record_question_result(self.student, self.q2, True, source='qbank')

        yesterday = QuestionEvent.objects.create(
            user=self.student, question=self.q2, is_correct=True, source='qbank',
        )
        yesterday.created_at = timezone.now() - timezone.timedelta(days=1)
        yesterday.save(update_fields=['created_at'])

        resp = self.client.get('/api/performance/overview/')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['kpis']['questions_today'], 2)

    def test_zero_when_no_activity_today(self):
        resp = self.client.get('/api/performance/overview/')
        self.assertEqual(resp.data['kpis']['questions_today'], 0)
