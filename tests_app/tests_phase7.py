"""Phase 7 — Results, Solutions, Ranking & Analytics Access: regression suite.

Covers CanViewSolutions' real enforcement of Test.solutions_visibility
(auto/manual + release), the manual-release endpoints and their audit
trail, solution-bypass coverage across every endpoint that can reveal
correct-answer/explanation content, ranking/analytics capability wiring,
auto-submitted/missed-attempt result handling, IDOR, and cross-source
entitlement independence for review access. See
docs/PHASE_7_ARCHITECTURE.md for the design these tests hold to account.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from academics.models import Option, Question, Subject
from academics.serializers import QuestionResultSerializer
from core.models import AdminEditAuditLog
from entitlements.services import can_view_analytics, can_view_rank, can_view_solutions
from tests_app.models import Answer, ExamSession, ExamTemplate, Test, TestAttempt, TestQuestion
from tests_app.tests_phase6 import _backdate_start, _mkexam, _mksession

User = get_user_model()


def _submitted_attempt(test, user, session=None, score=0, **overrides):
    fields = {'user': user, 'test': test, 'session': session, 'status': 'submitted', 'score': score, 'end_time': timezone.now()}
    fields.update(overrides)
    return TestAttempt.objects.create(**fields)


class CanViewSolutionsTests(TestCase):
    """The core policy matrix: auto vs manual, session-scoped vs
    session-less, released vs not."""

    def setUp(self):
        self.student = User.objects.create_user(username='cvs_student', email='cvs_student@example.com', password='pw')

    def test_in_progress_attempt_denied(self):
        test = _mkexam(solutions_visibility='auto')
        attempt = TestAttempt.objects.create(user=self.student, test=test)  # in_progress
        self.assertFalse(can_view_solutions(self.student, attempt).allowed)

    def test_auto_session_less_visible_immediately_on_submit(self):
        test = _mkexam(solutions_visibility='auto')
        attempt = _submitted_attempt(test, self.student)
        self.assertTrue(can_view_solutions(self.student, attempt).allowed)

    def test_auto_session_scoped_locked_while_session_still_open(self):
        test = _mkexam(solutions_visibility='auto')
        now = timezone.now()
        session = _mksession(test, now - timezone.timedelta(hours=1), now + timezone.timedelta(hours=2))
        attempt = _submitted_attempt(test, self.student, session=session)
        decision = can_view_solutions(self.student, attempt)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, 'solutions_not_released')

    def test_auto_session_scoped_visible_once_window_ends(self):
        test = _mkexam(solutions_visibility='auto')
        now = timezone.now()
        session = _mksession(test, now - timezone.timedelta(hours=3), now - timezone.timedelta(hours=1))
        attempt = _submitted_attempt(test, self.student, session=session)
        self.assertTrue(can_view_solutions(self.student, attempt).allowed)

    def test_manual_session_less_locked_until_test_release(self):
        test = _mkexam(solutions_visibility='manual')
        attempt = _submitted_attempt(test, self.student)
        self.assertFalse(can_view_solutions(self.student, attempt).allowed)

        test.solutions_released_at = timezone.now()
        test.save(update_fields=['solutions_released_at'])
        self.assertTrue(can_view_solutions(self.student, attempt).allowed)

    def test_manual_session_scoped_locked_even_after_window_ends_until_explicit_release(self):
        test = _mkexam(solutions_visibility='manual')
        now = timezone.now()
        session = _mksession(test, now - timezone.timedelta(hours=3), now - timezone.timedelta(hours=1))
        attempt = _submitted_attempt(test, self.student, session=session)
        self.assertFalse(can_view_solutions(self.student, attempt).allowed)

        session.solutions_released_at = timezone.now()
        session.save(update_fields=['solutions_released_at'])
        self.assertTrue(can_view_solutions(self.student, attempt).allowed)

    def test_manual_releasing_one_session_never_releases_a_sibling_session(self):
        """The exact scenario the two independent solutions_released_at
        fields exist to prevent: a re-released Daily Test's Session #1
        being released must never leak into a still-open Session #2."""
        test = _mkexam(solutions_visibility='manual')
        now = timezone.now()
        session1 = _mksession(test, now - timezone.timedelta(hours=27), now - timezone.timedelta(hours=25))
        session1.solutions_released_at = timezone.now()
        session1.save(update_fields=['solutions_released_at'])

        session2 = ExamSession.objects.create(
            exam_template=session1.exam_template, exam_version=test, session_name='Session 2',
            start_datetime=now - timezone.timedelta(hours=1), end_datetime=now + timezone.timedelta(hours=1),
        )
        attempt2 = _submitted_attempt(test, self.student, session=session2)
        self.assertFalse(can_view_solutions(self.student, attempt2).allowed)

    def test_auto_is_the_default_and_matches_pre_phase7_behavior_for_anytime_tests(self):
        test = _mkexam()  # default solutions_visibility='auto'
        self.assertEqual(test.solutions_visibility, 'auto')
        attempt = _submitted_attempt(test, self.student)
        self.assertTrue(can_view_solutions(self.student, attempt).allowed)

    def test_not_owner_denied_regardless_of_release_state(self):
        other = User.objects.create_user(username='cvs_other', email='cvs_other@example.com', password='pw')
        test = _mkexam(solutions_visibility='auto')
        attempt = _submitted_attempt(test, self.student)
        self.assertFalse(can_view_solutions(other, attempt).allowed)


class QuestionResultSerializerGatingTests(TestCase):
    """Direct serializer-level proof that solution content is actually
    stripped, not just hidden by a view-level 403 — the real defense
    against 'the serializer still includes it, some other caller reveals
    it' bypass class."""

    def setUp(self):
        self.subject = Subject.objects.create(name='Gating Subject')
        self.question = Question.objects.create(
            subject=self.subject, text='Q?', marks=2, negative_marks=0,
            explanation='The real explanation', key_takeaway='Remember this',
        )
        self.correct = Option.objects.create(question=self.question, text='Right', order=0, is_correct=True)
        self.wrong = Option.objects.create(question=self.question, text='Wrong', order=1, is_correct=False)

    def _serialize(self, show_solutions):
        context = {'attempt_map': {}, 'show_solutions': show_solutions}
        return QuestionResultSerializer(self.question, context=context).data

    def test_locked_strips_explanation_and_is_correct(self):
        data = self._serialize(show_solutions=False)
        self.assertTrue(data['solutions_locked'])
        self.assertNotIn('explanation', data)
        self.assertNotIn('is_correct', data)
        self.assertNotIn('key_takeaway', data)
        for opt in data['options']:
            self.assertNotIn('is_correct', opt)
            self.assertNotIn('explanation', opt)

    def test_locked_still_shows_question_and_option_text(self):
        """The lock hides the ANSWER, never the question itself."""
        data = self._serialize(show_solutions=False)
        self.assertEqual(data['text'], 'Q?')
        option_texts = {opt['text'] for opt in data['options']}
        self.assertEqual(option_texts, {'Right', 'Wrong'})

    def test_unlocked_includes_everything(self):
        data = self._serialize(show_solutions=True)
        self.assertFalse(data['solutions_locked'])
        self.assertEqual(data['explanation'], 'The real explanation')
        self.assertIn('is_correct', data)
        self.assertTrue(any(opt['is_correct'] for opt in data['options']))

    def test_defaults_to_unlocked_when_show_solutions_key_absent(self):
        """QuestionViewSet.answer() (the QBank immediate-explanation
        feature) never sets show_solutions — must stay completely
        unaffected by this phase."""
        data = QuestionResultSerializer(self.question, context={'attempt_map': {}}).data
        self.assertFalse(data['solutions_locked'])
        self.assertIn('explanation', data)


class SolutionBypassTests(APITestCase):
    """AttemptDetailView and TestResultView must agree — both serialize
    through the same TestResultSerializer, so neither can be used to
    bypass the other's solution gating. This is the actual fix for the
    Phase 4-era gap where both endpoints gated on CanReview alone."""

    def setUp(self):
        self.student = User.objects.create_user(username='byp_student', email='byp_student@example.com', password='pw')
        self.client.force_authenticate(user=self.student)
        self.subject = Subject.objects.create(name='Bypass Subject')
        self.question = Question.objects.create(subject=self.subject, text='Q?', marks=1, negative_marks=0, explanation='secret')
        self.correct = Option.objects.create(question=self.question, text='A', order=0, is_correct=True)

    def _locked_attempt(self):
        test = _mkexam(solutions_visibility='manual', allow=self.student)
        TestQuestion.objects.create(test=test, question=self.question)
        return _submitted_attempt(test, self.student)

    def test_attempt_detail_view_hides_solutions_when_locked(self):
        attempt = self._locked_attempt()
        resp = self.client.get(f'/api/attempts/{attempt.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['can_view_solutions'])
        for q in resp.data['questions']:
            self.assertTrue(q['solutions_locked'])
            self.assertNotIn('explanation', q)

    def test_result_view_hides_solutions_when_locked(self):
        attempt = self._locked_attempt()
        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['can_view_solutions'])
        for q in resp.data['questions']:
            self.assertTrue(q['solutions_locked'])
            self.assertNotIn('explanation', q)

    def test_both_endpoints_agree_once_released(self):
        attempt = self._locked_attempt()
        attempt.test.solutions_released_at = timezone.now()
        attempt.test.save(update_fields=['solutions_released_at'])

        detail = self.client.get(f'/api/attempts/{attempt.id}/')
        result = self.client.get(f'/api/attempts/{attempt.id}/result/')
        self.assertTrue(detail.data['can_view_solutions'])
        self.assertTrue(result.data['can_view_solutions'])
        self.assertFalse(detail.data['questions'][0]['solutions_locked'])
        self.assertFalse(result.data['questions'][0]['solutions_locked'])

    def test_score_and_rank_still_visible_while_solutions_locked(self):
        """CanReview (score/rank/status) is a separate, broader gate than
        CanViewSolutions — locking solutions must never hide the score."""
        attempt = self._locked_attempt()
        TestAttempt.objects.filter(pk=attempt.pk).update(score=1, rank=1, percentile=100)
        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')
        self.assertEqual(float(resp.data['score']), 1.0)
        self.assertEqual(resp.data['rank'], 1)

    def test_selected_option_still_visible_while_solutions_locked(self):
        """'Do not accidentally reveal the correct answer merely because
        the student can see their own selection.'"""
        attempt = self._locked_attempt()
        Answer.objects.create(attempt=attempt, question=self.question, selected_option=self.correct, is_correct=True)
        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')
        q = resp.data['questions'][0]
        self.assertEqual(q['selected_option_id'], self.correct.id)
        self.assertNotIn('is_correct', q)  # the judgment on that selection stays hidden

    def test_wrong_correct_filter_disabled_while_locked(self):
        """The ?filter=wrong/correct query param is itself a soft solution
        leak while locked — forced to 'all'."""
        attempt = self._locked_attempt()
        second_question = Question.objects.create(subject=self.subject, text='Q2?', marks=1, negative_marks=0)
        TestQuestion.objects.create(test=attempt.test, question=second_question)

        resp = self.client.get(f'/api/attempts/{attempt.id}/result/?filter=wrong')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['questions']), 2)  # both, not filtered

    def test_in_progress_attempt_never_reaches_the_solution_serializer_at_all(self):
        """Pre-existing Phase 4 fix, re-confirmed: an in-progress attempt
        gets TestAttemptSerializer (no solution fields exist on it at
        all), never TestResultSerializer."""
        test = _mkexam(solutions_visibility='auto', allow=self.student)
        TestQuestion.objects.create(test=test, question=self.question)
        attempt = TestAttempt.objects.create(user=self.student, test=test)
        resp = self.client.get(f'/api/attempts/{attempt.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('score', resp.data)  # TestAttemptSerializer shape, not TestResultSerializer's


class ReleaseSolutionsEndpointTests(APITestCase):
    """The manual-release actions: permission (feature-gated), audit
    trail, idempotency, and the 400 guard for a non-manual test/session."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username='rel_admin', email='rel_admin@example.com', password='pw', is_staff=True, admin_role='admin',
        )
        self.staff_no_feature = User.objects.create_user(
            username='rel_staff_nf', email='rel_staff_nf@example.com', password='pw', is_staff=True, admin_role='editor',
        )
        self.student = User.objects.create_user(username='rel_student', email='rel_student@example.com', password='pw')

    def test_admin_can_release_test_level_solutions(self):
        test = _mkexam(solutions_visibility='manual')
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(f'/api/tests/{test.id}/release_solutions/')
        self.assertEqual(resp.status_code, 200)
        test.refresh_from_db()
        self.assertIsNotNone(test.solutions_released_at)
        self.assertEqual(test.solutions_released_by_id, self.admin.id)

    def test_editor_without_feature_denied(self):
        test = _mkexam(solutions_visibility='manual')
        self.client.force_authenticate(user=self.staff_no_feature)
        resp = self.client.post(f'/api/tests/{test.id}/release_solutions/')
        self.assertEqual(resp.status_code, 403)
        test.refresh_from_db()
        self.assertIsNone(test.solutions_released_at)

    def test_student_denied(self):
        test = _mkexam(solutions_visibility='manual')
        self.client.force_authenticate(user=self.student)
        resp = self.client.post(f'/api/tests/{test.id}/release_solutions/')
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_denied(self):
        test = _mkexam(solutions_visibility='manual')
        resp = self.client.post(f'/api/tests/{test.id}/release_solutions/')
        self.assertIn(resp.status_code, (401, 403))

    def test_release_on_auto_test_is_a_400_not_a_silent_success(self):
        test = _mkexam(solutions_visibility='auto')
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(f'/api/tests/{test.id}/release_solutions/')
        self.assertEqual(resp.status_code, 400)

    def test_release_writes_an_audit_log_entry(self):
        test = _mkexam(solutions_visibility='manual')
        self.client.force_authenticate(user=self.admin)
        self.client.post(f'/api/tests/{test.id}/release_solutions/')
        entry = AdminEditAuditLog.objects.filter(resource_type='Test', resource_id=str(test.id)).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor_id, self.admin.id)
        self.assertIn('solutions_released_at', entry.changed_fields)

    def test_release_is_idempotent_never_restamps(self):
        test = _mkexam(solutions_visibility='manual')
        self.client.force_authenticate(user=self.admin)
        self.client.post(f'/api/tests/{test.id}/release_solutions/')
        test.refresh_from_db()
        first_at = test.solutions_released_at

        second_admin = User.objects.create_user(
            username='rel_admin2', email='rel_admin2@example.com', password='pw', is_staff=True, admin_role='admin',
        )
        self.client.force_authenticate(user=second_admin)
        self.client.post(f'/api/tests/{test.id}/release_solutions/')
        test.refresh_from_db()
        self.assertEqual(test.solutions_released_at, first_at)
        self.assertEqual(test.solutions_released_by_id, self.admin.id)  # not overwritten by the second caller

    def test_session_level_release_is_independent_of_test_level(self):
        test = _mkexam(solutions_visibility='manual')
        now = timezone.now()
        session = _mksession(test, now - timezone.timedelta(hours=3), now - timezone.timedelta(hours=1))
        self.client.force_authenticate(user=self.admin)

        resp = self.client.post(f'/api/exam-sessions/{session.id}/release_solutions/')
        self.assertEqual(resp.status_code, 200)
        session.refresh_from_db()
        test.refresh_from_db()
        self.assertIsNotNone(session.solutions_released_at)
        self.assertIsNone(test.solutions_released_at)  # Test-level field untouched


class RankAndAnalyticsCapabilityTests(APITestCase):
    """CanViewRank/CanViewAnalytics, explicitly exercised — including the
    now-wired-in analytics endpoints."""

    def setUp(self):
        self.student = User.objects.create_user(username='rank_student', email='rank_student@example.com', password='pw')
        self.other = User.objects.create_user(username='rank_other', email='rank_other@example.com', password='pw')

    def test_can_view_rank_requires_submitted_and_ownership(self):
        test = _mkexam()
        in_progress = TestAttempt.objects.create(user=self.student, test=test)
        self.assertFalse(can_view_rank(self.student, in_progress).allowed)

        submitted = _submitted_attempt(test, self.student)
        self.assertTrue(can_view_rank(self.student, submitted).allowed)
        self.assertFalse(can_view_rank(self.other, submitted).allowed)

    def test_can_view_analytics_self_only(self):
        self.assertTrue(can_view_analytics(self.student, self.student).allowed)
        self.assertFalse(can_view_analytics(self.student, self.other).allowed)

    def test_can_view_analytics_denies_anonymous(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertFalse(can_view_analytics(AnonymousUser(), self.student).allowed)

    def test_performance_overview_endpoint_reachable_for_authenticated_self(self):
        self.client.force_authenticate(user=self.student)
        resp = self.client.get('/api/performance/overview/')
        self.assertEqual(resp.status_code, 200)

    def test_performance_calendar_endpoint_reachable_for_authenticated_self(self):
        self.client.force_authenticate(user=self.student)
        resp = self.client.get('/api/performance/calendar/')
        self.assertEqual(resp.status_code, 200)

    def test_ranking_scoped_per_session_not_global(self):
        """Grand/Daily ranking must not merge across sessions of the same
        Test — re-confirms the Phase 6 scoping still holds under Phase 7's
        additional gating."""
        test = _mkexam()
        now = timezone.now()
        session_a = _mksession(test, now - timezone.timedelta(hours=5), now - timezone.timedelta(hours=3))
        session_b = ExamSession.objects.create(
            exam_template=session_a.exam_template, exam_version=test, session_name='B',
            start_datetime=now - timezone.timedelta(hours=2), end_datetime=now - timezone.timedelta(hours=1),
        )
        _submitted_attempt(test, self.other, session=session_a, score=100, rank=1, percentile=100)
        my_attempt = _submitted_attempt(test, self.student, session=session_b, score=1, rank=1, percentile=100)
        # my_attempt's rank was set directly (finalize_attempt's own ranking
        # math is Phase 6 territory, already tested there) — this test only
        # confirms CanViewRank doesn't block a legitimately-scoped result.
        self.assertTrue(can_view_rank(self.student, my_attempt).allowed)
        self.assertEqual(my_attempt.rank, 1)  # rank 1 in ITS OWN session's pool, unaffected by session_a's 100-scorer


class AutoSubmittedAndMissedResultTests(APITestCase):
    """Auto-submitted attempts are completed for every result purpose;
    never-started students get no fake result."""

    def setUp(self):
        self.student = User.objects.create_user(username='asm_student', email='asm_student@example.com', password='pw')
        self.client.force_authenticate(user=self.student)
        self.subject = Subject.objects.create(name='AutoSubmit Subject')
        self.question = Question.objects.create(subject=self.subject, text='Q?', marks=1, negative_marks=0)

    def test_auto_submitted_attempt_is_fully_reviewable(self):
        test = _mkexam(solutions_visibility='auto', duration_minutes=30, allow=self.student)
        TestQuestion.objects.create(test=test, question=self.question)
        attempt = TestAttempt.objects.create(user=self.student, test=test)
        attempt = _backdate_start(attempt, timezone.now() - timezone.timedelta(hours=1))

        # Touching it lazily finalizes it (Phase 6) — then Phase 7's gating applies identically.
        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')
        self.assertEqual(resp.status_code, 200)
        attempt.refresh_from_db()
        self.assertTrue(attempt.auto_submitted)
        self.assertTrue(resp.data['can_view_solutions'])
        self.assertTrue(resp.data['can_view_rank'])

    def test_never_started_scheduled_exam_yields_no_result(self):
        test = _mkexam(exam_type='daily', allow=self.student)
        TestQuestion.objects.create(test=test, question=self.question)
        now = timezone.now()
        _mksession(test, now - timezone.timedelta(hours=25), now - timezone.timedelta(hours=1))
        self.assertFalse(TestAttempt.objects.filter(user=self.student, test=test).exists())
        # No attempt exists at all — there is structurally no attempt id to request a result for.

    def test_missed_student_cannot_fabricate_a_result_via_another_students_attempt_id(self):
        other = User.objects.create_user(username='asm_other', email='asm_other@example.com', password='pw')
        test = _mkexam(exam_type='daily', allow=[self.student, other])
        TestQuestion.objects.create(test=test, question=self.question)
        now = timezone.now()
        session = _mksession(test, now - timezone.timedelta(hours=25), now - timezone.timedelta(hours=1))
        others_attempt = _submitted_attempt(test, other, session=session, score=5)

        resp = self.client.get(f'/api/attempts/{others_attempt.id}/result/')
        self.assertEqual(resp.status_code, 404)


class CrossSourceReviewIndependenceTests(APITestCase):
    """Review/solution access, once an attempt exists, must not depend on
    the student's CURRENT entitlement state — only on ownership + attempt
    status + solutions_visibility policy. Re-verifying this invariant
    explicitly, since can_review_attempt/can_view_solutions never
    re-check entitlement at all (by design — see their docstrings)."""

    def setUp(self):
        self.student = User.objects.create_user(username='xsrc_student', email='xsrc_student@example.com', password='pw')

    def test_review_access_survives_a_later_subscription_expiry(self):
        test = _mkexam(exam_type='mock', is_pro=True, solutions_visibility='auto')
        attempt = _submitted_attempt(test, self.student)
        # No subscription/entitlement of any kind exists for this student —
        # simulating "their access has since lapsed" — review must still work.
        self.assertTrue(can_view_solutions(self.student, attempt).allowed)

    def test_review_access_survives_free_starter_exhaustion(self):
        test = _mkexam(exam_type='daily', is_pro=True, solutions_visibility='auto')
        attempt = _submitted_attempt(test, self.student)
        self.assertTrue(can_view_solutions(self.student, attempt).allowed)
