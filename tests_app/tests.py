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
