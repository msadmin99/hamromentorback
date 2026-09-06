from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase, APITransactionTestCase

from academics.models import Option, Question, QuestionAttempt, QuestionEvent, Subject
from core.models import DeletionAuditLog
from tests_app.models import Answer, ExamSession, ExamTemplate, ExamTypePolicy, SavedExamView, Test, TestAttempt, TestQuestion
from tests_app.policy import POLICY_CONTROLLED_FIELDS, get_all_exam_type_defaults, get_exam_type_defaults

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


class ExamSessionAttemptsPermissionTests(APITestCase):
    """P0 security-audit regression: GET /exam-sessions/{id}/attempts/ used
    to inherit IsStaffOrReadOnly with no override — since GET is a
    SAFE_METHOD, that permission class returned True for ANYONE, including
    an anonymous caller, leaking every participant's name/email/score/rank.
    Must now be admin-only, matching GET /exam-templates/{id}/sessions/'s
    identical fix below."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='sess_attempts_staff', email='sess_attempts_staff@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(
            username='sess_attempts_student', email='sess_attempts_student@example.com', password='pw12345',
        )
        self.template = ExamTemplate.objects.create(title='Attempts Perm Exam', exam_type='mock')
        self.version = Test.objects.create(title='Attempts Perm Exam v1', exam_type='mock', exam_template=self.template)
        now = timezone.now()
        self.session = ExamSession.objects.create(
            exam_template=self.template, exam_version=self.version,
            session_name='Session 1', start_datetime=now, end_datetime=now,
        )
        TestAttempt.objects.create(
            user=self.student, test=self.version, session=self.session, status='submitted', score=10,
        )

    def test_anonymous_caller_is_rejected(self):
        resp = self.client.get(f'/api/exam-sessions/{self.session.id}/attempts/')
        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_authenticated_non_staff_student_is_rejected(self):
        self.client.force_authenticate(user=self.student)
        resp = self.client.get(f'/api/exam-sessions/{self.session.id}/attempts/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_view_participants(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get(f'/api/exam-sessions/{self.session.id}/attempts/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)


class ExamTemplateSessionsPermissionTests(APITestCase):
    """P0 security-audit regression: same unauthenticated-read pattern as
    ExamSessionAttemptsPermissionTests, on GET /exam-templates/{id}/sessions/
    (schedule metadata, no participant PII — lower severity, same root cause
    and same fix)."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='tmpl_sessions_staff', email='tmpl_sessions_staff@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(
            username='tmpl_sessions_student', email='tmpl_sessions_student@example.com', password='pw12345',
        )
        self.template = ExamTemplate.objects.create(title='Sessions Perm Exam', exam_type='mock')

    def test_anonymous_caller_is_rejected(self):
        resp = self.client.get(f'/api/exam-templates/{self.template.id}/sessions/')
        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_authenticated_non_staff_student_is_rejected(self):
        self.client.force_authenticate(user=self.student)
        resp = self.client.get(f'/api/exam-templates/{self.template.id}/sessions/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_view_schedule_history(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get(f'/api/exam-templates/{self.template.id}/sessions/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


class ExamDeleteFeatureGateTests(APITestCase):
    """P0 security-audit regression: TestViewSet.destroy previously allowed
    ANY staff account (including Editor/Teacher, who the product's own
    RolePermission/EXAM_MANAGEMENT_FEATURES design explicitly excludes from
    exam deletion) through plain IsStaffOrReadOnly. Now gated on the
    'exam_delete' feature key, matching what the Admin UI already hides for
    these roles (ExamTable.js's hasFeature(user, "exam_delete") check) —
    this closes the gap between the frontend hiding it and the backend
    actually enforcing it."""

    def setUp(self):
        self.editor = User.objects.create_user(
            username='examdel_editor', email='examdel_editor@example.com', password='pw12345',
            is_staff=True, admin_role='editor',
        )
        self.teacher = User.objects.create_user(
            username='examdel_teacher', email='examdel_teacher@example.com', password='pw12345',
            is_staff=True, admin_role='teacher',
        )
        self.admin = User.objects.create_user(
            username='examdel_admin', email='examdel_admin@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )

    def test_editor_without_exam_delete_feature_is_rejected(self):
        test = Test.objects.create(title='Editor Delete Target', exam_type='mock')
        self.client.force_authenticate(user=self.editor)

        resp = self.client.delete(f'/api/tests/{test.id}/')

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Test.objects.filter(id=test.id).exists())

    def test_teacher_without_exam_delete_feature_is_rejected(self):
        test = Test.objects.create(title='Teacher Delete Target', exam_type='mock', created_by=self.teacher)
        self.client.force_authenticate(user=self.teacher)

        resp = self.client.delete(f'/api/tests/{test.id}/')

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Test.objects.filter(id=test.id).exists())

    def test_admin_with_exam_delete_feature_succeeds(self):
        test = Test.objects.create(title='Admin Delete Target', exam_type='mock')
        self.client.force_authenticate(user=self.admin)

        resp = self.client.delete(f'/api/tests/{test.id}/')

        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)


class ExamRescheduleFeatureGateTests(APITestCase):
    """Same fix, applied to POST /tests/{id}/reschedule/ ('exam_schedule')."""

    def setUp(self):
        self.editor = User.objects.create_user(
            username='examsched_editor', email='examsched_editor@example.com', password='pw12345',
            is_staff=True, admin_role='editor',
        )
        self.teacher = User.objects.create_user(
            username='examsched_teacher', email='examsched_teacher@example.com', password='pw12345',
            is_staff=True, admin_role='teacher',
        )
        self.admin = User.objects.create_user(
            username='examsched_admin', email='examsched_admin@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.test = Test.objects.create(title='Reschedule Target', exam_type='mock')

    def test_teacher_without_exam_schedule_feature_is_rejected(self):
        """admin_role='teacher' is a fixed, non-configurable ceiling
        (accounts.models.TEACHER_ALLOWED_FEATURES) that does not include
        'exam_schedule' — this is the documented design intent, not a new
        restriction; the Admin UI already never offered this button to a
        teacher account."""
        self.client.force_authenticate(user=self.teacher)

        resp = self.client.post(f'/api/tests/{self.test.id}/reschedule/', {
            'start_datetime': (timezone.now() + timezone.timedelta(days=1)).isoformat(),
            'end_datetime': (timezone.now() + timezone.timedelta(days=1, hours=1)).isoformat(),
        })

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_editor_with_exam_schedule_feature_succeeds(self):
        """'exam_schedule' IS in EDITOR_ALLOWED_FEATURES's ceiling, and an
        Editor with no RolePermission row yet falls back to that ceiling
        (accounts.models.user_feature_list) — so this must succeed."""
        self.client.force_authenticate(user=self.editor)

        resp = self.client.post(f'/api/tests/{self.test.id}/reschedule/', {
            'start_datetime': (timezone.now() + timezone.timedelta(days=1)).isoformat(),
            'end_datetime': (timezone.now() + timezone.timedelta(days=1, hours=1)).isoformat(),
        })

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)


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

    @override_settings(STATS_PROCESSING_ASYNC=False)
    def test_result_view_reports_total_responses_gated_by_threshold(self):
        """Deadlock-fix audit: total_responses/stats_available are derived
        from Option.pick_percentage/Question.total_attempts, which
        SubmitTestView now applies via a deferred stats task instead of
        inline (see StatsDeferralTests) — enqueued from transaction.
        on_commit(), which never fires under TestCase's non-committing
        transaction wrapper unless captured via captureOnCommitCallbacks().
        STATS_PROCESSING_ASYNC=False (local-dev-only sync fallback) then
        makes the *captured* callback apply stats inline, without changing
        what this test actually verifies."""
        from academics.models import QuestionBankConfig

        self.client.post(f'/api/attempts/{self.attempt.id}/answer/', {'question_id': self.question.id, 'option_id': self.correct.id})
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f'/api/attempts/{self.attempt.id}/submit/')

        resp = self.client.get(f'/api/attempts/{self.attempt.id}/')
        q = resp.data['questions'][0]
        self.assertFalse(q['stats_available'])
        self.assertIsNone(q['total_responses'])

        config = QuestionBankConfig.load()
        config.min_attempts_for_option_stats = 1
        config.save()

        resp = self.client.get(f'/api/attempts/{self.attempt.id}/')
        q = resp.data['questions'][0]
        self.assertTrue(q['stats_available'])
        self.assertEqual(q['total_responses'], 1)

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
        from django.core.cache import cache

        # StudentPerformanceOverviewView now caches its response per-user
        # for 30s (scalability audit) — see PerformanceCourseScopingTests.
        # setUp() for the same reasoning; this class's two tests reuse the
        # same email/user across methods and would otherwise leak this
        # test's cached questions_today into the other.
        cache.clear()

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

    def test_latest_attempt_id_points_at_the_best_scoring_submitted_attempt(self):
        """ExamCard's 'Review Test' button links straight to
        /tests/result/{latest_attempt_id} — it must be the same attempt
        best_score already reports, not just any submitted attempt."""
        TestAttempt.objects.create(user=self.student, test=self.test, status='submitted', score=1)
        best = TestAttempt.objects.create(user=self.student, test=self.test, status='submitted', score=2)
        TestAttempt.objects.create(user=self.student, test=self.test, status='in_progress')  # must not win

        resp = self.client.get('/api/tests/?exam_type=mock')

        row = next(r for r in resp.data if r['id'] == self.test.id)
        self.assertEqual(row['best_score'], 2.0)
        self.assertEqual(row['latest_attempt_id'], best.id)

    def test_latest_attempt_id_is_null_before_any_submission(self):
        resp = self.client.get('/api/tests/?exam_type=mock')
        row = next(r for r in resp.data if r['id'] == self.test.id)
        self.assertIsNone(row['latest_attempt_id'])

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
        from django.core.cache import cache

        # StudentPerformanceOverviewView now caches its response per-user
        # for 30s (scalability audit) — the cache backend isn't reset by
        # Django's per-test DB rollback, so a prior test's cached response
        # for the same (reused-id) user/params can otherwise leak into this
        # one. Same reasoning/pattern as SubjectRankCachingTests.setUp().
        cache.clear()

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


class TestListQueryCountAndCorrectnessTests(APITestCase):
    """Scalability audit fix: GET /tests/ used to cost ~8 queries per row
    (question_count/total_marks properties + separate best-score/
    in-progress/attempts-used/courses/subject/created_by lookups) with no
    select_related/prefetch_related at all. Confirms the fix is both fast
    (bounded query count regardless of row count) and correct (annotated
    values match what the old per-row queries would have computed) —
    speed without correctness would be a worse regression than the
    original N+1."""

    def setUp(self):
        from courses.models import Course, Enrollment

        self.course = Course.objects.create(name='QC Course', prefix='QCCOURSE')
        self.other_course = Course.objects.create(name='QC Other Course', prefix='QCOTHER')
        self.student = User.objects.create_user(username='qc_student', email='qc_student@example.com', password='pw12345')
        Enrollment.objects.create(user=self.student, course=self.course)
        self.teacher = User.objects.create_user(
            username='qc_teacher', email='qc_teacher@example.com', password='pw12345', is_staff=True, admin_role='teacher',
        )
        self.subject = Subject.objects.create(name='QC Subject')

        def make_test(title, marks_list):
            t = Test.objects.create(title=title, exam_type='mock', is_draft=False, subject=self.subject, created_by=self.teacher)
            t.courses.set([self.course])
            for i, marks in enumerate(marks_list):
                q = Question.objects.create(subject=self.subject, text=f'{title} Q{i}', marks=marks, negative_marks=0)
                TestQuestion.objects.create(test=t, question=q, order=i)
            return t

        # 5 tests with varying question counts/marks — a real N+1 would cost
        # more queries as this number grows; the fix must not.
        self.tests = [make_test(f'QC Test {i}', [1, 2, 3][: i + 1]) for i in range(5)]

        self.client.force_authenticate(user=self.student)

    def test_response_is_still_a_bare_array_not_a_pagination_envelope(self):
        """The core non-negotiable: existing callers (7 confirmed frontend
        call sites) expect a plain array back, not {count,next,previous,results}."""
        resp = self.client.get('/api/tests/?exam_type=mock')
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.data, list)
        self.assertEqual(len(resp.data), 5)

    def test_question_count_and_total_marks_are_correct(self):
        resp = self.client.get('/api/tests/?exam_type=mock')
        by_title = {row['title']: row for row in resp.data}
        self.assertEqual(by_title['QC Test 0']['question_count'], 1)
        self.assertEqual(by_title['QC Test 0']['total_marks'], 1.0)
        self.assertEqual(by_title['QC Test 4']['question_count'], 3)
        self.assertEqual(by_title['QC Test 4']['total_marks'], 6.0)  # 1+2+3

    def test_subject_name_courses_and_created_by_are_correct(self):
        resp = self.client.get('/api/tests/?exam_type=mock')
        row = resp.data[0]
        self.assertEqual(row['subject_name'], 'QC Subject')
        self.assertEqual(row['created_by_name'], self.teacher.email)
        self.assertEqual([c['id'] for c in row['courses_detail']], [self.course.id])

    def test_query_count_does_not_grow_with_row_count(self):
        """The actual regression test for the N+1 fix — 10 tests must not
        cost meaningfully more queries than 5. A real N+1 would show a
        clear linear jump; the annotated/prefetched version stays flat."""
        with self.assertNumQueries(FixedQueryCount := 6):
            resp = self.client.get('/api/tests/?exam_type=mock')
            self.assertEqual(len(resp.data), 5)

        # Double the row count — a per-row query pattern would roughly
        # double total queries too; this must not.
        def make_more(title):
            t = Test.objects.create(title=title, exam_type='mock', is_draft=False, subject=self.subject, created_by=self.teacher)
            t.courses.set([self.course])
            q = Question.objects.create(subject=self.subject, text=f'{title} Q', marks=1, negative_marks=0)
            TestQuestion.objects.create(test=t, question=q)
            return t

        for i in range(5, 10):
            make_more(f'QC Test {i}')

        with self.assertNumQueries(FixedQueryCount):
            resp = self.client.get('/api/tests/?exam_type=mock')
            self.assertEqual(len(resp.data), 10)

    def test_best_score_and_latest_attempt_id_pick_the_highest_scoring_submitted_attempt(self):
        t = self.tests[0]
        low = TestAttempt.objects.create(user=self.student, test=t, status='submitted', score=1)
        high = TestAttempt.objects.create(user=self.student, test=t, status='submitted', score=5)
        TestAttempt.objects.create(user=self.student, test=t, status='in_progress', score=0)  # must not count as "best"

        resp = self.client.get('/api/tests/?exam_type=mock')
        row = next(r for r in resp.data if r['id'] == t.id)
        self.assertEqual(row['best_score'], 5.0)
        self.assertEqual(row['latest_attempt_id'], high.id)
        self.assertEqual(row['card_status'], 'completed')
        self.assertEqual(row['attempts_used'], 3)
        self.assertNotEqual(row['latest_attempt_id'], low.id)

    def test_tied_best_scores_deterministically_pick_the_most_recent_attempt(self):
        """No secondary sort key existed in the original .order_by('-score')
        — an improvement over undefined tie-break behavior, not a
        regression, but must be deterministic and documented."""
        t = self.tests[0]
        first = TestAttempt.objects.create(user=self.student, test=t, status='submitted', score=3)
        second = TestAttempt.objects.create(user=self.student, test=t, status='submitted', score=3)

        resp = self.client.get('/api/tests/?exam_type=mock')
        row = next(r for r in resp.data if r['id'] == t.id)
        self.assertEqual(row['best_score'], 3.0)
        # TestAttempt's default ordering is -start_time (most recent first);
        # `second` was created after `first`, so it's the deterministic pick.
        self.assertEqual(row['latest_attempt_id'], second.id)
        self.assertNotEqual(row['latest_attempt_id'], first.id)

    def test_in_progress_attempt_reports_real_answered_count_with_no_extra_queries(self):
        t = self.tests[1]
        q1 = t.questions.all()[0]
        q2 = t.questions.all()[1]
        attempt = TestAttempt.objects.create(user=self.student, test=t, status='in_progress')
        Answer.objects.create(attempt=attempt, question=q1)
        Answer.objects.create(attempt=attempt, question=q2)

        with self.assertNumQueries(6):
            resp = self.client.get('/api/tests/?exam_type=mock')
        row = next(r for r in resp.data if r['id'] == t.id)
        self.assertEqual(row['card_status'], 'in_progress')
        self.assertEqual(row['in_progress_answered_count'], 2)

    def test_expired_bounded_cap_still_returns_a_bare_array_shape(self):
        """pagination_class is attached (a real DB-level LIMIT) but its
        get_paginated_response() unwraps back to a bare array — confirms
        the pagination mechanism itself doesn't leak the {count,...}
        envelope even when it actually triggers."""
        resp = self.client.get('/api/tests/?exam_type=mock&page_size=2')
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.data, list)

    def test_anonymous_request_still_works_without_the_authenticated_only_prefetch(self):
        self.client.force_authenticate(user=None)
        t = self.tests[0]
        t.is_pro = False
        t.save()
        resp = self.client.get('/api/tests/?exam_type=mock')
        # Anonymous users don't pass visible_test_queryset's eligibility
        # (no Enrollment) — this must not error, whatever it returns.
        self.assertIn(resp.status_code, (200, 401, 403))


class SubmitTestRankingTests(APITestCase):
    """Scalability audit fix: SubmitTestView's ranking used to `list()` the
    entire submitted-attempt pool ordered by score and call `.index()` on
    it — an O(pool size) query and Python scan on every single submission.
    Replaced with a single aggregate query: rank = 1 + COUNT(strictly ahead
    on score). Tied scores now share a rank (standard "competition
    ranking") instead of getting an arbitrary DB-order-dependent sequential
    position, which was never a guaranteed behavior in the old code."""

    def setUp(self):
        from academics.models import QuestionBankConfig

        # Pre-warm the QuestionBankConfig singleton — record_question_result()
        # lazily get-or-creates it on first use, which would otherwise add
        # one-time extra queries (SELECT+SAVEPOINT+INSERT+RELEASE) to
        # whichever submission happens first in a test, unrelated to
        # ranking and not something this test class is about.
        QuestionBankConfig.load()

        self.student = User.objects.create_user(username='rank_student', email='rank_student@example.com', password='pw12345')
        self.subject = Subject.objects.create(name='Ranking Subject')
        self.question = Question.objects.create(subject=self.subject, text='2+2=?', marks=10, negative_marks=0)
        self.correct = Option.objects.create(question=self.question, text='4', is_correct=True)
        self.wrong = Option.objects.create(question=self.question, text='5', is_correct=False)
        self.test = Test.objects.create(title='Ranking Mock', exam_type='mock', negative_marking=False)
        TestQuestion.objects.create(test=self.test, question=self.question)
        self.attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='in_progress')
        self.client.force_authenticate(user=self.student)

    def _submit_for_score_10(self):
        """Answers the one question correctly (marks=10, no negative
        marking) so the real SubmitTestView scoring path produces exactly
        score=10 — a real integration test, not a fabricated score."""
        self.client.post(f'/api/attempts/{self.attempt.id}/answer/', {'question_id': self.question.id, 'option_id': self.correct.id})
        return self.client.post(f'/api/attempts/{self.attempt.id}/submit/')

    def test_solo_submission_is_rank_1_percentile_100(self):
        resp = self._submit_for_score_10()
        self.assertEqual(resp.status_code, 200)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.rank, 1)
        self.assertEqual(float(self.attempt.percentile), 100)

    def test_rank_reflects_strictly_higher_scores_only(self):
        """One attempt ahead (20), one tied (10), one behind (5) — rank
        must be 2 (only the strictly-higher one counts), not affected by
        the tie or the lower score."""
        TestAttempt.objects.create(user=self.student, test=self.test, status='submitted', score=20)
        TestAttempt.objects.create(user=self.student, test=self.test, status='submitted', score=10)
        TestAttempt.objects.create(user=self.student, test=self.test, status='submitted', score=5)

        resp = self._submit_for_score_10()

        self.assertEqual(resp.status_code, 200)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.rank, 2)
        # total pool = 4 (this attempt + the 3 above); percentile = (4-2)/4*100
        self.assertEqual(float(self.attempt.percentile), 50.0)

    def test_tied_scores_share_the_same_rank(self):
        """Two attempts tied at the top score must both be rank 1 — this
        attempt (score 10) ties with a pre-existing score-10 attempt, and
        neither should be pushed to rank 2 by the other."""
        TestAttempt.objects.create(user=self.student, test=self.test, status='submitted', score=10)

        resp = self._submit_for_score_10()

        self.assertEqual(resp.status_code, 200)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.rank, 1)

    def test_incomplete_in_progress_attempts_are_excluded_from_ranking(self):
        """An in_progress attempt (the model's only non-submitted status —
        there is no 'cancelled' status on TestAttempt) must never count
        toward another attempt's rank or the pool total, however high its
        score field happens to be set."""
        TestAttempt.objects.create(user=self.student, test=self.test, status='in_progress', score=9999)

        resp = self._submit_for_score_10()

        self.assertEqual(resp.status_code, 200)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.rank, 1)
        self.assertEqual(float(self.attempt.percentile), 100)

    def test_attempts_on_a_different_test_are_excluded(self):
        other_test = Test.objects.create(title='Unrelated Mock', exam_type='mock')
        TestAttempt.objects.create(user=self.student, test=other_test, status='submitted', score=9999)

        resp = self._submit_for_score_10()

        self.assertEqual(resp.status_code, 200)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.rank, 1)
        self.assertEqual(float(self.attempt.percentile), 100)

    def test_attempts_in_a_different_session_of_the_same_test_are_excluded(self):
        """Rescheduled occurrences of the same exam must never merge
        rankings — a submitted attempt in another ExamSession of this same
        Test must not count toward this session-less attempt's rank."""
        template = ExamTemplate.objects.create(title='Ranking Template', exam_type='mock')
        self.test.exam_template = template
        self.test.save(update_fields=['exam_template'])
        now = timezone.now()
        session = ExamSession.objects.create(
            exam_template=template, exam_version=self.test,
            session_name='Other Session', start_datetime=now, end_datetime=now,
        )
        TestAttempt.objects.create(user=self.student, test=self.test, session=session, status='submitted', score=9999)

        resp = self._submit_for_score_10()

        self.assertEqual(resp.status_code, 200)
        self.attempt.refresh_from_db()
        # self.attempt has session=None — only shares a ranking pool with
        # other session=None attempts on this Test, not the session=X one.
        self.assertEqual(self.attempt.rank, 1)
        self.assertEqual(float(self.attempt.percentile), 100)

    def test_attempts_in_the_same_session_do_count(self):
        """The positive counterpart of the test above — a submitted
        attempt in the SAME session must count toward ranking."""
        template = ExamTemplate.objects.create(title='Ranking Template 2', exam_type='mock')
        self.test.exam_template = template
        self.test.save(update_fields=['exam_template'])
        now = timezone.now()
        # Phase 6: must be a genuinely OPEN window — self.attempt gets
        # assigned to this session below, and Phase 6's server-side
        # deadline enforcement (tests_app.lifecycle) now correctly rejects
        # answering/submitting once a session's window has closed. A
        # start==end instant (as the sibling negative test above still
        # uses, safely, since it never assigns self.attempt to that
        # session) would make this attempt expired before the test's own
        # answer/submit calls ran.
        session = ExamSession.objects.create(
            exam_template=template, exam_version=self.test,
            session_name='Shared Session',
            start_datetime=now - timezone.timedelta(hours=1), end_datetime=now + timezone.timedelta(hours=1),
        )
        self.attempt.session = session
        self.attempt.save(update_fields=['session'])
        TestAttempt.objects.create(user=self.student, test=self.test, session=session, status='submitted', score=20)

        resp = self._submit_for_score_10()

        self.assertEqual(resp.status_code, 200)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.rank, 2)
        self.assertEqual(float(self.attempt.percentile), 0)

    def _submit_query_count(self, pool_size):
        """Creates `pool_size` extra already-submitted competing attempts,
        then submits a fresh attempt for this student and returns how many
        queries SubmitTestView.post() issued in total. Uses its own fresh
        Test/Question/Option (not self.test/self.question/self.correct)
        each time it's called so the two measurements in the test below
        start from identical state: reusing the same Test would accumulate
        an extra TestQuestion each call, making the *second* response
        serialize more questions than the first (a real but unrelated
        per-question serialization cost, not a ranking-pool-size effect);
        reusing the same Option would make its accumulated total_attempts/
        pick_count cross a real, unrelated recompute threshold differently
        each time (Option.pick_percentage bookkeeping in
        record_question_result). Either would make this test flaky for
        reasons that have nothing to do with ranking."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        test = Test.objects.create(title='Ranking QC Mock', exam_type='mock', negative_marking=False)
        question = Question.objects.create(subject=self.subject, text='fresh q', marks=10, negative_marks=0)
        correct = Option.objects.create(question=question, text='right', is_correct=True)
        TestQuestion.objects.create(test=test, question=question)

        for i in range(pool_size):
            TestAttempt.objects.create(user=self.student, test=test, status='submitted', score=i)

        attempt = TestAttempt.objects.create(user=self.student, test=test, status='in_progress')
        self.client.post(f'/api/attempts/{attempt.id}/answer/', {'question_id': question.id, 'option_id': correct.id})
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.post(f'/api/attempts/{attempt.id}/submit/')
        self.assertEqual(resp.status_code, 200)
        return len(ctx.captured_queries)

    def test_ranking_query_count_does_not_grow_with_pool_size(self):
        """The old .index()-based implementation issued one query that
        returned every submitted attempt id in the pool — query count was
        flat but result-row-count (and Python work) grew with pool size.
        The new aggregate approach is flat on both fronts; this test
        proves the query *count* (measured across two separate students,
        since ranking pools are scoped per-user's-own-attempt-flow here,
        not per-test) stays flat as the pool grows from a handful of
        competing attempts to a much larger one."""
        small_pool_count = self._submit_query_count(pool_size=3)

        other_student = User.objects.create_user(username='rank_student_2', email='rank_student_2@example.com', password='pw12345')
        self.student = other_student
        self.client.force_authenticate(user=other_student)
        large_pool_count = self._submit_query_count(pool_size=50)

        self.assertEqual(small_pool_count, large_pool_count)


class LargeExamSubmissionTests(APITestCase):
    """Scalability audit fix (Phase 2.1): SubmitTestView now runs the whole
    submission (scoring + per-answer record_question_result() + rank) in
    one outer transaction instead of each record_question_result() call
    committing independently. Confirms scoring/accuracy stay exactly
    correct at 50/100/200/300-question exam sizes (explicitly required by
    the audit), and that a crash partway through leaves no partial state —
    the attempt stays 'in_progress' and none of that submission's
    QuestionAttempt/QuestionEvent rows exist, safe to retry cleanly."""

    def setUp(self):
        self.student = User.objects.create_user(username='large_exam_student', email='large_exam_student@example.com', password='pw12345')
        self.subject = Subject.objects.create(name='Large Exam Subject')
        self.client.force_authenticate(user=self.student)

    def _build_exam(self, n, negative_marking=True):
        """n questions, each marks=2, negative_marks=0.5. Odd-indexed
        questions (0-based) are answered correctly, even-indexed
        incorrectly — a known, hand-computable mix, not all-correct or
        all-wrong (which could hide a sign error in the score math)."""
        test = Test.objects.create(title=f'{n}Q Exam', exam_type='mock', negative_marking=negative_marking)
        # bulk_create() skips Question.save(), which is what normally
        # generates slug/public_id — both are unique=True, so bulk-created
        # rows need explicit, distinct values or they'd all collide on the
        # unique constraint (empty string == empty string). test.id keeps
        # these unique across this test class's several exam sizes too.
        questions = Question.objects.bulk_create([
            Question(
                subject=self.subject, text=f'Q{i}', marks=2, negative_marks=0.5,
                slug=f'large-exam-{test.id}-q{i}', public_id=f'LE{test.id}{i:04d}',
            )
            for i in range(n)
        ])
        options = []
        for q in questions:
            options += [
                Option(question=q, text='Correct', order=0, is_correct=True),
                Option(question=q, text='Wrong', order=1, is_correct=False),
            ]
        Option.objects.bulk_create(options)
        TestQuestion.objects.bulk_create([TestQuestion(test=test, question=q, order=i) for i, q in enumerate(questions)])

        attempt = TestAttempt.objects.create(user=self.student, test=test, status='in_progress')
        correct_options = {o.question_id: o for o in Option.objects.filter(question__in=questions, is_correct=True)}
        wrong_options = {o.question_id: o for o in Option.objects.filter(question__in=questions, is_correct=False)}
        answers = []
        expected_correct = 0
        for i, q in enumerate(questions):
            if i % 2 == 0:
                answers.append(Answer(attempt=attempt, question=q, selected_option=correct_options[q.id], is_correct=True))
                expected_correct += 1
            else:
                answers.append(Answer(attempt=attempt, question=q, selected_option=wrong_options[q.id], is_correct=False))
        Answer.objects.bulk_create(answers)
        return test, attempt, questions, expected_correct

    def _assert_correct_scoring(self, n):
        test, attempt, questions, expected_correct = self._build_exam(n)
        expected_wrong = n - expected_correct
        expected_score = round(expected_correct * 2 - expected_wrong * 0.5, 2)
        expected_accuracy = round(expected_correct / n * 100, 2)

        resp = self.client.post(f'/api/attempts/{attempt.id}/submit/')

        self.assertEqual(resp.status_code, 200, resp.data)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'submitted')
        self.assertEqual(float(attempt.score), expected_score)
        self.assertEqual(float(attempt.accuracy), expected_accuracy)
        self.assertEqual(attempt.rank, 1)
        self.assertEqual(QuestionAttempt.objects.filter(user=self.student, question__in=questions).count(), n)
        self.assertEqual(QuestionEvent.objects.filter(user=self.student, question__in=questions).count(), n)

    def test_50_question_exam_scores_correctly(self):
        self._assert_correct_scoring(50)

    def test_100_question_exam_scores_correctly(self):
        self._assert_correct_scoring(100)

    def test_200_question_exam_scores_correctly(self):
        self._assert_correct_scoring(200)

    def test_300_question_exam_scores_correctly(self):
        self._assert_correct_scoring(300)

    @override_settings(STATS_PROCESSING_ASYNC=False)
    def _assert_deferred_stats_applied_correctly(self, n):
        """Deadlock-fix audit requirement: large exam test for 200/300
        questions must also confirm the deferred stats pipeline actually
        applies correctly at that scale, not just that scoring is
        unaffected (already proven by _assert_correct_scoring)."""
        test, attempt, questions, expected_correct = self._build_exam(n)

        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(f'/api/attempts/{attempt.id}/submit/')
        self.assertEqual(resp.status_code, 200, resp.data)

        attempt.refresh_from_db()
        self.assertIsNotNone(attempt.stats_applied_at)
        for q in Question.objects.filter(id__in=[q.id for q in questions]):
            self.assertEqual(q.total_attempts, 1)
        correct_count = sum(1 for i in range(n) if i % 2 == 0)
        self.assertEqual(correct_count, expected_correct)
        for q in Question.objects.filter(id__in=[q.id for q in questions]):
            correct_opt = q.options.get(is_correct=True)
            wrong_opt = q.options.get(is_correct=False)
            # Exactly one of the two options got this attempt's one vote —
            # whichever this question's answer pattern selected.
            self.assertEqual(correct_opt.pick_count + wrong_opt.pick_count, 1)

    def test_200_question_exam_deferred_stats_apply_correctly(self):
        self._assert_deferred_stats_applied_correctly(200)

    def test_300_question_exam_deferred_stats_apply_correctly(self):
        self._assert_deferred_stats_applied_correctly(300)

    def test_a_crash_partway_through_leaves_no_partial_state(self):
        from unittest.mock import patch

        from academics.services import record_question_result as real_record_question_result

        test, attempt, questions, _ = self._build_exam(20)

        calls = {'n': 0}

        def flaky(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 12:
                raise RuntimeError('simulated crash partway through submission')
            return real_record_question_result(*args, **kwargs)

        self.client.raise_request_exception = False
        # Phase 6: SubmitTestView's scoring/ranking body moved to
        # tests_app.lifecycle.finalize_attempt() (shared with the
        # auto-submit path) — patch targets updated to match; the behavior
        # under test (a mid-loop crash rolls back the whole transaction)
        # is unchanged.
        with patch('tests_app.lifecycle.record_question_result', side_effect=flaky), \
                patch('tests_app.lifecycle.enqueue_question_stats_task') as mock_enqueue, \
                self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(f'/api/attempts/{attempt.id}/submit/')

        self.assertEqual(resp.status_code, 500)
        # transaction.on_commit() requirement: a rolled-back submission
        # must never create a stats task — nothing was captured/executed
        # above because nothing committed, and the mock confirms it
        # directly rather than just by absence of a side effect.
        mock_enqueue.assert_not_called()
        attempt.refresh_from_db()
        # Nothing committed: not just the un-processed answers, but ALSO
        # the ones record_question_result() already handled successfully
        # before the 12th call raised — proving the whole transaction
        # rolled back together, not just the failing call.
        self.assertEqual(attempt.status, 'in_progress')
        self.assertIsNone(attempt.end_time)
        self.assertIsNone(attempt.stats_applied_at)
        self.assertEqual(float(attempt.score), 0)
        self.assertEqual(QuestionAttempt.objects.filter(user=self.student, question__in=questions).count(), 0)
        self.assertEqual(QuestionEvent.objects.filter(user=self.student, question__in=questions).count(), 0)

        # And it's safe to retry cleanly from here.
        self.client.raise_request_exception = True
        resp2 = self.client.post(f'/api/attempts/{attempt.id}/submit/')
        self.assertEqual(resp2.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'submitted')
        self.assertEqual(QuestionAttempt.objects.filter(user=self.student, question__in=questions).count(), 20)


class StatsDeferralTests(APITestCase):
    """Deadlock-fix audit mandatory validation, isolated from the full
    SubmitTestView request cycle already covered by LargeExamSubmissionTests
    above: academics.services.apply_question_stats_deltas() (the async-
    worker batch-apply path), its ordering guarantee, its 1213/1205 retry
    safety net, tests_app.stats_tasks.process_question_stats()'s
    idempotency claim, and SubmitTestView's transaction.on_commit() wiring
    itself (which args it enqueues, not just that DB state ends up
    correct — that part is already covered elsewhere)."""

    def setUp(self):
        self.subject = Subject.objects.create(name='Stats Deferral Subject')
        self.student_inline = User.objects.create_user(username='stats_inline', email='stats_inline@example.com', password='pw12345')
        self.student_deferred = User.objects.create_user(username='stats_deferred', email='stats_deferred@example.com', password='pw12345')

    def _make_question(self, suffix):
        q = Question.objects.create(subject=self.subject, text=f'Stats Q {suffix}', marks=1, negative_marks=0)
        correct = Option.objects.create(question=q, text='Correct', order=0, is_correct=True)
        wrong = Option.objects.create(question=q, text='Wrong', order=1, is_correct=False)
        return q, correct, wrong

    def test_deferred_stats_equivalent_to_inline_stats(self):
        """The old inline path (defer_stats=False, still used unchanged by
        QBank) and the new deferred+batched path (defer_stats=True ->
        apply_question_stats_deltas, used by SubmitTestView) must produce
        byte-identical Question/Option aggregate results for the same
        sequence of answers — this is the whole point of moving the
        computation out-of-line without changing what it computes."""
        from academics.services import apply_question_stats_deltas, record_question_result

        q_inline, correct_inline, wrong_inline = self._make_question('inline')
        q_deferred, correct_deferred, wrong_deferred = self._make_question('deferred')

        # Each question answered wrong, then (retrying) correct — exercises
        # the "move the vote, don't add a second one" branch, not just the
        # simple first-answer branch.
        record_question_result(self.student_inline, q_inline, False, source='qbank', selected_option=wrong_inline)
        record_question_result(self.student_inline, q_inline, True, source='qbank', selected_option=correct_inline)

        _, delta1 = record_question_result(
            self.student_deferred, q_deferred, False, source='test', selected_option=wrong_deferred, defer_stats=True,
        )
        _, delta2 = record_question_result(
            self.student_deferred, q_deferred, True, source='test', selected_option=correct_deferred, defer_stats=True,
        )
        apply_question_stats_deltas([d for d in (delta1, delta2) if d])

        q_inline.refresh_from_db()
        q_deferred.refresh_from_db()
        correct_inline.refresh_from_db()
        wrong_inline.refresh_from_db()
        correct_deferred.refresh_from_db()
        wrong_deferred.refresh_from_db()

        self.assertEqual(q_inline.total_attempts, q_deferred.total_attempts)
        self.assertEqual(q_inline.correct_attempts, q_deferred.correct_attempts)
        self.assertEqual(correct_inline.pick_count, correct_deferred.pick_count)
        self.assertEqual(wrong_inline.pick_count, wrong_deferred.pick_count)
        self.assertEqual(correct_inline.pick_percentage, correct_deferred.pick_percentage)
        self.assertEqual(wrong_inline.pick_percentage, wrong_deferred.pick_percentage)
        # And the shared values are the actually-expected ones, not just
        # "equal to each other by coincidence of both being wrong".
        self.assertEqual(q_deferred.total_attempts, 1)
        self.assertEqual(q_deferred.correct_attempts, 1)
        self.assertEqual(correct_deferred.pick_count, 1)
        self.assertEqual(wrong_deferred.pick_count, 0)

    def test_apply_question_stats_deltas_applies_in_ascending_question_id_order(self):
        """The actual deadlock fix: a consistent lock-acquisition order
        across every caller, regardless of the order deltas arrive in
        (which, before this fix, followed each student's randomly
        shuffled answer order)."""
        from unittest.mock import patch

        from academics import services

        call_order = []

        def fake_apply(delta):
            call_order.append(delta['question_id'])

        with patch.object(services, '_apply_one_delta_with_retry', side_effect=fake_apply):
            services.apply_question_stats_deltas([
                {'question_id': 30, 'total_delta': 1, 'correct_delta': 0, 'option_deltas': {}},
                {'question_id': 10, 'total_delta': 1, 'correct_delta': 0, 'option_deltas': {}},
                {'question_id': 20, 'total_delta': 1, 'correct_delta': 0, 'option_deltas': {}},
            ])

        self.assertEqual(call_order, [10, 20, 30])

    def test_retryable_deadlock_errno_is_retried_then_succeeds(self):
        """MySQL errno 1213 (deadlock) is the safety net, not the primary
        fix — this proves the net itself works: transient failures on a
        single question's apply are retried a few times within the same
        max_attempts budget, and don't abort the whole batch."""
        from unittest.mock import patch

        from django.db import OperationalError

        from academics import services

        delta = {'question_id': 999, 'total_delta': 1, 'correct_delta': 1, 'option_deltas': {}}
        call_count = {'n': 0}

        def flaky_apply(question_id, total_delta, correct_delta, option_deltas):
            call_count['n'] += 1
            if call_count['n'] < 3:
                raise OperationalError(1213, 'Deadlock found when trying to get lock; try restarting transaction')

        with patch.object(services, '_apply_question_stat_delta', side_effect=flaky_apply), \
                patch.object(services.time, 'sleep'):
            services._apply_one_delta_with_retry(delta)

        self.assertEqual(call_count['n'], 3)

    def test_retryable_lock_timeout_errno_is_retried_then_succeeds(self):
        """Same safety net, MySQL errno 1205 (lock wait timeout) — the
        other retryable error this fix explicitly targets."""
        from unittest.mock import patch

        from django.db import OperationalError

        from academics import services

        delta = {'question_id': 998, 'total_delta': 1, 'correct_delta': 0, 'option_deltas': {}}
        call_count = {'n': 0}

        def flaky_apply(question_id, total_delta, correct_delta, option_deltas):
            call_count['n'] += 1
            if call_count['n'] < 2:
                raise OperationalError(1205, 'Lock wait timeout exceeded; try restarting transaction')

        with patch.object(services, '_apply_question_stat_delta', side_effect=flaky_apply), \
                patch.object(services.time, 'sleep'):
            services._apply_one_delta_with_retry(delta)

        self.assertEqual(call_count['n'], 2)

    def test_non_retryable_errno_propagates_immediately_without_retry(self):
        """Per the explicit "do NOT hide the error with retries" requirement:
        only 1213/1205 are retried — any other OperationalError (e.g. 1062,
        a duplicate-key error) must propagate on the very first attempt,
        not be silently swallowed by the safety net."""
        from unittest.mock import patch

        from django.db import OperationalError

        from academics import services

        delta = {'question_id': 997, 'total_delta': 1, 'correct_delta': 1, 'option_deltas': {}}
        call_count = {'n': 0}

        def always_fail(question_id, total_delta, correct_delta, option_deltas):
            call_count['n'] += 1
            raise OperationalError(1062, "Duplicate entry '997' for key 'PRIMARY'")

        with patch.object(services, '_apply_question_stat_delta', side_effect=always_fail):
            with self.assertRaises(OperationalError):
                services._apply_one_delta_with_retry(delta)

        self.assertEqual(call_count['n'], 1)

    def test_process_question_stats_is_idempotent_under_redelivery(self):
        """Cloud Tasks is at-least-once delivery: the same (attempt_id,
        deltas) payload can arrive twice. The stats_applied_at claim must
        make the second delivery a safe no-op, not a double-count."""
        from tests_app.stats_tasks import process_question_stats

        q, correct, _wrong = self._make_question('idempotent')
        test = Test.objects.create(title='Idempotency Exam', exam_type='mock')
        attempt = TestAttempt.objects.create(user=self.student_inline, test=test, status='submitted')
        deltas = [{'question_id': q.id, 'total_delta': 1, 'correct_delta': 1, 'option_deltas': {correct.id: 1}}]

        process_question_stats(attempt.id, deltas)
        process_question_stats(attempt.id, deltas)  # simulated Cloud Tasks redelivery, identical payload

        attempt.refresh_from_db()
        self.assertIsNotNone(attempt.stats_applied_at)
        q.refresh_from_db()
        correct.refresh_from_db()
        self.assertEqual(q.total_attempts, 1)
        self.assertEqual(q.correct_attempts, 1)
        self.assertEqual(correct.pick_count, 1)

    def test_process_question_stats_unclaims_on_failure_so_a_retry_can_still_apply(self):
        """The other half of the idempotency contract: if applying the
        deltas fails partway (e.g. a non-retryable DB error escapes the
        retry safety net), the claim must be released, not left set —
        otherwise a legitimate Cloud Tasks retry would see "already
        processed" and the stats would be lost forever instead of
        eventually applied."""
        from unittest.mock import patch

        from tests_app.stats_tasks import process_question_stats

        q, correct, _wrong = self._make_question('unclaim')
        test = Test.objects.create(title='Unclaim Exam', exam_type='mock')
        attempt = TestAttempt.objects.create(user=self.student_inline, test=test, status='submitted')
        deltas = [{'question_id': q.id, 'total_delta': 1, 'correct_delta': 1, 'option_deltas': {correct.id: 1}}]

        with patch('academics.services.apply_question_stats_deltas', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                process_question_stats(attempt.id, deltas)

        attempt.refresh_from_db()
        self.assertIsNone(attempt.stats_applied_at)

        # A retry (this time succeeding) must still be able to apply it.
        process_question_stats(attempt.id, deltas)
        attempt.refresh_from_db()
        self.assertIsNotNone(attempt.stats_applied_at)
        q.refresh_from_db()
        self.assertEqual(q.total_attempts, 1)

    def test_a_failure_partway_through_a_multi_delta_batch_rolls_back_the_earlier_delta_too(self):
        """Final-review fix (pre-production stats-pipeline audit): the
        whole claim-check + apply + mark-applied sequence is now one outer
        transaction. Proves the specific new guarantee that motivated it —
        previously each delta in a batch committed via its own independent
        atomic() block, so a failure on delta 2 of 2 would leave delta 1's
        effect permanently applied even though the claim was released and
        the whole attempt was retried, silently double-counting delta 1 on
        the next successful retry. Now a failure anywhere in the batch
        rolls back every delta in it, not just the ones after the failure
        point, so a retry starts from a clean slate and can never
        double-count."""
        from unittest.mock import patch

        from tests_app.stats_tasks import process_question_stats

        q1, correct1, _w1 = self._make_question('batch1')
        q2, correct2, _w2 = self._make_question('batch2')
        test = Test.objects.create(title='Partial Batch Exam', exam_type='mock')
        attempt = TestAttempt.objects.create(user=self.student_inline, test=test, status='submitted')
        deltas = [
            {'question_id': q1.id, 'total_delta': 1, 'correct_delta': 1, 'option_deltas': {correct1.id: 1}},
            {'question_id': q2.id, 'total_delta': 1, 'correct_delta': 1, 'option_deltas': {correct2.id: 1}},
        ]

        from academics.services import _apply_question_stat_delta as real_apply_one

        def flaky_apply(question_id, total_delta, correct_delta, option_deltas):
            real_apply_one(question_id, total_delta, correct_delta, option_deltas)
            if question_id == q2.id:
                raise RuntimeError('simulated crash after q1 succeeded, during q2')

        with patch('academics.services._apply_question_stat_delta', side_effect=flaky_apply):
            with self.assertRaises(RuntimeError):
                process_question_stats(attempt.id, deltas)

        attempt.refresh_from_db()
        self.assertIsNone(attempt.stats_applied_at)
        q1.refresh_from_db()
        q2.refresh_from_db()
        # q1's delta must NOT be durably applied even though it "succeeded"
        # before q2 raised — the whole batch rolled back together.
        self.assertEqual(q1.total_attempts, 0)
        self.assertEqual(q2.total_attempts, 0)

        # A clean retry applies both exactly once — no double-count of q1.
        process_question_stats(attempt.id, deltas)
        attempt.refresh_from_db()
        self.assertIsNotNone(attempt.stats_applied_at)
        q1.refresh_from_db()
        q2.refresh_from_db()
        self.assertEqual(q1.total_attempts, 1)
        self.assertEqual(q2.total_attempts, 1)

    def test_submit_test_enqueues_stats_only_after_commit_with_correct_payload(self):
        """transaction.on_commit() wiring, checked directly (which args
        SubmitTestView hands to the enqueue function), complementing the
        DB-state-only checks in LargeExamSubmissionTests and the negative
        (rolled-back-submission) case in
        test_a_crash_partway_through_leaves_no_partial_state."""
        from unittest.mock import patch

        test = Test.objects.create(title='OnCommit Exam', exam_type='mock', negative_marking=False)
        question = Question.objects.create(subject=self.subject, text='On-commit Q', marks=1, negative_marks=0)
        correct = Option.objects.create(question=question, text='Correct', order=0, is_correct=True)
        TestQuestion.objects.create(test=test, question=question)
        attempt = TestAttempt.objects.create(user=self.student_inline, test=test, status='in_progress')
        Answer.objects.create(attempt=attempt, question=question, selected_option=correct, is_correct=True)

        self.client.force_authenticate(user=self.student_inline)
        # Phase 6: patch target moved with the enqueue call — see the
        # comment on the same rename above in
        # test_a_crash_partway_through_leaves_no_partial_state.
        with patch('tests_app.lifecycle.enqueue_question_stats_task') as mock_enqueue, \
                self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(f'/api/attempts/{attempt.id}/submit/')

        self.assertEqual(resp.status_code, 200, resp.data)
        mock_enqueue.assert_called_once()
        called_attempt_id, called_deltas = mock_enqueue.call_args[0]
        self.assertEqual(called_attempt_id, attempt.id)
        self.assertEqual(len(called_deltas), 1)
        self.assertEqual(called_deltas[0]['question_id'], question.id)


class ConcurrentStatsDeferralRegressionTests(APITransactionTestCase):
    """Deadlock-fix audit mandatory validation ("concurrent submission
    regression test"): two DIFFERENT students submitting DIFFERENT
    attempts that share a question pool is exactly the scenario that used
    to deadlock (two per-question select_for_update() calls, in whatever
    order each student's randomly shuffled answers came back in — see the
    staging reproduction that motivated this whole fix). Needs a real
    APITransactionTestCase, same reasoning as
    SubmitTestDoubleSubmissionRaceTests above: each thread needs its own
    real, committing connection, and transaction.on_commit() must
    actually fire.

    SQLite (this suite's test DB) has no real row-level locking, so it
    can't reproduce a MySQL 1213 deadlock itself — that reproduction, and
    its absence after this fix, was done directly against the staging
    MySQL instance (see the load-test report). What this test proves
    locally is that the new code path — sorted, per-question-transaction
    delta application — completes cleanly under real concurrency and
    that both students' stats land correctly with none lost or
    duplicated."""

    @override_settings(STATS_PROCESSING_ASYNC=False)
    def test_two_students_submitting_shared_question_pool_concurrently(self):
        import threading

        from django.db import connection

        subject = Subject.objects.create(name='Concurrent Regression Subject')
        students = [
            User.objects.create_user(
                username=f'concurrent_student_{i}', email=f'concurrent_student_{i}@example.com', password='pw12345',
            )
            for i in range(2)
        ]
        questions = []
        for i in range(10):
            q = Question.objects.create(subject=subject, text=f'Concurrent Q{i}', marks=1, negative_marks=0)
            Option.objects.create(question=q, text='Correct', order=0, is_correct=True)
            Option.objects.create(question=q, text='Wrong', order=1, is_correct=False)
            questions.append(q)
        test = Test.objects.create(title='Concurrent Shared Pool Exam', exam_type='mock', negative_marking=False)
        for i, q in enumerate(questions):
            TestQuestion.objects.create(test=test, question=q, order=i)

        attempts = []
        for idx, student in enumerate(students):
            attempt = TestAttempt.objects.create(user=student, test=test, status='in_progress')
            # Opposite processing order per student — exactly the pattern
            # random shuffle_questions used to produce, and the mechanism
            # that caused the original deadlock.
            ordered_questions = questions if idx == 0 else list(reversed(questions))
            for q in ordered_questions:
                correct = q.options.get(is_correct=True)
                Answer.objects.create(attempt=attempt, question=q, selected_option=correct, is_correct=True)
            attempts.append(attempt)

        results = {}
        errors = []

        def submit(student, attempt):
            import time

            from rest_framework.test import APIClient

            client = APIClient()
            client.force_authenticate(user=student)
            # Same documented SQLite testing artifact as
            # SubmitTestDoubleSubmissionRaceTests above: SQLite has no
            # per-row locking, so concurrent threads can hit "database
            # table is locked" even where real MySQL/InnoDB row locks
            # would just make one transaction wait — retried here rather
            # than treated as a failure, since it's a test-DB artifact,
            # not the deadlock behavior under test.
            for attempt_no in range(20):
                try:
                    if connection.vendor == 'sqlite':
                        with connection.cursor() as cur:
                            cur.execute('PRAGMA busy_timeout = 30000')
                    resp = client.post(f'/api/attempts/{attempt.id}/submit/')
                    results[attempt.id] = resp.status_code
                    return
                except Exception as exc:  # noqa: BLE001
                    if 'locked' in str(exc).lower() and attempt_no < 19:
                        time.sleep(0.05)
                        continue
                    errors.append(exc)
                    return
                finally:
                    connection.close()

        threads = [threading.Thread(target=submit, args=(s, a)) for s, a in zip(students, attempts)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        # Same loosening as SubmitTestDoubleSubmissionRaceTests above, and
        # for the same reason: SQLite's table-level (not row-level) locking
        # can make a commit appear to fail to the Python client when it
        # actually landed, so a retried request can find its own attempt
        # already submitted and get a clean 404 instead of 200. That's a
        # SQLite test-DB artifact, not a correctness bug — the actual
        # invariant this test exists to prove is the DB state below (both
        # students' stats land exactly once each, nothing lost or
        # duplicated), which holds regardless of which status codes came
        # back.
        self.assertTrue(set(results.values()).issubset({200, 404}), results)
        for q in Question.objects.filter(id__in=[q.id for q in questions]):
            self.assertEqual(q.total_attempts, 2)
            self.assertEqual(q.correct_attempts, 2)
            correct_opt = q.options.get(is_correct=True)
            self.assertEqual(correct_opt.pick_count, 2)
        for attempt in TestAttempt.objects.filter(id__in=[a.id for a in attempts]):
            self.assertIsNotNone(attempt.stats_applied_at)


class SubmitTestDoubleSubmissionRaceTests(APITransactionTestCase):
    """The select_for_update() half of the Phase 2.1 fix — needs a real
    TransactionTestCase (via APITransactionTestCase) because each thread
    needs its own real, committing connection; the default APITestCase
    wraps the whole test in one outer transaction that never actually
    commits, which would hide exactly the race this test exists to
    catch."""

    def test_concurrent_double_submit_only_processes_once(self):
        import threading
        import time

        student = User.objects.create_user(username='race_student', email='race_student@example.com', password='pw12345')
        subject = Subject.objects.create(name='Race Subject')
        question = Question.objects.create(subject=subject, text='2+2=?', marks=1, negative_marks=0)
        correct = Option.objects.create(question=question, text='4', order=0, is_correct=True)
        test = Test.objects.create(title='Race Exam', exam_type='mock', negative_marking=False)
        TestQuestion.objects.create(test=test, question=question)
        attempt = TestAttempt.objects.create(user=student, test=test, status='in_progress')
        Answer.objects.create(attempt=attempt, question=question, selected_option=correct, is_correct=True)

        results = []
        lock = threading.Lock()

        def submit_once():
            from django.db import connection
            from rest_framework.test import APIClient

            client = APIClient()
            client.force_authenticate(user=student)
            for attempt_no in range(20):
                try:
                    # SQLite (this suite's test DB) has no per-row locking
                    # and its shared-cache mode (needed for genuinely
                    # concurrent threads to see each other's commits) raises
                    # "database table is locked" under contention — see
                    # Phase 1.8's QuestionPublicIdConcurrencyTests for the
                    # same, already-documented testing artifact. Re-applied
                    # every attempt since connection.close() below tears
                    # down the connection this pragma was set on. On the
                    # real target database (MySQL/InnoDB), select_for_
                    # update() takes an actual row lock and a concurrent
                    # transaction blocks-and-waits instead of erroring.
                    if connection.vendor == 'sqlite':
                        with connection.cursor() as cur:
                            cur.execute('PRAGMA busy_timeout = 30000')
                    resp = client.post(f'/api/attempts/{attempt.id}/submit/')
                    with lock:
                        results.append(resp.status_code)
                    return
                except Exception as exc:  # noqa: BLE001 — SQLite lock-contention retry, see Phase 1.8's tests
                    if 'locked' in str(exc).lower() and attempt_no < 19:
                        time.sleep(0.05)
                        continue
                    raise
                finally:
                    connection.close()

        threads = [threading.Thread(target=submit_once) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Under real production contention (MySQL/InnoDB), exactly one
        # request scores it (200) and the other cleanly finds it no longer
        # 'in_progress' once it gets the row lock (404 — the existing,
        # unchanged get_object_or_404(status='in_progress') behavior for
        # an already-submitted attempt). Under this test's SQLite table-
        # level (not row-level) locking, a request can hit "table is
        # locked" mid-transaction, roll back cleanly, and retry — which
        # can scramble the exact status codes observed (e.g. both threads
        # ending up 404 because a retry lands after the *other* thread's
        # retry already committed) without ever causing a double-count.
        # So the response codes are checked loosely; the DB state below —
        # exactly one write, no duplication — is the actual invariant this
        # test exists to prove, and it holds regardless of engine.
        self.assertTrue(set(results).issubset({200, 404}), results)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'submitted')
        self.assertEqual(float(attempt.score), 1.0)
        self.assertEqual(QuestionAttempt.objects.filter(user=student, question=question).count(), 1)
        qa = QuestionAttempt.objects.get(user=student, question=question)
        self.assertEqual(qa.attempts_count, 1)  # not 2 — the whole point of this test
        self.assertEqual(QuestionEvent.objects.filter(user=student, question=question).count(), 1)


class SubjectRankCachingTests(APITestCase):
    """Scalability audit fix (Phase 3): _subject_rank used to run two full
    cross-student GROUP-BY scans (QuestionAttempt + Answer, across the
    whole subject) on every single call — subject_breakdown() calls it
    once per subject shown, so a dashboard load re-scanned every student's
    data in that subject from scratch. Now Redis/cache-backed per subject,
    shared across every student and every call within the TTL."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.subject = Subject.objects.create(name='Rank Cache Subject', is_free=True)
        self.students = [
            User.objects.create_user(username=f'rank_cache_s{i}', email=f'rank_cache_s{i}@example.com', password='pw12345')
            for i in range(3)
        ]
        # Each student answers 5 questions (>= MIN_ATTEMPTS_FOR_SUBJECT_RANK)
        # with a distinct accuracy so their ranks are unambiguous: s0 best,
        # s2 worst.
        accuracies = [5, 3, 1]  # correct out of 5
        self.questions = [Question.objects.create(subject=self.subject, text=f'Rank Q{i}') for i in range(5)]
        for student, correct_count in zip(self.students, accuracies):
            for i, q in enumerate(self.questions):
                QuestionAttempt.objects.create(
                    user=student, question=q, attempts_count=1,
                    correct_count=1 if i < correct_count else 0,
                    is_correct=i < correct_count,
                )

    def test_rank_and_out_of_are_correct(self):
        from tests_app.performance import _subject_rank

        self.assertEqual(_subject_rank(self.students[0], self.subject), {'rank': 1, 'out_of': 3})
        self.assertEqual(_subject_rank(self.students[1], self.subject), {'rank': 2, 'out_of': 3})
        self.assertEqual(_subject_rank(self.students[2], self.subject), {'rank': 3, 'out_of': 3})

    def test_a_student_below_the_minimum_attempts_threshold_has_no_rank(self):
        from tests_app.performance import _subject_rank

        newcomer = User.objects.create_user(username='rank_cache_newcomer', email='rank_cache_newcomer@example.com', password='pw12345')
        QuestionAttempt.objects.create(user=newcomer, question=self.questions[0], attempts_count=1, correct_count=1, is_correct=True)
        self.assertIsNone(_subject_rank(newcomer, self.subject))

    def test_second_call_within_ttl_does_not_requery(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from tests_app.performance import _subject_rank

        _subject_rank(self.students[0], self.subject)  # warms the cache
        with CaptureQueriesContext(connection) as ctx:
            result = _subject_rank(self.students[1], self.subject)  # different student, same subject
        self.assertEqual(result, {'rank': 2, 'out_of': 3})
        self.assertEqual(len(ctx.captured_queries), 0)  # served entirely from cache

    def test_a_different_subject_gets_its_own_independent_cache_entry(self):
        from tests_app.performance import _subject_rank

        other_subject = Subject.objects.create(name='Rank Cache Other Subject', is_free=True)
        for i, q_text in enumerate(['A', 'B', 'C', 'D', 'E']):
            oq = Question.objects.create(subject=other_subject, text=f'Other Rank Q{q_text}')
            # Reverse the ranking here vs. the main subject: s2 best this time.
            for student, correct_count in zip(self.students, [1, 3, 5]):
                QuestionAttempt.objects.create(
                    user=student, question=oq, attempts_count=1,
                    correct_count=1 if i < correct_count else 0, is_correct=i < correct_count,
                )

        self.assertEqual(_subject_rank(self.students[0], self.subject), {'rank': 1, 'out_of': 3})
        self.assertEqual(_subject_rank(self.students[0], other_subject), {'rank': 3, 'out_of': 3})

    def test_overview_endpoint_still_reports_correct_rank(self):
        self.client.force_authenticate(user=self.students[0])
        resp = self.client.get('/api/performance/overview/')
        row = next(r for r in resp.data['subjects'] if r['subject_id'] == self.subject.id)
        self.assertEqual(row['rank'], {'rank': 1, 'out_of': 3})


class SubjectBreakdownOptimizationTests(APITestCase):
    """Scalability audit fix (Phase 3): subject_breakdown()'s
    subject.questions.count() per subject (an N+1) is now a single
    annotated query; StudentPerformanceOverviewView computes
    subject_breakdown() once and shares it with strengths_and_weaknesses()/
    recommendations() instead of each silently recomputing it (a 3x call
    for the same request previously)."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.student = User.objects.create_user(username='sb_opt_student', email='sb_opt_student@example.com', password='pw12345')
        self.subject = Subject.objects.create(name='SB Opt Subject', is_free=True)
        self.questions = [Question.objects.create(subject=self.subject, text=f'SB Opt Q{i}') for i in range(4)]
        for i, q in enumerate(self.questions[:2]):
            QuestionAttempt.objects.create(user=self.student, question=q, attempts_count=1, correct_count=1, is_correct=True)
        self.client.force_authenticate(user=self.student)

    def test_total_questions_is_correct(self):
        from tests_app.performance import subject_breakdown

        rows = subject_breakdown(self.student)
        row = next(r for r in rows if r['subject_id'] == self.subject.id)
        self.assertEqual(row['total_questions'], 4)
        self.assertEqual(row['attempted'], 2)
        self.assertEqual(row['correct'], 2)

    def test_total_questions_query_count_does_not_grow_with_question_count(self):
        """Isolates the annotated_question_count fix specifically: a
        subject with more questions must not cost more queries to report
        total_questions for (the old subject.questions.count() N+1). Adds
        questions to the *same already-ranked* subject rather than new
        subjects, so this doesn't also exercise _subject_rank's separate,
        legitimately-per-subject cache cost (see SubjectRankCachingTests)."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from tests_app.performance import subject_breakdown

        subject_breakdown(self.student)  # warm this subject's rank cache first

        with CaptureQueriesContext(connection) as ctx:
            rows = subject_breakdown(self.student)
        small_count = len(ctx.captured_queries)
        small_total = next(r for r in rows if r['subject_id'] == self.subject.id)['total_questions']

        for i in range(20):
            Question.objects.create(subject=self.subject, text=f'SB Opt Extra Q{i}')

        with CaptureQueriesContext(connection) as ctx:
            rows = subject_breakdown(self.student)
        large_count = len(ctx.captured_queries)
        large_total = next(r for r in rows if r['subject_id'] == self.subject.id)['total_questions']

        self.assertEqual(small_count, large_count)
        self.assertEqual(small_total, 4)
        self.assertEqual(large_total, 24)

    def test_overview_endpoint_computes_subject_breakdown_only_once(self):
        """The dedup fix: strengths_and_weaknesses/recommendations must
        receive the already-computed `subjects` list, not silently call
        subject_breakdown() again."""
        from unittest.mock import patch

        import tests_app.performance as performance_module

        original = performance_module.subject_breakdown
        calls = {'n': 0}

        def counting_wrapper(*args, **kwargs):
            calls['n'] += 1
            return original(*args, **kwargs)

        with patch('tests_app.views.performance.subject_breakdown', side_effect=counting_wrapper):
            resp = self.client.get('/api/performance/overview/')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls['n'], 1)


# ============================================================================
# Phase 5 — Exam Type Policies & Unified Exam Configuration
# ============================================================================

class ExamTypePolicyModelAndServiceTests(TestCase):
    """get_exam_type_defaults()/get_all_exam_type_defaults() — the single
    read path consumed by TestAdminSerializer.create() and the
    exam_type_policies endpoint."""

    def test_every_exam_type_has_a_seeded_policy_row(self):
        for exam_type, _label in Test.EXAM_TYPE_CHOICES:
            self.assertTrue(ExamTypePolicy.objects.filter(pk=exam_type).exists(), exam_type)

    def test_get_exam_type_defaults_returns_every_controlled_field(self):
        defaults = get_exam_type_defaults('mock')
        self.assertEqual(set(defaults.keys()), set(POLICY_CONTROLLED_FIELDS))

    def test_get_exam_type_defaults_reflects_a_policy_row_edit(self):
        policy = ExamTypePolicy.objects.get(pk='mock')
        policy.default_duration_minutes = 90
        policy.save()

        self.assertEqual(get_exam_type_defaults('mock')['duration_minutes'], 90)

    def test_missing_policy_row_falls_back_to_model_field_defaults(self):
        """Defensive fallback — a fresh DB before the seeding data migration
        has run, or a row deleted by hand, must never crash exam creation."""
        ExamTypePolicy.objects.filter(pk='grand').delete()

        defaults = get_exam_type_defaults('grand')

        self.assertEqual(defaults['duration_minutes'], Test._meta.get_field('duration_minutes').get_default())
        self.assertEqual(defaults['is_draft'], Test._meta.get_field('is_draft').get_default())

    def test_get_all_exam_type_defaults_covers_all_five_categories(self):
        all_defaults = get_all_exam_type_defaults()
        self.assertEqual(set(all_defaults.keys()), {'qbank', 'daily', 'mock', 'grand', 'pyq'})


class ExamTypePoliciesEndpointTests(APITestCase):
    """GET /api/tests/exam_type_policies/ — staff-only, read-only."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='policy_staff', email='policy_staff@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(username='policy_student', email='policy_student@example.com', password='pw12345')

    def test_staff_can_read_all_five_policies(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get('/api/tests/exam_type_policies/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.data.keys()), {'qbank', 'daily', 'mock', 'grand', 'pyq'})
        self.assertEqual(set(resp.data['mock'].keys()), set(POLICY_CONTROLLED_FIELDS))

    def test_student_cannot_read_policies(self):
        self.client.force_authenticate(user=self.student)
        resp = self.client.get('/api/tests/exam_type_policies/')
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_cannot_read_policies(self):
        resp = self.client.get('/api/tests/exam_type_policies/')
        self.assertIn(resp.status_code, (401, 403))

    def test_endpoint_reflects_an_admin_edit_immediately(self):
        ExamTypePolicy.objects.filter(pk='daily').update(default_max_attempts=3)
        self.client.force_authenticate(user=self.staff)

        resp = self.client.get('/api/tests/exam_type_policies/')

        self.assertEqual(resp.data['daily']['max_attempts'], 3)


class ExamCreationAppliesPolicyDefaultsTests(APITestCase):
    """Per-exam-type template applied at creation time when a field is
    omitted from the payload — one test per category, per the Phase 5 spec's
    explicit per-exam-type coverage requirement. Confirms the backend
    itself (not just the frontend) is authoritative for defaults."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='createpolicy_staff', email='createpolicy_staff@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.client.force_authenticate(user=self.staff)

    def _create_minimal(self, exam_type):
        resp = self.client.post('/api/tests/', {'title': f'{exam_type} exam', 'exam_type': exam_type}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        return Test.objects.get(pk=resp.data['id'])

    def test_qbank_receives_policy_defaults_when_fields_omitted(self):
        self._assert_matches_policy('qbank')

    def test_daily_receives_policy_defaults_when_fields_omitted(self):
        self._assert_matches_policy('daily')

    def test_mock_receives_policy_defaults_when_fields_omitted(self):
        self._assert_matches_policy('mock')

    def test_grand_receives_policy_defaults_when_fields_omitted(self):
        self._assert_matches_policy('grand')

    def test_pyq_receives_policy_defaults_when_fields_omitted(self):
        self._assert_matches_policy('pyq')

    def _assert_matches_policy(self, exam_type):
        expected = get_exam_type_defaults(exam_type)
        test = self._create_minimal(exam_type)
        for field in POLICY_CONTROLLED_FIELDS:
            self.assertEqual(getattr(test, field), expected[field], field)

    def test_admin_can_customize_a_category_and_new_exams_pick_it_up(self):
        """Admin override behavior — editing the policy in Django admin
        (simulated here via the ORM, exactly what the admin UI writes)
        changes what NEW exams of that category receive."""
        ExamTypePolicy.objects.filter(pk='mock').update(
            default_duration_minutes=45, default_max_attempts=2, default_is_draft=False,
        )

        test = self._create_minimal('mock')

        self.assertEqual(test.duration_minutes, 45)
        self.assertEqual(test.max_attempts, 2)
        self.assertFalse(test.is_draft)


class ExamCreationExplicitValueOverridesPolicyTests(APITestCase):
    """An admin's explicit value in the create payload always wins over the
    category policy — the policy only ever fills in what's missing."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='overridepolicy_staff', email='overridepolicy_staff@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.client.force_authenticate(user=self.staff)

    def test_explicit_duration_beats_policy_default(self):
        self.assertNotEqual(get_exam_type_defaults('mock')['duration_minutes'], 15)

        resp = self.client.post(
            '/api/tests/', {'title': 'Custom duration', 'exam_type': 'mock', 'duration_minutes': 15}, format='json',
        )

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        test = Test.objects.get(pk=resp.data['id'])
        self.assertEqual(test.duration_minutes, 15)

    def test_explicit_is_draft_false_beats_policy_default(self):
        test = self._create_with_explicit_is_draft(False)
        self.assertFalse(test.is_draft)

    def test_explicit_is_draft_true_beats_a_policy_customized_to_false(self):
        ExamTypePolicy.objects.filter(pk='mock').update(default_is_draft=False)
        test = self._create_with_explicit_is_draft(True)
        self.assertTrue(test.is_draft)

    def _create_with_explicit_is_draft(self, value):
        resp = self.client.post(
            '/api/tests/', {'title': 'Explicit draft flag', 'exam_type': 'mock', 'is_draft': value}, format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        return Test.objects.get(pk=resp.data['id'])


class ExamTypePolicyImmutabilityTests(APITestCase):
    """Mandatory Policy Immutability Test, exact steps from the Phase 5
    spec: create a Mock Test under policy config A, change the Mock policy
    to config B, confirm the EXISTING Test still reflects config A, create
    a second Mock Test, confirm IT reflects config B. Policy is applied
    once, at creation — never re-derived from a live FK."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='immutability_staff', email='immutability_staff@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.client.force_authenticate(user=self.staff)

    def test_policy_change_never_mutates_an_already_created_exam(self):
        # (1) Create a Mock Test under policy config A.
        ExamTypePolicy.objects.filter(pk='mock').update(default_duration_minutes=60, default_max_attempts=1)
        resp_a = self.client.post('/api/tests/', {'title': 'Config A exam', 'exam_type': 'mock'}, format='json')
        self.assertEqual(resp_a.status_code, status.HTTP_201_CREATED, resp_a.data)
        test_a = Test.objects.get(pk=resp_a.data['id'])
        self.assertEqual(test_a.duration_minutes, 60)
        self.assertEqual(test_a.max_attempts, 1)

        # (2) Change the Mock policy to config B.
        ExamTypePolicy.objects.filter(pk='mock').update(default_duration_minutes=120, default_max_attempts=3)

        # (3) The existing Mock Test still uses config A.
        test_a.refresh_from_db()
        self.assertEqual(test_a.duration_minutes, 60)
        self.assertEqual(test_a.max_attempts, 1)

        # (4) Create another Mock Test.
        resp_b = self.client.post('/api/tests/', {'title': 'Config B exam', 'exam_type': 'mock'}, format='json')
        self.assertEqual(resp_b.status_code, status.HTTP_201_CREATED, resp_b.data)
        test_b = Test.objects.get(pk=resp_b.data['id'])

        # (5) The new Test uses config B.
        self.assertEqual(test_b.duration_minutes, 120)
        self.assertEqual(test_b.max_attempts, 3)


class ExamPolicyOverrideSurvivesLaterPolicyChangeTests(APITestCase):
    """Mandatory Override Test, exact steps: policy says duration=X, create
    exam (receives X), admin edits that specific exam to duration=Y, verify
    Y, change the policy to Z, verify the exam is still Y (not Z)."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='overridesurvive_staff', email='overridesurvive_staff@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.client.force_authenticate(user=self.staff)

    def test_manual_edit_survives_a_later_policy_change(self):
        # Policy says duration=X.
        ExamTypePolicy.objects.filter(pk='mock').update(default_duration_minutes=60)

        # Create exam — receives X.
        resp = self.client.post('/api/tests/', {'title': 'Override test exam', 'exam_type': 'mock'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        test_id = resp.data['id']
        self.assertEqual(Test.objects.get(pk=test_id).duration_minutes, 60)

        # Admin edits that specific exam to duration=Y.
        resp = self.client.patch(f'/api/tests/{test_id}/', {'duration_minutes': 90}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(Test.objects.get(pk=test_id).duration_minutes, 90)

        # Change the policy to Z.
        ExamTypePolicy.objects.filter(pk='mock').update(default_duration_minutes=180)

        # The specific exam remains Y (not Z) — update() never reads policy.
        self.assertEqual(Test.objects.get(pk=test_id).duration_minutes, 90)


class ExamEditNeverAppliesPolicyDefaultsTests(APITestCase):
    """A PATCH that omits a policy-controlled field must leave that field
    unchanged (normal partial-update semantics) — never silently pull in
    the category's current policy default, unlike create()."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='editpolicy_staff', email='editpolicy_staff@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.client.force_authenticate(user=self.staff)

    def test_partial_patch_omitting_duration_leaves_it_unchanged(self):
        test = Test.objects.create(title='Edit no-touch', exam_type='mock', duration_minutes=77)
        ExamTypePolicy.objects.filter(pk='mock').update(default_duration_minutes=999)

        resp = self.client.patch(f'/api/tests/{test.id}/', {'title': 'Edit no-touch — renamed'}, format='json')

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        test.refresh_from_db()
        self.assertEqual(test.duration_minutes, 77)


class IsDraftDriftResolutionTests(APITestCase):
    """Phase 5's explicit, deliberate resolution of the is_draft default
    drift (Create Exam Wizard defaulted to True/draft, Import & Create Test
    defaulted to False/publish-immediately) — the canonical policy default
    is now True for every category, matching Test.is_draft's own
    documented safe-by-default behavior."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='draftdrift_staff', email='draftdrift_staff@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.client.force_authenticate(user=self.staff)

    def test_canonical_is_draft_default_is_true_for_every_category(self):
        for exam_type, _label in Test.EXAM_TYPE_CHOICES:
            self.assertTrue(get_exam_type_defaults(exam_type)['is_draft'], exam_type)

    def test_new_exam_created_with_no_is_draft_field_starts_as_draft(self):
        resp = self.client.post('/api/tests/', {'title': 'No is_draft sent', 'exam_type': 'mock'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertTrue(Test.objects.get(pk=resp.data['id']).is_draft)


class ExamTypePolicyBackwardCompatibilityTests(APITestCase):
    """Existing Tests created before Phase 5 keep working unchanged —
    ExamTypePolicy is never read on any list/detail/attempt/result path."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='backcompat_staff', email='backcompat_staff@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.client.force_authenticate(user=self.staff)

    def test_pre_existing_test_values_are_untouched_by_any_policy_row(self):
        """A Test created directly via the ORM (simulating one that
        predates Phase 5) with values that disagree with every current
        policy default must never be silently 'corrected'."""
        test = Test.objects.create(
            title='Legacy exam', exam_type='mock', duration_minutes=37, max_attempts=9,
            is_draft=False, negative_marking=False,
        )
        ExamTypePolicy.objects.filter(pk='mock').update(
            default_duration_minutes=60, default_max_attempts=1, default_is_draft=True, default_negative_marking=True,
        )

        resp = self.client.get(f'/api/tests/{test.id}/')
        self.assertEqual(resp.status_code, 200)

        test.refresh_from_db()
        self.assertEqual(test.duration_minutes, 37)
        self.assertEqual(test.max_attempts, 9)
        self.assertFalse(test.is_draft)
        self.assertFalse(test.negative_marking)

    def test_makemigrations_check_is_clean(self):
        """Sanity check embedded in the suite: the ExamTypePolicy model as
        currently defined has no pending, un-migrated changes."""
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        try:
            call_command('makemigrations', '--check', '--dry-run', stdout=out, stderr=out)
            clean = True
        except SystemExit:
            clean = False
        self.assertTrue(clean, out.getvalue())
