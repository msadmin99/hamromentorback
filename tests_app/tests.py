from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from academics.models import Option, Question, QuestionAttempt, QuestionEvent, Subject
from core.models import DeletionAuditLog
from tests_app.models import Answer, ExamSession, ExamTemplate, SavedExamView, Test, TestAttempt, TestQuestion

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


class MarkForReviewViewTests(APITestCase):
    """Test Player redesign: mark-for-review must be settable independently
    of answer() (a real bug fix, not just UI — the old flow lost the mark
    entirely if the student never also answered that question), and must
    never blank an existing answer."""

    def setUp(self):
        self.student = User.objects.create_user(username='mfr_student', email='mfr_student@example.com', password='pw12345')
        self.subject = Subject.objects.create(name='Mark Review Subject')
        self.question = Question.objects.create(subject=self.subject, text='Mark Review Q1', marks=1, negative_marks=0)
        self.correct = Option.objects.create(question=self.question, text='Right', is_correct=True)
        self.wrong = Option.objects.create(question=self.question, text='Wrong', is_correct=False)
        self.test = Test.objects.create(title='Mark Review Test', exam_type='mock')
        TestQuestion.objects.create(test=self.test, question=self.question)
        self.attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='in_progress')
        self.client.force_authenticate(user=self.student)

    def test_marking_a_never_answered_question_creates_a_row_with_no_option(self):
        resp = self.client.post(f'/api/attempts/{self.attempt.id}/mark-review/', {'question_id': self.question.id, 'marked': True})

        self.assertEqual(resp.status_code, 200)
        answer = Answer.objects.get(attempt=self.attempt, question=self.question)
        self.assertTrue(answer.is_marked_for_review)
        self.assertIsNone(answer.selected_option_id)

    def test_marking_an_already_answered_question_does_not_blank_the_answer(self):
        self.client.post(f'/api/attempts/{self.attempt.id}/answer/', {'question_id': self.question.id, 'option_id': self.correct.id})

        resp = self.client.post(f'/api/attempts/{self.attempt.id}/mark-review/', {'question_id': self.question.id, 'marked': True})

        self.assertEqual(resp.status_code, 200)
        answer = Answer.objects.get(attempt=self.attempt, question=self.question)
        self.assertTrue(answer.is_marked_for_review)
        self.assertEqual(answer.selected_option_id, self.correct.id)
        self.assertTrue(answer.is_correct)

    def test_unmarking_clears_the_flag_without_touching_the_answer(self):
        self.client.post(f'/api/attempts/{self.attempt.id}/answer/', {'question_id': self.question.id, 'option_id': self.wrong.id})
        self.client.post(f'/api/attempts/{self.attempt.id}/mark-review/', {'question_id': self.question.id, 'marked': True})

        resp = self.client.post(f'/api/attempts/{self.attempt.id}/mark-review/', {'question_id': self.question.id, 'marked': False})

        self.assertEqual(resp.status_code, 200)
        answer = Answer.objects.get(attempt=self.attempt, question=self.question)
        self.assertFalse(answer.is_marked_for_review)
        self.assertEqual(answer.selected_option_id, self.wrong.id)

    def test_requires_question_id(self):
        resp = self.client.post(f'/api/attempts/{self.attempt.id}/mark-review/', {'marked': True})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_mark_on_a_submitted_attempt(self):
        self.attempt.status = 'submitted'
        self.attempt.save()

        resp = self.client.post(f'/api/attempts/{self.attempt.id}/mark-review/', {'question_id': self.question.id, 'marked': True})

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_requires_auth(self):
        self.client.force_authenticate(user=None)
        resp = self.client.post(f'/api/attempts/{self.attempt.id}/mark-review/', {'question_id': self.question.id, 'marked': True})
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class AttemptDetailRestoresProgressTests(APITestCase):
    """Test Player redesign: reopening an in-progress attempt must restore
    previously-saved answers/marks/bookmarks — the second real bug fix
    (state used to start blank on every page mount)."""

    def setUp(self):
        self.student = User.objects.create_user(username='restore_student', email='restore_student@example.com', password='pw12345')
        self.subject = Subject.objects.create(name='Restore Subject')
        self.q1 = Question.objects.create(subject=self.subject, text='Restore Q1', marks=1, negative_marks=0)
        self.q1_correct = Option.objects.create(question=self.q1, text='Right', is_correct=True)
        Option.objects.create(question=self.q1, text='Wrong', is_correct=False)
        self.q2 = Question.objects.create(subject=self.subject, text='Restore Q2', marks=1, negative_marks=0)
        self.test = Test.objects.create(title='Restore Test', exam_type='mock', shuffle_questions=False)
        TestQuestion.objects.create(test=self.test, question=self.q1, order=0)
        TestQuestion.objects.create(test=self.test, question=self.q2, order=1)
        self.attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='in_progress')
        self.client.force_authenticate(user=self.student)

    def test_get_attempt_returns_previously_saved_answers_and_marks(self):
        self.client.post(f'/api/attempts/{self.attempt.id}/answer/', {'question_id': self.q1.id, 'option_id': self.q1_correct.id})
        self.client.post(f'/api/attempts/{self.attempt.id}/mark-review/', {'question_id': self.q2.id, 'marked': True})

        resp = self.client.get(f'/api/attempts/{self.attempt.id}/')

        self.assertEqual(resp.status_code, 200)
        answers = resp.data['answers']
        self.assertEqual(answers[self.q1.id]['option_id'], self.q1_correct.id)
        self.assertFalse(answers[self.q1.id]['is_marked_for_review'])
        self.assertIsNone(answers[self.q2.id]['option_id'])
        self.assertTrue(answers[self.q2.id]['is_marked_for_review'])

    def test_get_attempt_returns_no_answers_before_any_are_saved(self):
        resp = self.client.get(f'/api/attempts/{self.attempt.id}/')
        self.assertEqual(resp.data['answers'], {})

    def test_question_reflects_a_bookmark_made_from_qbank(self):
        self.client.post(f'/api/questions/{self.q1.id}/bookmark/', {'bookmark': True})

        resp = self.client.get(f'/api/attempts/{self.attempt.id}/')

        questions_by_id = {q['id']: q for q in resp.data['questions']}
        self.assertTrue(questions_by_id[self.q1.id]['is_bookmarked'])
        self.assertFalse(questions_by_id[self.q2.id]['is_bookmarked'])


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


class CardStatusInProgressTests(APITestCase):
    """Daily/Mock Test pages redesign: card_status must distinguish
    'in_progress' (started, not yet submitted) from 'available' (never
    started) so the status tabs and Continue-Test CTA are accurate."""

    def setUp(self):
        from courses.models import Course, Enrollment

        self.course = Course.objects.create(name='In Progress Course', prefix='IPCOURSE')
        self.student = User.objects.create_user(username='ip_student', email='ip_student@example.com', password='pw12345')
        Enrollment.objects.create(user=self.student, course=self.course)
        self.subject = Subject.objects.create(name='In Progress Subject')
        self.q1 = Question.objects.create(subject=self.subject, text='IP Q1', marks=1, negative_marks=0)
        self.q2 = Question.objects.create(subject=self.subject, text='IP Q2', marks=1, negative_marks=0)
        self.test = Test.objects.create(title='In Progress Test', exam_type='mock', is_draft=False)
        self.test.courses.set([self.course])
        TestQuestion.objects.create(test=self.test, question=self.q1, order=0)
        TestQuestion.objects.create(test=self.test, question=self.q2, order=1)
        self.client.force_authenticate(user=self.student)

    def test_never_started_test_is_available_not_in_progress(self):
        resp = self.client.get('/api/tests/?exam_type=mock')
        row = next(r for r in resp.data if r['id'] == self.test.id)
        self.assertEqual(row['card_status'], 'available')
        self.assertIsNone(row['in_progress_answered_count'])

    def test_started_but_unsubmitted_attempt_is_in_progress_with_real_answered_count(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='in_progress')
        Answer.objects.create(attempt=attempt, question=self.q1)

        resp = self.client.get('/api/tests/?exam_type=mock')

        row = next(r for r in resp.data if r['id'] == self.test.id)
        self.assertEqual(row['card_status'], 'in_progress')
        self.assertEqual(row['in_progress_answered_count'], 1)

    def test_submitted_attempt_is_completed_not_in_progress(self):
        TestAttempt.objects.create(user=self.student, test=self.test, status='submitted', score=1)

        resp = self.client.get('/api/tests/?exam_type=mock')

        row = next(r for r in resp.data if r['id'] == self.test.id)
        self.assertEqual(row['card_status'], 'completed')

    def test_another_students_in_progress_attempt_does_not_leak(self):
        other = User.objects.create_user(username='ip_other', email='ip_other@example.com', password='pw12345')
        TestAttempt.objects.create(user=other, test=self.test, status='in_progress')

        resp = self.client.get('/api/tests/?exam_type=mock')

        row = next(r for r in resp.data if r['id'] == self.test.id)
        self.assertEqual(row['card_status'], 'available')


class ExamCourseAccessControlTests(APITestCase):
    """The restructure's own acceptance tests, made literal — Test 1-5 from
    the spec. Confirms exam visibility/access is derived server-side from
    real Enrollment rows, never from a client-supplied ?course= param or
    trust in the frontend not linking to an unauthorized exam."""

    def setUp(self):
        from courses.models import Course, Enrollment

        self.staff = User.objects.create_user(
            username='staff1', email='staff1@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.cee_mbbs = Course.objects.create(name='CEE-MBBS', prefix='CEEMBBS', program_group='CEE-UG')
        self.nmcle_mbbs = Course.objects.create(name='NMCLE-MBBS', prefix='NMCLEMBBS', program_group='NMCLE')
        self.nmcle_bds = Course.objects.create(name='NMCLE-BDS', prefix='NMCLEBDS', program_group='NMCLE')
        self.nlen = Course.objects.create(name='NLEN-PCL Nursing', prefix='NLENPCL', program_group='Nursing Council')

        self.cee_mbbs_student = User.objects.create_user(username='cee_student', email='cee@example.com', password='pw12345')
        Enrollment.objects.create(user=self.cee_mbbs_student, course=self.cee_mbbs)
        self.nmcle_mbbs_student = User.objects.create_user(username='nmcle_mbbs_student', email='nmclembbs@example.com', password='pw12345')
        Enrollment.objects.create(user=self.nmcle_mbbs_student, course=self.nmcle_mbbs)
        self.nmcle_bds_student = User.objects.create_user(username='nmcle_bds_student', email='nmclebds@example.com', password='pw12345')
        Enrollment.objects.create(user=self.nmcle_bds_student, course=self.nmcle_bds)
        self.nlen_student = User.objects.create_user(username='nlen_student', email='nlen@example.com', password='pw12345')
        Enrollment.objects.create(user=self.nlen_student, course=self.nlen)

        # Zero Enrollment rows at all — distinct from nmcle_mbbs_student
        # (enrolled, just in an unrelated course) and matches the exact
        # production audit account shape (freshly registered, never enrolled).
        self.unenrolled_student = User.objects.create_user(
            username='unenrolled_student', email='unenrolled@example.com', password='pw12345',
        )

        self.exam = Test.objects.create(title='CEE-MBBS Mock Test', exam_type='mock', is_draft=False)
        self.exam.courses.set([self.cee_mbbs])

    def _visible_ids(self, user):
        self.client.force_authenticate(user=user)
        resp = self.client.get('/api/tests/?exam_type=mock')
        self.assertEqual(resp.status_code, 200)
        return {t['id'] for t in resp.data}

    def _start(self, user):
        self.client.force_authenticate(user=user)
        return self.client.post(f'/api/tests/{self.exam.id}/start/', {})

    def test_1_assigned_course_student_sees_and_can_start_the_exam(self):
        self.assertIn(self.exam.id, self._visible_ids(self.cee_mbbs_student))
        resp = self._start(self.cee_mbbs_student)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_2_unassigned_course_student_does_not_see_or_start_it(self):
        self.assertNotIn(self.exam.id, self._visible_ids(self.nmcle_mbbs_student))
        resp = self._start(self.nmcle_mbbs_student)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_3_another_unrelated_course_student_also_excluded(self):
        self.assertNotIn(self.exam.id, self._visible_ids(self.nmcle_bds_student))
        resp = self._start(self.nmcle_bds_student)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_4_adding_a_second_course_only_grants_that_course_not_others(self):
        """Spec's Test 4 (its literal 'CEE-MBBS -> NOT VISIBLE' line
        contradicts Test 1/the rest of the document, which requires
        CEE-MBBS to keep seeing an exam it's assigned to — treated here as
        a typo and implemented per the document's actual intent: adding a
        course is additive, never revokes an existing assignment)."""
        self.exam.courses.add(self.nmcle_mbbs)

        self.assertIn(self.exam.id, self._visible_ids(self.cee_mbbs_student))
        self.assertIn(self.exam.id, self._visible_ids(self.nmcle_mbbs_student))
        self.assertNotIn(self.exam.id, self._visible_ids(self.nmcle_bds_student))
        self.assertNotIn(self.exam.id, self._visible_ids(self.nlen_student))

    def test_5_direct_id_access_is_denied_regardless_of_query_params(self):
        """Copied-URL / API-tampering case — omitting ?course=, or passing a
        DIFFERENT course's id than the student's own, must never widen
        access. This is the fix for _start_attempt having no eligibility
        check at all before this change."""
        self.client.force_authenticate(user=self.nmcle_mbbs_student)

        no_param_resp = self.client.get('/api/tests/?exam_type=mock')
        self.assertNotIn(self.exam.id, {t['id'] for t in no_param_resp.data})

        tampered_resp = self.client.get(f'/api/tests/?exam_type=mock&course={self.cee_mbbs.id}')
        self.assertNotIn(self.exam.id, {t['id'] for t in tampered_resp.data})

        start_resp = self._start(self.nmcle_mbbs_student)
        self.assertEqual(start_resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_individually_assigned_student_gets_access_regardless_of_course(self):
        self.exam.assigned_students.add(self.nmcle_bds_student)
        resp = self._start(self.nmcle_bds_student)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_batch_assigned_student_gets_access(self):
        from courses.models import Batch, Enrollment

        batch = Batch.objects.create(course=self.nmcle_bds, name='2082 Batch')
        Enrollment.objects.filter(user=self.nmcle_bds_student, course=self.nmcle_bds).update(batch=batch)
        self.exam.assigned_batches.add(batch)

        resp = self._start(self.nmcle_bds_student)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_draft_exam_is_invisible_and_unstartable_even_when_course_assigned(self):
        self.exam.is_draft = True
        self.exam.save(update_fields=['is_draft'])

        self.assertNotIn(self.exam.id, self._visible_ids(self.cee_mbbs_student))
        resp = self._start(self.cee_mbbs_student)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_new_exam_defaults_to_draft(self):
        """Root-cause fix for spec item 7 — the model default alone must
        keep an exam invisible even if the creating code (e.g. the Admin
        create-exam form) never explicitly sets is_draft."""
        bare = Test.objects.create(title='Untouched default', exam_type='mock')
        self.assertTrue(bare.is_draft)

    def test_legacy_unscoped_published_exam_stays_visible_under_needs_course_review(self):
        """The migration escape hatch — an exam that predates this feature,
        already published with no course assignment, must not vanish."""
        legacy = Test.objects.create(title='Legacy Exam', exam_type='mock', is_draft=False, needs_course_review=True)

        self.assertIn(legacy.id, self._visible_ids(self.cee_mbbs_student))
        self.assertIn(legacy.id, self._visible_ids(self.nmcle_mbbs_student))

    def test_staff_sees_and_can_start_any_exam_including_drafts(self):
        self.exam.is_draft = True
        self.exam.save(update_fields=['is_draft'])
        self.assertIn(self.exam.id, self._visible_ids(self.staff))
        resp = self._start(self.staff)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_6_unenrolled_student_cannot_see_or_start_any_exam(self):
        """Zero Enrollment rows, not just an unrelated one — matches the
        exact production audit scenario (a freshly registered account)."""
        self.assertNotIn(self.exam.id, self._visible_ids(self.unenrolled_student))
        detail_resp = self.client.get(f'/api/tests/{self.exam.id}/')
        self.assertEqual(detail_resp.status_code, status.HTTP_404_NOT_FOUND)
        resp = self._start(self.unenrolled_student)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_7_needs_course_review_does_not_bypass_an_already_assigned_course(self):
        """The exact production bug, reproduced and pinned down so it can
        never come back: visible_test_queryset() used to OR
        needs_course_review=True unconditionally, so a test that already
        had real courses assigned (but was never explicitly cleared of the
        legacy flag — nothing in the Admin exam form even shows this field,
        see TestAdminSerializer.update()) stayed visible — title, subject,
        exam_type, question_count, courses_detail all leaked — to every
        student regardless of enrollment. Starting it was already correctly
        blocked (can_access_test never had this bug); only the listing/
        detail leak needed fixing."""
        stale = Test.objects.create(
            title='Daily Physics — Vectors & Scalars', exam_type='daily', is_draft=False, needs_course_review=True,
        )
        stale.courses.set([self.cee_mbbs])

        # Assigned course: sees and can start it, same as any normal exam.
        self.client.force_authenticate(user=self.cee_mbbs_student)
        list_resp = self.client.get('/api/tests/?exam_type=daily')
        self.assertIn(stale.id, {t['id'] for t in list_resp.data})
        self.assertEqual(self.client.get(f'/api/tests/{stale.id}/').status_code, status.HTTP_200_OK)

        # Unrelated course AND zero-enrollment: must not see it in the list,
        # must not retrieve it directly, must not be able to start it.
        for student in (self.nmcle_mbbs_student, self.unenrolled_student):
            self.client.force_authenticate(user=student)
            list_resp = self.client.get('/api/tests/?exam_type=daily')
            self.assertNotIn(stale.id, {t['id'] for t in list_resp.data})
            self.assertEqual(self.client.get(f'/api/tests/{stale.id}/').status_code, status.HTTP_404_NOT_FOUND)
            start_resp = self.client.post(f'/api/tests/{stale.id}/start/', {})
            self.assertEqual(start_resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_multi_course_student_sees_only_tests_from_their_own_courses(self):
        from courses.models import Enrollment

        multi_student = User.objects.create_user(username='multi_student', email='multi@example.com', password='pw12345')
        Enrollment.objects.create(user=multi_student, course=self.cee_mbbs)
        Enrollment.objects.create(user=multi_student, course=self.nmcle_bds)

        nmcle_mbbs_only_exam = Test.objects.create(title='NMCLE-MBBS Only Mock', exam_type='mock', is_draft=False)
        nmcle_mbbs_only_exam.courses.set([self.nmcle_mbbs])

        visible = self._visible_ids(multi_student)
        self.assertIn(self.exam.id, visible)  # cee_mbbs — one of their two enrolled courses
        self.assertNotIn(nmcle_mbbs_only_exam.id, visible)  # nmcle_mbbs — not enrolled in this one

    def test_admin_role_sees_and_can_start_every_exam_regardless_of_assignment(self):
        """A second, explicit check beyond test_staff_... — an 'admin'-role
        account (not just is_staff generically) retains full access after
        this fix, matching can_access_test's own staff bypass."""
        other_course_exam = Test.objects.create(title='NMCLE-BDS Only Mock', exam_type='mock', is_draft=False)
        other_course_exam.courses.set([self.nmcle_bds])

        visible = self._visible_ids(self.staff)
        self.assertIn(self.exam.id, visible)
        self.assertIn(other_course_exam.id, visible)
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(f'/api/tests/{other_course_exam.id}/start/', {})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_published_and_assigned_exam_is_visible_across_all_four_exam_types(self):
        """The exact matrix from the exam-visibility regression report: a
        published exam assigned to Course A must be visible to a Course A
        student, hidden from a Course B student and an unenrolled student,
        and never visible at all while still a draft — proven independently
        for mock/daily/grand/pyq, since all four share the identical
        visible_test_queryset() code path (only ?exam_type= differs)."""
        for exam_type in ('mock', 'daily', 'grand', 'pyq'):
            extra = {'academic_year': '2025-26', 'university': 'IOM'} if exam_type == 'pyq' else {}
            exam_a = Test.objects.create(title=f'{exam_type} A', exam_type=exam_type, is_draft=False, **extra)
            exam_a.courses.set([self.cee_mbbs])

            # Course A student: sees it in the type-filtered list and can retrieve it directly.
            self.client.force_authenticate(user=self.cee_mbbs_student)
            list_ids = {t['id'] for t in self.client.get(f'/api/tests/?exam_type={exam_type}').data}
            self.assertIn(exam_a.id, list_ids, f'{exam_type}: Course A student should see their own exam')
            self.assertEqual(
                self.client.get(f'/api/tests/{exam_a.id}/').status_code, status.HTTP_200_OK,
                f'{exam_type}: Course A student should retrieve their own exam directly',
            )

            # Course B student and a fully unenrolled student: neither sees or can start it.
            for other_student in (self.nmcle_mbbs_student, self.unenrolled_student):
                self.client.force_authenticate(user=other_student)
                other_list_ids = {t['id'] for t in self.client.get(f'/api/tests/?exam_type={exam_type}').data}
                self.assertNotIn(exam_a.id, other_list_ids, f'{exam_type}: unauthorized student must not see it in the list')
                self.assertEqual(
                    self.client.get(f'/api/tests/{exam_a.id}/').status_code, status.HTTP_404_NOT_FOUND,
                    f'{exam_type}: unauthorized student must not retrieve it directly',
                )
                start_resp = self.client.post(f'/api/tests/{exam_a.id}/start/', {})
                self.assertEqual(start_resp.status_code, status.HTTP_403_FORBIDDEN, f'{exam_type}: unauthorized start must be blocked')

            # Same course, but still a draft: invisible to everyone but staff,
            # regardless of exam_type — this is the exact "assigned but not
            # published" state the regression report's Demo/Demo Test exams
            # were actually in (not a course-scoping bug).
            exam_a_draft = Test.objects.create(title=f'{exam_type} A Draft', exam_type=exam_type, is_draft=True, **extra)
            exam_a_draft.courses.set([self.cee_mbbs])
            self.client.force_authenticate(user=self.cee_mbbs_student)
            draft_list_ids = {t['id'] for t in self.client.get(f'/api/tests/?exam_type={exam_type}').data}
            self.assertNotIn(exam_a_draft.id, draft_list_ids, f'{exam_type}: draft must stay invisible even to the assigned course')


class AuditExamCourseAssignmentCommandTests(APITestCase):
    def setUp(self):
        from courses.models import Course

        self.cee_mbbs = Course.objects.create(name='CEE-MBBS', prefix='CEEMBBS2')
        self.cee_bds = Course.objects.create(name='CEE-BDS', prefix='CEEBDS2')
        self.subject_single = Subject.objects.create(name='Biology (single-course)')
        self.subject_single.courses.set([self.cee_mbbs])
        self.subject_shared = Subject.objects.create(name='Shared Subject')
        self.subject_shared.courses.set([self.cee_mbbs, self.cee_bds])
        self.subject_none = Subject.objects.create(name='No-course Subject')

    def test_dry_run_makes_no_changes(self):
        from io import StringIO

        from django.core.management import call_command

        unambiguous = Test.objects.create(title='Unambiguous', exam_type='mock', is_draft=False, subject=self.subject_single)
        call_command('audit_exam_course_assignment', stdout=StringIO())

        unambiguous.refresh_from_db()
        self.assertEqual(unambiguous.courses.count(), 0)
        self.assertFalse(unambiguous.needs_course_review)

    def test_apply_maps_unambiguous_subject_and_flags_the_rest(self):
        from io import StringIO

        from django.core.management import call_command

        unambiguous = Test.objects.create(title='Unambiguous', exam_type='mock', is_draft=False, subject=self.subject_single)
        ambiguous_shared = Test.objects.create(title='Ambiguous shared', exam_type='mock', is_draft=False, subject=self.subject_shared)
        ambiguous_no_subject = Test.objects.create(title='No subject', exam_type='mock', is_draft=False)
        already_scoped = Test.objects.create(title='Already scoped', exam_type='mock', is_draft=False)
        already_scoped.courses.set([self.cee_mbbs])

        call_command('audit_exam_course_assignment', '--apply', stdout=StringIO())

        unambiguous.refresh_from_db()
        self.assertEqual(list(unambiguous.courses.values_list('id', flat=True)), [self.cee_mbbs.id])
        self.assertFalse(unambiguous.needs_course_review)

        ambiguous_shared.refresh_from_db()
        self.assertEqual(ambiguous_shared.courses.count(), 0)
        self.assertTrue(ambiguous_shared.needs_course_review)

        ambiguous_no_subject.refresh_from_db()
        self.assertTrue(ambiguous_no_subject.needs_course_review)

        already_scoped.refresh_from_db()
        self.assertFalse(already_scoped.needs_course_review)
        self.assertEqual(list(already_scoped.courses.values_list('id', flat=True)), [self.cee_mbbs.id])


class AdminExamCreateEditApiTests(APITestCase):
    """Regression coverage for the Admin exam-management Create/Edit form's
    actual API calls — TestAdminSerializer.create()/update() must pop EVERY
    M2M field (courses, assigned_students, assigned_batches) out of
    validated_data before touching the instance, since Django raises
    TypeError on both `Model(**kwargs)` and plain `setattr()` for M2M
    fields. A prior version of this serializer only popped `courses`,
    silently 500ing on every save once assigned_students/assigned_batches
    were added to Meta.fields but not to create()/update() — caught here by
    exercising the real endpoint, not just constructing Test objects
    directly via the ORM the way the access-control tests above do."""

    def setUp(self):
        from courses.models import Course

        self.staff = User.objects.create_user(
            username='examstaff', email='examstaff@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(username='examstudent', email='examstudent@example.com', password='pw12345')
        self.course = Course.objects.create(name='CEE-MBBS API Test', prefix='CEEAPITEST')
        self.client.force_authenticate(user=self.staff)

    def _payload(self, **overrides):
        payload = {
            'title': 'API Test Exam', 'exam_type': 'mock', 'courses': [self.course.id],
            'assigned_students': [self.student.id], 'assigned_batches': [], 'is_draft': False,
        }
        payload.update(overrides)
        return payload

    def test_create_with_courses_and_assigned_students_succeeds(self):
        resp = self.client.post('/api/tests/', self._payload(), format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

        test = Test.objects.get(pk=resp.data['id'])
        self.assertEqual(list(test.courses.values_list('id', flat=True)), [self.course.id])
        self.assertEqual(list(test.assigned_students.values_list('id', flat=True)), [self.student.id])

    def test_edit_updates_courses_and_assigned_students(self):
        test = Test.objects.create(title='To edit', exam_type='mock')

        resp = self.client.patch(f'/api/tests/{test.id}/', self._payload(), format='json')

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        test.refresh_from_db()
        self.assertEqual(list(test.courses.values_list('id', flat=True)), [self.course.id])
        self.assertEqual(list(test.assigned_students.values_list('id', flat=True)), [self.student.id])

    def test_edit_assigning_courses_auto_clears_stale_needs_course_review(self):
        """Root-cause regression for the production leak: the Admin exam
        form never surfaces needs_course_review, so an admin assigning real
        courses to a legacy-flagged test previously left the flag stuck at
        True forever — the exact state that made 3 real tests visible to
        every student regardless of enrollment. Assigning courses here must
        clear it automatically."""
        test = Test.objects.create(title='Legacy needing review', exam_type='mock', is_draft=False, needs_course_review=True)

        resp = self.client.patch(f'/api/tests/{test.id}/', self._payload(assigned_students=[]), format='json')

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        test.refresh_from_db()
        self.assertFalse(test.needs_course_review)

    def test_edit_explicit_needs_course_review_value_is_respected(self):
        """If a caller explicitly sends needs_course_review in the same
        request, that explicit value wins over the auto-clear."""
        test = Test.objects.create(title='Explicit review flag', exam_type='mock', is_draft=False, needs_course_review=True)

        resp = self.client.patch(
            f'/api/tests/{test.id}/', self._payload(assigned_students=[], needs_course_review=True), format='json',
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        test.refresh_from_db()
        self.assertTrue(test.needs_course_review)

    def test_edit_clears_assignment_when_set_to_empty(self):
        test = Test.objects.create(title='To clear', exam_type='mock')
        test.courses.set([self.course])
        test.assigned_students.set([self.student])

        resp = self.client.patch(f'/api/tests/{test.id}/', self._payload(courses=[], assigned_students=[], is_draft=True), format='json')

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        test.refresh_from_db()
        self.assertEqual(test.courses.count(), 0)
        self.assertEqual(test.assigned_students.count(), 0)

    def test_edit_without_touching_assignment_fields_leaves_them_unchanged(self):
        """A PATCH that omits courses/assigned_students/assigned_batches
        entirely (e.g. a partial update from some other future caller)
        must not wipe existing assignment — matches the `is not None` guard
        in TestAdminSerializer.update()."""
        test = Test.objects.create(title='Partial patch', exam_type='mock')
        test.courses.set([self.course])

        resp = self.client.patch(f'/api/tests/{test.id}/', {'title': 'Partial patch — renamed'}, format='json')

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        test.refresh_from_db()
        self.assertEqual(test.title, 'Partial patch — renamed')
        self.assertEqual(list(test.courses.values_list('id', flat=True)), [self.course.id])


class PerformanceCourseScopingTests(APITestCase):
    """The `tests_app/performance.py` catalog reads (subject_breakdown,
    activity_calendar's upcoming exams, recommendations, and
    SubjectPerformanceDetailView's subject_id path param) previously trusted
    an optional, client-supplied `course` and went fully unfiltered whenever
    it was omitted. These confirm a CEE-PG student's own performance
    dashboard/calendar/recommendations never disclose a CEE-UG-only
    subject, exam, or suggestion — including when `course` is tampered."""

    def setUp(self):
        from courses.models import Course, Enrollment

        self.cee_ug = Course.objects.create(name='CEE-UG Perf', prefix='CEEUGPERF')
        self.cee_pg = Course.objects.create(name='CEE-PG Perf', prefix='CEEPGPERF')

        self.pg_student = User.objects.create_user(username='perf_pg', email='perf_pg@example.com', password='pw12345')
        Enrollment.objects.create(user=self.pg_student, course=self.cee_pg)

        self.physics = Subject.objects.create(name='Physics Perf', is_free=True)
        self.physics.courses.set([self.cee_ug])
        self.physics_q = Question.objects.create(subject=self.physics, text='Physics Perf Q1')
        self.physics_q.courses.set([self.cee_ug])

        self.pathology = Subject.objects.create(name='Pathology Perf', is_free=True)
        self.pathology.courses.set([self.cee_pg])
        self.pathology_q = Question.objects.create(subject=self.pathology, text='Pathology Perf Q1')
        self.pathology_q.courses.set([self.cee_pg])

        self.client.force_authenticate(user=self.pg_student)

    def test_overview_subject_breakdown_excludes_other_course_subject(self):
        resp = self.client.get('/api/performance/overview/')
        names = {s['subject_name'] for s in resp.data['subjects']}
        self.assertIn('Pathology Perf', names)
        self.assertNotIn('Physics Perf', names)

    def test_overview_tampered_course_param_cannot_surface_other_course_subject(self):
        resp = self.client.get(f'/api/performance/overview/?course={self.cee_ug.id}')
        names = {s['subject_name'] for s in resp.data['subjects']}
        self.assertNotIn('Physics Perf', names)

    def test_subject_detail_denies_unassigned_subject_by_id(self):
        resp = self.client.get(f'/api/performance/subjects/{self.physics.id}/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_subject_detail_allows_assigned_subject_by_id(self):
        resp = self.client.get(f'/api/performance/subjects/{self.pathology.id}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_calendar_upcoming_exams_excludes_other_course_exam(self):
        ug_exam = Test.objects.create(
            title='UG Perf Exam', exam_type='mock', is_draft=False,
            scheduled_start=timezone.now() + timezone.timedelta(days=3),
        )
        ug_exam.courses.set([self.cee_ug])
        month = timezone.now().strftime('%Y-%m')

        resp = self.client.get(f'/api/performance/calendar/?month={month}')

        test_ids = {row['test_id'] for row in resp.data['upcoming_exams']}
        self.assertNotIn(ug_exam.id, test_ids)

    def test_calendar_upcoming_exams_excludes_draft_exam_even_if_course_assigned(self):
        draft_exam = Test.objects.create(
            title='PG Draft Perf Exam', exam_type='mock',
            scheduled_start=timezone.now() + timezone.timedelta(days=3),
        )
        draft_exam.courses.set([self.cee_pg])
        month = timezone.now().strftime('%Y-%m')

        resp = self.client.get(f'/api/performance/calendar/?month={month}')

        test_ids = {row['test_id'] for row in resp.data['upcoming_exams']}
        self.assertNotIn(draft_exam.id, test_ids)

    def test_calendar_upcoming_exams_includes_own_course_published_exam(self):
        pg_exam = Test.objects.create(
            title='PG Perf Exam', exam_type='mock', is_draft=False,
            scheduled_start=timezone.now() + timezone.timedelta(days=3),
        )
        pg_exam.courses.set([self.cee_pg])
        month = timezone.now().strftime('%Y-%m')

        resp = self.client.get(f'/api/performance/calendar/?month={month}')

        test_ids = {row['test_id'] for row in resp.data['upcoming_exams']}
        self.assertIn(pg_exam.id, test_ids)

    def test_recommendations_never_suggests_other_course_test_for_shared_subject(self):
        """A subject shared across both courses can have a Test scoped to
        only one of them — the suggested_test_id/suggested_video_id must
        never point at the other course's resource."""
        shared_subject = Subject.objects.create(name='Anatomy Perf Shared', is_free=True)
        shared_subject.courses.set([self.cee_ug, self.cee_pg])

        for i in range(3):
            shared_q = Question.objects.create(subject=shared_subject, text=f'Anatomy Perf Shared Q{i}')
            shared_q.courses.set([self.cee_ug, self.cee_pg])
            QuestionAttempt.objects.create(
                user=self.pg_student, question=shared_q, is_correct=False,
                attempts_count=1, correct_count=0,
            )

        ug_only_test = Test.objects.create(
            title='Anatomy UG-only QBank Perf', exam_type='qbank', is_draft=False, subject=shared_subject,
        )
        ug_only_test.courses.set([self.cee_ug])

        resp = self.client.get('/api/performance/overview/')

        revise = [s for s in resp.data['recommendations']['suggestions'] if s.get('subject_id') == shared_subject.id]
        self.assertTrue(revise)
        self.assertNotEqual(revise[0]['suggested_test_id'], ug_only_test.id)


class ExamManagementDashboardApiTests(APITestCase):
    """The Admin Exam Management rebuild's new server-side filters
    (?program=/?status=/?search=/?standalone=), the opt-in paginated
    `browse` actions on TestViewSet/ExamTemplateViewSet (GET /tests/ and
    GET /exam-templates/ themselves must stay bare-array, per the existing
    callers this session confirmed — Frontend SingleTestSection.js and
    Admin videos/page.js both do `.then(setTests)` on a plain array), and
    the new /tests/stats/ aggregate endpoint."""

    def setUp(self):
        from courses.models import Course

        self.staff = User.objects.create_user(
            username='examdash_staff', email='examdash_staff@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(
            username='examdash_student', email='examdash_student@example.com', password='pw12345',
        )
        self.mbbs_course = Course.objects.create(name='CEE-MBBS Dash', prefix='MBBSDASH', program_group='CEE-MBBS')
        self.pg_course = Course.objects.create(name='CEE-PG Dash', prefix='PGDASH', program_group='CEE-PG')

    def test_program_filter_scopes_by_course_program_group(self):
        mbbs_test = Test.objects.create(title='MBBS Mock Dash', exam_type='mock', is_draft=False)
        mbbs_test.courses.set([self.mbbs_course])
        pg_test = Test.objects.create(title='PG Mock Dash', exam_type='mock', is_draft=False)
        pg_test.courses.set([self.pg_course])
        self.client.force_authenticate(user=self.staff)

        resp = self.client.get('/api/tests/?program=CEE-MBBS')

        ids = {row['id'] for row in resp.data}
        self.assertIn(mbbs_test.id, ids)
        self.assertNotIn(pg_test.id, ids)

    def test_search_filter_matches_title_case_insensitively(self):
        Test.objects.create(title='Respiratory System Daily Test', exam_type='daily', is_draft=False)
        Test.objects.create(title='Cardiology Mock', exam_type='mock', is_draft=False)
        self.client.force_authenticate(user=self.staff)

        resp = self.client.get('/api/tests/?search=respiratory')

        titles = {row['title'] for row in resp.data}
        self.assertIn('Respiratory System Daily Test', titles)
        self.assertNotIn('Cardiology Mock', titles)

    def test_status_filter_draft_vs_published(self):
        draft = Test.objects.create(title='Draft Dash Exam', exam_type='mock', is_draft=True)
        published = Test.objects.create(title='Published Dash Exam', exam_type='mock', is_draft=False)
        self.client.force_authenticate(user=self.staff)

        draft_ids = {row['id'] for row in self.client.get('/api/tests/?status=draft').data}
        published_ids = {row['id'] for row in self.client.get('/api/tests/?status=published').data}

        self.assertIn(draft.id, draft_ids)
        self.assertNotIn(published.id, draft_ids)
        self.assertIn(published.id, published_ids)
        self.assertNotIn(draft.id, published_ids)

    def test_status_filter_scheduled_matches_tests_with_an_upcoming_session(self):
        template = ExamTemplate.objects.create(title='Scheduled Dash Exam', exam_type='mock', created_by=self.staff)
        scheduled_test = Test.objects.create(
            title='Scheduled Dash Exam v1', exam_type='mock', is_draft=False, exam_template=template,
        )
        ExamSession.objects.create(
            exam_template=template, exam_version=scheduled_test, session_name='Session 1',
            start_datetime=timezone.now() + timezone.timedelta(days=1),
            end_datetime=timezone.now() + timezone.timedelta(days=1, hours=2),
            status='scheduled', created_by=self.staff,
        )
        unscheduled_test = Test.objects.create(title='No Session Dash Exam', exam_type='mock', is_draft=False)
        self.client.force_authenticate(user=self.staff)

        resp = self.client.get('/api/tests/?status=scheduled')

        ids = {row['id'] for row in resp.data}
        self.assertIn(scheduled_test.id, ids)
        self.assertNotIn(unscheduled_test.id, ids)

    def test_standalone_filter_excludes_templated_exam_versions(self):
        template = ExamTemplate.objects.create(title='Templated Dash Exam', exam_type='mock', created_by=self.staff)
        templated_test = Test.objects.create(title='Templated Dash v1', exam_type='mock', exam_template=template)
        standalone_test = Test.objects.create(title='Standalone Dash Exam', exam_type='mock')
        self.client.force_authenticate(user=self.staff)

        resp = self.client.get('/api/tests/?standalone=true')

        ids = {row['id'] for row in resp.data}
        self.assertIn(standalone_test.id, ids)
        self.assertNotIn(templated_test.id, ids)

    def test_bare_list_endpoint_stays_unpaginated_for_existing_callers(self):
        """Frontend/src/components/plans/SingleTestSection.js and
        Admin/src/app/videos/page.js both call GET /tests/ and pass the
        response straight to setState — must stay a plain array."""
        Test.objects.create(title='Bare List Dash Exam', exam_type='mock', is_draft=False)
        self.client.force_authenticate(user=self.staff)

        resp = self.client.get('/api/tests/')

        self.assertIsInstance(resp.data, list)

    def test_tests_browse_action_returns_paginated_shape(self):
        for i in range(3):
            Test.objects.create(title=f'Browse Dash Exam {i}', exam_type='mock', is_draft=False)
        self.client.force_authenticate(user=self.staff)

        resp = self.client.get('/api/tests/browse/?page_size=2')

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for key in ('count', 'next', 'previous', 'results'):
            self.assertIn(key, resp.data)
        self.assertEqual(len(resp.data['results']), 2)
        self.assertGreaterEqual(resp.data['count'], 3)

    def test_tests_browse_action_requires_admin(self):
        self.client.force_authenticate(user=self.student)

        resp = self.client.get('/api/tests/browse/')

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_exam_templates_browse_action_paginates_and_filters_by_program(self):
        mbbs_template = ExamTemplate.objects.create(title='MBBS Browse Template', exam_type='mock', created_by=self.staff)
        mbbs_version = Test.objects.create(title='MBBS Browse Template v1', exam_type='mock', exam_template=mbbs_template)
        mbbs_version.courses.set([self.mbbs_course])
        pg_template = ExamTemplate.objects.create(title='PG Browse Template', exam_type='mock', created_by=self.staff)
        pg_version = Test.objects.create(title='PG Browse Template v1', exam_type='mock', exam_template=pg_template)
        pg_version.courses.set([self.pg_course])
        self.client.force_authenticate(user=self.staff)

        resp = self.client.get('/api/exam-templates/browse/?program=CEE-MBBS')

        self.assertIn('results', resp.data)
        ids = {row['id'] for row in resp.data['results']}
        self.assertIn(mbbs_template.id, ids)
        self.assertNotIn(pg_template.id, ids)

    def test_stats_endpoint_counts_are_real_not_hardcoded(self):
        draft_standalone = Test.objects.create(title='Stats Draft Standalone', exam_type='mock', is_draft=True)
        published_standalone = Test.objects.create(title='Stats Published Standalone', exam_type='mock', is_draft=False)

        template = ExamTemplate.objects.create(title='Stats Template', exam_type='mock', created_by=self.staff)
        template_version = Test.objects.create(
            title='Stats Template v1', exam_type='mock', is_draft=False, exam_template=template,
        )
        ExamSession.objects.create(
            exam_template=template, exam_version=template_version, session_name='Stats Session',
            start_datetime=timezone.now() + timezone.timedelta(days=1),
            end_datetime=timezone.now() + timezone.timedelta(days=1, hours=1),
            status='scheduled', created_by=self.staff,
        )

        question = Question.objects.create(subject=Subject.objects.create(name='Stats Dash Subject'), text='Stats Q1')
        TestAttempt.objects.create(user=self.student, test=published_standalone, status='submitted')

        self.client.force_authenticate(user=self.staff)
        resp = self.client.get('/api/tests/stats/')

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(resp.data['total_exams'], 3)  # 2 standalone + 1 template
        self.assertGreaterEqual(resp.data['draft_exams'], 1)
        self.assertGreaterEqual(resp.data['published_exams'], 2)  # published standalone + published template
        self.assertGreaterEqual(resp.data['scheduled_exams'], 1)
        self.assertGreaterEqual(resp.data['total_questions'], 1)
        self.assertGreaterEqual(resp.data['total_attempts'], 1)

    def test_stats_endpoint_scoped_by_program_excludes_other_program(self):
        mbbs_test = Test.objects.create(title='Stats MBBS Only', exam_type='mock', is_draft=False)
        mbbs_test.courses.set([self.mbbs_course])
        pg_test = Test.objects.create(title='Stats PG Only', exam_type='mock', is_draft=False)
        pg_test.courses.set([self.pg_course])
        self.client.force_authenticate(user=self.staff)

        resp = self.client.get('/api/tests/stats/?program=CEE-MBBS')

        # Exactly the MBBS-scoped standalone exam, not the PG one — proven by
        # comparing against an unscoped call that must count strictly more.
        unscoped = self.client.get('/api/tests/stats/').data
        self.assertLess(resp.data['total_exams'], unscoped['total_exams'])

    def test_access_filter_scopes_by_is_pro(self):
        pro_test = Test.objects.create(title='Pro Dash Exam', exam_type='mock', is_draft=False, is_pro=True)
        free_test = Test.objects.create(title='Free Dash Exam', exam_type='mock', is_draft=False, is_pro=False)
        self.client.force_authenticate(user=self.staff)

        pro_ids = {row['id'] for row in self.client.get('/api/tests/?access=pro').data}
        free_ids = {row['id'] for row in self.client.get('/api/tests/?access=free').data}

        self.assertIn(pro_test.id, pro_ids)
        self.assertNotIn(free_test.id, pro_ids)
        self.assertIn(free_test.id, free_ids)
        self.assertNotIn(pro_test.id, free_ids)

    def test_exam_templates_browse_status_filter_matches_latest_version_draft_state(self):
        published_template = ExamTemplate.objects.create(title='Published Template Dash', exam_type='mock', created_by=self.staff)
        Test.objects.create(title='Published Template Dash v1', exam_type='mock', is_draft=False, exam_template=published_template)
        draft_template = ExamTemplate.objects.create(title='Draft Template Dash', exam_type='mock', created_by=self.staff)
        Test.objects.create(title='Draft Template Dash v1', exam_type='mock', is_draft=True, exam_template=draft_template)
        self.client.force_authenticate(user=self.staff)

        published_resp = self.client.get('/api/exam-templates/browse/?status=published')
        draft_resp = self.client.get('/api/exam-templates/browse/?status=draft')

        published_ids = {row['id'] for row in published_resp.data['results']}
        draft_ids = {row['id'] for row in draft_resp.data['results']}
        self.assertIn(published_template.id, published_ids)
        self.assertNotIn(draft_template.id, published_ids)
        self.assertIn(draft_template.id, draft_ids)
        self.assertNotIn(published_template.id, draft_ids)

    def test_stats_by_program_returns_one_row_per_distinct_program(self):
        mbbs_test = Test.objects.create(title='Stats By Program MBBS', exam_type='mock', is_draft=False)
        mbbs_test.courses.set([self.mbbs_course])
        pg_test = Test.objects.create(title='Stats By Program PG', exam_type='mock', is_draft=False)
        pg_test.courses.set([self.pg_course])
        self.client.force_authenticate(user=self.staff)

        resp = self.client.get('/api/tests/stats_by_program/')

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        by_program = {row['program']: row for row in resp.data}
        self.assertIn('CEE-MBBS', by_program)
        self.assertIn('CEE-PG', by_program)
        self.assertGreaterEqual(by_program['CEE-MBBS']['total_exams'], 1)
        self.assertGreaterEqual(by_program['CEE-PG']['total_exams'], 1)

    def test_stats_endpoint_requires_admin(self):
        self.client.force_authenticate(user=self.student)

        resp = self.client.get('/api/tests/stats/')

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class SavedExamViewApiTests(APITestCase):
    """Per-admin saved filter combos — must never leak between admins."""

    def setUp(self):
        self.admin_a = User.objects.create_user(
            username='saved_view_admin_a', email='saved_view_admin_a@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.admin_b = User.objects.create_user(
            username='saved_view_admin_b', email='saved_view_admin_b@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(
            username='saved_view_student', email='saved_view_student@example.com', password='pw12345',
        )

    def test_create_and_list_own_saved_view(self):
        self.client.force_authenticate(user=self.admin_a)

        create_resp = self.client.post(
            '/api/saved-exam-views/', {'name': 'CEE-MBBS Exams', 'filters': {'program': 'CEE-MBBS'}}, format='json',
        )
        self.assertEqual(create_resp.status_code, status.HTTP_201_CREATED, create_resp.data)

        list_resp = self.client.get('/api/saved-exam-views/')
        names = {row['name'] for row in list_resp.data}
        self.assertIn('CEE-MBBS Exams', names)

    def test_saved_views_are_scoped_to_the_owning_admin(self):
        SavedExamView.objects.create(user=self.admin_a, name='Admin A View', filters={})
        SavedExamView.objects.create(user=self.admin_b, name='Admin B View', filters={})
        self.client.force_authenticate(user=self.admin_b)

        resp = self.client.get('/api/saved-exam-views/')

        names = {row['name'] for row in resp.data}
        self.assertIn('Admin B View', names)
        self.assertNotIn('Admin A View', names)

    def test_non_admin_cannot_access_saved_views(self):
        self.client.force_authenticate(user=self.student)

        resp = self.client.get('/api/saved-exam-views/')

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class DifficultyFilterTests(APITestCase):
    def test_difficulty_filter_scopes_tests_returned(self):
        easy = Test.objects.create(title='Easy Daily', exam_type='daily', is_draft=False, difficulty='easy')
        hard = Test.objects.create(title='Hard Daily', exam_type='daily', is_draft=False, difficulty='hard')
        staff = User.objects.create_user(username='diff_staff', email='diff_staff@example.com', password='pw12345', is_staff=True, admin_role='admin')
        self.client.force_authenticate(user=staff)

        resp = self.client.get('/api/tests/?difficulty=easy')

        ids = {t['id'] for t in resp.data}
        self.assertIn(easy.id, ids)
        self.assertNotIn(hard.id, ids)


class RecommendedTestEndpointTests(APITestCase):
    """Student exam-pages redesign: GET /tests/recommended/?exam_type=daily|mock|grand
    picks a real featured test per type — no editorial flag, no fabricated
    numbers."""

    def setUp(self):
        from courses.models import Course, Enrollment

        self.course = Course.objects.create(name='Recommend Course', prefix='RECRSE')
        self.student = User.objects.create_user(username='rec_student', email='rec_student@example.com', password='pw12345')
        Enrollment.objects.create(user=self.student, course=self.course)
        self.client.force_authenticate(user=self.student)

    def test_invalid_exam_type_is_rejected(self):
        resp = self.client.get('/api/tests/recommended/?exam_type=pyq')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_requires_auth(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get('/api/tests/recommended/?exam_type=daily')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_returns_null_when_no_tests_available(self):
        resp = self.client.get(f'/api/tests/recommended/?exam_type=daily&course={self.course.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data['test_id'])

    def test_daily_matches_weakest_subject_with_enough_attempts(self):
        from academics.services import record_question_result

        weak_subject = Subject.objects.create(name='Weak Subject Rec')
        weak_subject.courses.set([self.course])
        strong_subject = Subject.objects.create(name='Strong Subject Rec')
        strong_subject.courses.set([self.course])

        weak_daily = Test.objects.create(title='Weak Subject Daily', exam_type='daily', is_draft=False, subject=weak_subject)
        weak_daily.courses.set([self.course])
        strong_daily = Test.objects.create(title='Strong Subject Daily', exam_type='daily', is_draft=False, subject=strong_subject)
        strong_daily.courses.set([self.course])

        for i in range(4):
            q = Question.objects.create(subject=weak_subject, text=f'Weak Q{i}')
            record_question_result(self.student, q, i == 0, source='qbank')  # 1/4 correct
        for i in range(4):
            q = Question.objects.create(subject=strong_subject, text=f'Strong Q{i}')
            record_question_result(self.student, q, True, source='qbank')  # 4/4 correct

        resp = self.client.get(f'/api/tests/recommended/?exam_type=daily&course={self.course.id}')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['test_id'], weak_daily.id)
        self.assertEqual(resp.data['weak_area'], 'Weak Subject Rec')
        self.assertEqual(resp.data['reason'], 'weak_subject')

    def test_daily_falls_back_to_oldest_unattempted_test_with_no_performance_data(self):
        older = Test.objects.create(title='Older Daily Rec', exam_type='daily', is_draft=False)
        older.courses.set([self.course])
        newer = Test.objects.create(title='Newer Daily Rec', exam_type='daily', is_draft=False)
        newer.courses.set([self.course])

        resp = self.client.get(f'/api/tests/recommended/?exam_type=daily&course={self.course.id}')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['test_id'], older.id)
        self.assertEqual(resp.data['reason'], 'new')

    def test_mock_picks_the_test_with_the_most_questions(self):
        small = Test.objects.create(title='Small Mock Rec', exam_type='mock', is_draft=False)
        small.courses.set([self.course])
        big = Test.objects.create(title='Big Mock Rec', exam_type='mock', is_draft=False)
        big.courses.set([self.course])
        subject = Subject.objects.create(name='Mock Rec Subject')
        for i in range(3):
            q = Question.objects.create(subject=subject, text=f'Big Mock Q{i}')
            TestQuestion.objects.create(test=big, question=q, order=i)
        q = Question.objects.create(subject=subject, text='Small Mock Q0')
        TestQuestion.objects.create(test=small, question=q, order=0)

        resp = self.client.get(f'/api/tests/recommended/?exam_type=mock&course={self.course.id}')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['test_id'], big.id)
        self.assertEqual(resp.data['reason'], 'most_comprehensive')
        self.assertEqual(resp.data['question_count'], 3)

    def test_grand_picks_the_most_attempted_test(self):
        popular = Test.objects.create(title='Popular Grand Rec', exam_type='grand', is_draft=False)
        popular.courses.set([self.course])
        quiet = Test.objects.create(title='Quiet Grand Rec', exam_type='grand', is_draft=False)
        quiet.courses.set([self.course])
        other_student = User.objects.create_user(username='rec_other', email='rec_other@example.com', password='pw12345')
        TestAttempt.objects.create(user=self.student, test=popular, status='submitted')
        TestAttempt.objects.create(user=other_student, test=popular, status='submitted')
        TestAttempt.objects.create(user=self.student, test=quiet, status='submitted')

        resp = self.client.get(f'/api/tests/recommended/?exam_type=grand&course={self.course.id}')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['test_id'], popular.id)
        self.assertEqual(resp.data['attempted_count'], 2)


class UniversitiesEnrichmentTests(APITestCase):
    def test_universities_returns_years_available_and_paper_count(self):
        staff = User.objects.create_user(username='uni_staff', email='uni_staff@example.com', password='pw12345', is_staff=True, admin_role='admin')
        Test.objects.create(title='IOM 2020', exam_type='pyq', is_draft=False, university='IOM', academic_year='2020')
        Test.objects.create(title='IOM 2021', exam_type='pyq', is_draft=False, university='IOM', academic_year='2021')
        Test.objects.create(title='KU 2020', exam_type='pyq', is_draft=False, university='KU', academic_year='2020')
        self.client.force_authenticate(user=staff)

        resp = self.client.get('/api/tests/universities/')

        by_name = {row['name']: row for row in resp.data}
        self.assertEqual(by_name['IOM']['years_available'], 2)
        self.assertEqual(by_name['IOM']['paper_count'], 2)
        self.assertEqual(by_name['KU']['years_available'], 1)
        self.assertEqual(by_name['KU']['paper_count'], 1)
