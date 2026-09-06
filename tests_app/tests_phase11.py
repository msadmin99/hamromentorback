"""Phase 11 — analytics integration invariants.

The free-tier/conversion metrics the plan asked for live in
`entitlements/tests_phase11.py`. This file pins the properties of the
*existing* student analytics layer that Phase 11's audit had to verify
rather than assume — attempt eligibility, score authority, the Phase 8
relationship, the CanViewAnalytics gap the audit found, and the fact that
reading analytics never consumes quota.

Several of these assert behavior that was already correct before Phase 11.
That is deliberate: the phase's claim is that analytics observes
authoritative data without mutating or re-deriving it, and an unpinned
invariant is a claim, not a fact.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APITestCase

from academics.models import Question, Subject
from entitlements.models import EntitlementEventLog, FreeStarterEntitlement
from tests_app import performance
from tests_app.lifecycle import finalize_attempt
from tests_app.models import Answer, TestAttempt, TestQuestion
from tests_app.tests_phase6 import _mkexam
from tests_app.tests_phase8 import _mkoptions, _mkquestion

User = get_user_model()


class AttemptEligibilityTests(TestCase):
    """Which attempts count as a performance record.

    This platform has exactly two attempt statuses — `in_progress` and
    `submitted` (tests_app.models.TestAttempt.STATUS_CHOICES). Phase 6
    deliberately did NOT add a third for auto-submission: an auto-submitted
    attempt is `submitted` with an informational `auto_submitted=True`
    marker, because it is scored, ranked and reviewable in every way a
    manual submission is. And there is no "missed" row at all — a student
    who never started simply has no TestAttempt. So the eligibility rule
    the analytics layer needs is the single filter `status='submitted'`.
    """

    def setUp(self):
        self.student = User.objects.create_user(username='elig', email='elig@x.com', password='pw12345!')
        self.subject = Subject.objects.create(name='Elig Subject')
        self.test = _mkexam(allow=self.student)
        self.question = _mkquestion(self.subject, marks=2, negative_marks=0)
        self.correct, self.wrongs = _mkoptions(self.question)
        TestQuestion.objects.create(test=self.test, question=self.question)

    def _attempt(self, *, submit, auto=False, correct=True):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test)
        Answer.objects.create(
            attempt=attempt, question=self.question,
            selected_option=self.correct if correct else self.wrongs[0], is_correct=correct,
        )
        if submit:
            finalize_attempt(attempt, auto_submitted=auto)
        return attempt

    def test_in_progress_attempts_are_excluded(self):
        self._attempt(submit=False)
        kpis = performance.kpi_overview(self.student)
        self.assertEqual(kpis['total_attempts'], 0)

    def test_submitted_attempts_are_counted(self):
        self._attempt(submit=True)
        self.assertEqual(performance.kpi_overview(self.student)['total_attempts'], 1)

    def test_auto_submitted_attempts_are_counted_as_finalized_performance(self):
        """An auto-submit is a real performance record — excluding it would
        silently delete the results of every student who ran out of time."""
        attempt = self._attempt(submit=True, auto=True)
        attempt.refresh_from_db()
        self.assertTrue(attempt.auto_submitted)
        self.assertEqual(attempt.status, 'submitted')
        self.assertEqual(performance.kpi_overview(self.student)['total_attempts'], 1)

    def test_a_never_started_exam_contributes_nothing(self):
        """"Missed" is the absence of a row, not a status — there is nothing
        to exclude, and nothing may be invented for it."""
        _mkexam(allow=self.student)
        self.assertEqual(performance.kpi_overview(self.student)['total_attempts'], 0)

    def test_mixed_statuses_count_only_the_finalized_ones(self):
        self._attempt(submit=True)
        self._attempt(submit=True, auto=True)
        self._attempt(submit=False)
        self.assertEqual(performance.kpi_overview(self.student)['total_attempts'], 2)


class ScoreAuthorityTests(TestCase):
    """Analytics consumes the finalized score. It never re-derives one.

    This is the Phase 6/7/8 contract seen from the analytics side: once an
    attempt is finalized, editing the underlying question — its marks, its
    correct answer — must not move a historical analytics figure. A second
    grading system computed at read time would fail these.
    """

    def setUp(self):
        self.student = User.objects.create_user(username='auth', email='auth@x.com', password='pw12345!')
        self.subject = Subject.objects.create(name='Auth Subject')
        self.test = _mkexam(allow=self.student)
        self.question = _mkquestion(self.subject, marks=10, negative_marks=0)
        self.correct, self.wrongs = _mkoptions(self.question)
        TestQuestion.objects.create(test=self.test, question=self.question)

        attempt = TestAttempt.objects.create(user=self.student, test=self.test)
        Answer.objects.create(
            attempt=attempt, question=self.question, selected_option=self.correct, is_correct=True,
        )
        self.attempt = finalize_attempt(attempt, auto_submitted=False)
        self.attempt.refresh_from_db()
        self.finalized_score = float(self.attempt.score)

    def test_baseline_score_is_recorded(self):
        self.assertGreater(self.finalized_score, 0)
        self.assertEqual(performance.kpi_overview(self.student)['overall_score'], round(self.finalized_score, 2))

    def test_changing_question_marks_does_not_rewrite_historical_analytics(self):
        self.question.marks = Decimal('99')
        self.question.save(update_fields=['marks'])
        self.attempt.refresh_from_db()
        self.assertEqual(float(self.attempt.score), self.finalized_score)
        self.assertEqual(performance.kpi_overview(self.student)['overall_score'], round(self.finalized_score, 2))

    def test_changing_the_correct_answer_does_not_rewrite_historical_accuracy(self):
        """The classic historical-integrity case: an admin fixes a wrong key
        after students have already been graded. Their finalized result, and
        therefore their analytics, must not move."""
        before = performance.kpi_overview(self.student)
        self.correct.is_correct = False
        self.correct.save(update_fields=['is_correct'])
        self.wrongs[0].is_correct = True
        self.wrongs[0].save(update_fields=['is_correct'])

        after = performance.kpi_overview(self.student)
        self.assertEqual(after['overall_score'], before['overall_score'])
        self.assertEqual(after['overall_accuracy'], before['overall_accuracy'])
        self.assertEqual(after['questions_correct'], before['questions_correct'])


class AccuracyDefinitionTests(TestCase):
    """accuracy, percentage and score are three different things — pinned
    against hand-counted data so the definitions in the architecture doc
    are verified, not merely asserted."""

    def setUp(self):
        self.student = User.objects.create_user(username='acc', email='acc@x.com', password='pw12345!')
        self.subject = Subject.objects.create(name='Acc Subject')
        self.test = _mkexam(allow=self.student)
        self.questions = []
        for i in range(4):
            q = _mkquestion(self.subject, text=f'Q{i}', marks=1, negative_marks=0)
            correct, wrongs = _mkoptions(q)
            TestQuestion.objects.create(test=self.test, question=q)
            self.questions.append((q, correct, wrongs))

        attempt = TestAttempt.objects.create(user=self.student, test=self.test)
        # 2 correct, 1 wrong, 1 left unanswered.
        q0, c0, _ = self.questions[0]
        q1, c1, _ = self.questions[1]
        q2, _, w2 = self.questions[2]
        Answer.objects.create(attempt=attempt, question=q0, selected_option=c0, is_correct=True)
        Answer.objects.create(attempt=attempt, question=q1, selected_option=c1, is_correct=True)
        Answer.objects.create(attempt=attempt, question=q2, selected_option=w2[0], is_correct=False)
        finalize_attempt(attempt, auto_submitted=False)

    def test_accuracy_is_correct_over_answered_not_over_total(self):
        kpis = performance.kpi_overview(self.student)
        self.assertEqual(kpis['questions_attempted'], 3)
        self.assertEqual(kpis['questions_correct'], 2)
        self.assertEqual(kpis['questions_incorrect'], 1)
        self.assertEqual(kpis['questions_unanswered'], 1)
        # 2/3, not 2/4 — an unanswered question does not count against accuracy.
        self.assertEqual(kpis['overall_accuracy'], round(2 / 3 * 100, 2))

    def test_unanswered_is_total_questions_minus_answered(self):
        kpis = performance.kpi_overview(self.student)
        self.assertEqual(
            kpis['questions_unanswered'],
            self.test.question_count - kpis['questions_attempted'],
        )


class AnalyticsAuthorizationTests(APITestCase):
    """CanViewAnalytics on every analytics endpoint, and IDOR on the one
    that takes an id in the path."""

    def setUp(self):
        self.student = User.objects.create_user(username='a1', email='a1@x.com', password='pw12345!')
        self.other = User.objects.create_user(username='a2', email='a2@x.com', password='pw12345!')
        self.subject = Subject.objects.create(name='Authz Subject')
        self.test = _mkexam(allow=self.other)
        q = _mkquestion(self.subject)
        correct, _ = _mkoptions(q)
        TestQuestion.objects.create(test=self.test, question=q)
        attempt = TestAttempt.objects.create(user=self.other, test=self.test)
        Answer.objects.create(attempt=attempt, question=q, selected_option=correct, is_correct=True)
        self.other_attempt = finalize_attempt(attempt, auto_submitted=False)

    def test_comparative_denies_another_students_attempt(self):
        """IDOR: 404 rather than 403, so another student's attempt id is not
        distinguishable from one that does not exist."""
        self.client.force_authenticate(self.student)
        resp = self.client.get(f'/api/attempts/{self.other_attempt.id}/comparative/')
        self.assertEqual(resp.status_code, 404)

    def test_comparative_allows_own_attempt(self):
        self.client.force_authenticate(self.other)
        resp = self.client.get(f'/api/attempts/{self.other_attempt.id}/comparative/')
        self.assertEqual(resp.status_code, 200)

    def test_comparative_enforces_can_view_analytics(self):
        """Phase 11 audit finding: this endpoint was the one of five that
        never ran the capability check. Enforced now — and checked BEFORE
        the ownership lookup, so a capability denial cannot be inferred
        from a 404."""
        from unittest.mock import patch

        from entitlements.services import AccessDecision

        denied = AccessDecision(
            allowed=False, capability='CanViewAnalytics', source_type='none',
            reason='Analytics not available.', reason_code='not_authenticated',
        )
        self.client.force_authenticate(self.other)
        with patch('entitlements.services.can_view_analytics', return_value=denied):
            resp = self.client.get(f'/api/attempts/{self.other_attempt.id}/comparative/')
        self.assertEqual(resp.status_code, 403)

    def test_all_analytics_endpoints_require_authentication(self):
        for url in (
            '/api/performance/overview/',
            '/api/performance/calendar/',
            '/api/performance/exam-type/mock/',
            f'/api/attempts/{self.other_attempt.id}/comparative/',
        ):
            with self.subTest(url=url):
                self.assertIn(self.client.get(url).status_code, (401, 403))


class AnalyticsDoNotConsumeQuotaTests(APITestCase):
    """Reading analytics is an observation. It must never draw down a
    student's Free Starter allowance, and must never lazily provision one."""

    def setUp(self):
        self.student = User.objects.create_user(username='q1', email='q1@x.com', password='pw12345!')
        self.entitlement = FreeStarterEntitlement.objects.create(
            user=self.student, resource_type='mock_test', quantity=3, used=1,
        )
        self.client.force_authenticate(self.student)

    def test_overview_does_not_consume(self):
        events_before = EntitlementEventLog.objects.count()
        resp = self.client.get('/api/performance/overview/')
        self.assertEqual(resp.status_code, 200)
        self.entitlement.refresh_from_db()
        self.assertEqual(self.entitlement.used, 1)
        self.assertEqual(EntitlementEventLog.objects.count(), events_before)

    def test_analytics_does_not_provision_a_missing_entitlement(self):
        FreeStarterEntitlement.objects.all().delete()
        self.client.get('/api/performance/overview/')
        self.assertEqual(FreeStarterEntitlement.objects.filter(user=self.student).count(), 0)


class RankingIsNotDuplicatedTests(TestCase):
    """Analytics must reuse the authoritative Phase 7 rank/percentile, not
    compute its own."""

    def setUp(self):
        self.student = User.objects.create_user(username='r1', email='r1@x.com', password='pw12345!')
        self.subject = Subject.objects.create(name='Rank Subject')
        self.test = _mkexam(allow=self.student)
        q = _mkquestion(self.subject, marks=5, negative_marks=0)
        self.correct, _ = _mkoptions(q)
        TestQuestion.objects.create(test=self.test, question=q)
        attempt = TestAttempt.objects.create(user=self.student, test=self.test)
        Answer.objects.create(attempt=attempt, question=q, selected_option=self.correct, is_correct=True)
        self.attempt = finalize_attempt(attempt, auto_submitted=False)
        self.attempt.refresh_from_db()

    def test_comparative_reports_the_finalized_attempt_score_verbatim(self):
        comp = performance.comparative(self.student, self.attempt.id)
        self.assertEqual(comp['current']['score'], float(self.attempt.score))
        self.assertEqual(comp['current']['accuracy'], float(self.attempt.accuracy))

    def test_comparative_does_not_invent_a_rank_field(self):
        """Rank/percentile are Phase 7's, carried on TestAttempt and gated by
        CanViewRank — a separate capability from CanViewAnalytics. The
        comparative analytics payload deliberately does not emit them, so
        analytics visibility can never leak rank visibility."""
        comp = performance.comparative(self.student, self.attempt.id)
        self.assertNotIn('rank', comp)
        self.assertNotIn('percentile', comp)
        self.assertNotIn('rank', comp['current'])


class QuestionTaxonomyIndexTests(TestCase):
    """Plan bullet 2 — the composite taxonomy index actually exists in the
    model state (and therefore in the migration that was generated from it)."""

    def test_composite_subject_chapter_topic_index_is_declared(self):
        names = {idx.name for idx in Question._meta.indexes}
        self.assertIn('question_taxonomy_idx', names)

    def test_index_covers_the_taxonomy_hot_path_in_order(self):
        idx = next(i for i in Question._meta.indexes if i.name == 'question_taxonomy_idx')
        self.assertEqual(idx.fields, ['subject', 'chapter', 'topic'])

    def test_adding_meta_did_not_introduce_a_default_ordering(self):
        """Question had no Meta before Phase 11. Declaring one must not add
        an implicit ordering, which would silently change the row order of
        every existing Question queryset in the codebase."""
        self.assertEqual(Question._meta.ordering, [])
