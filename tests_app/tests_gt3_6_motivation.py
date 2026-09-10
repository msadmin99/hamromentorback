"""Grand Test 3.0 / GT3-6 — Personalized Motivation + Smart Practice
Integration + Exam Recommendations + Grand Test Series Analytics.

Core rules under test:

1. Score-band motivation (GrandTestMotivationBand) is wired into the
   appeared result response, using the exact seeded bands.
2. Top-3 recommendations are real, evidence-based (never invented), and
   access-aware (a CTA is only ever present when genuinely actionable).
3. A missed student's motivation payload NEVER contains a fabricated
   score/rank/accuracy or current-test weakness breakdown.
4. Grand Test series analytics (trend/attendance/personal best) never
   counts a missed test as a zero-score attempt.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from academics.models import Chapter, Option, Question, Subject, Topic
from billing import payment_service
from billing.access import get_grand_test_access
from billing.models import Purchase
from tests_app.grand_test_analytics import grand_test_series_summary, motivation_for_score
from tests_app.models import Answer, GrandTestMotivationBand, Test, TestAttempt, TestQuestion

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


class MotivationBandLookupTests(APITestCase):
    """Direct, non-HTTP checks against the 6 seeded default bands (GT3-6
    spec §16) plus the safe fallback for an out-of-range/misconfigured
    input."""

    def test_all_six_seeded_bands_are_present(self):
        self.assertEqual(GrandTestMotivationBand.objects.count(), 6)

    def test_outstanding_band(self):
        self.assertEqual(motivation_for_score(95)['title'], 'Outstanding Performance')

    def test_excellent_band(self):
        self.assertEqual(motivation_for_score(80)['title'], 'Excellent Progress')

    def test_good_band(self):
        self.assertEqual(motivation_for_score(65)['title'], 'Good Progress')

    def test_keep_improving_band(self):
        self.assertEqual(motivation_for_score(45)['title'], 'Keep Improving')

    def test_build_foundation_band(self):
        self.assertEqual(motivation_for_score(25)['title'], 'Build the Foundation')

    def test_start_step_by_step_band(self):
        self.assertEqual(motivation_for_score(5)['title'], 'Start Step by Step')

    def test_boundary_values_resolve_to_the_correct_band(self):
        self.assertEqual(motivation_for_score(90)['title'], 'Outstanding Performance')
        self.assertEqual(motivation_for_score(89.99)['title'], 'Excellent Progress')
        self.assertEqual(motivation_for_score(0)['title'], 'Start Step by Step')
        self.assertEqual(motivation_for_score(100)['title'], 'Outstanding Performance')

    def test_out_of_range_score_falls_back_safely_without_crashing(self):
        result = motivation_for_score(-5)
        self.assertEqual(result['title'], 'Result Available')

    def test_empty_band_table_falls_back_safely(self):
        GrandTestMotivationBand.objects.all().delete()
        result = motivation_for_score(72)
        self.assertEqual(result['title'], 'Result Available')
        self.assertTrue(result['message'])


class GrandTestResultFixture(APITestCase):
    """Two-subject Grand Test: Physics weak (0% accuracy, 2 attempted),
    Chemistry strong (100%), one question left completely unanswered."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt6m_a', email='gt6m_a@example.com', password='pw12345')

        self.physics = Subject.objects.create(name='GT6M Physics')
        self.chapter = Chapter.objects.create(subject=self.physics, name='Mechanics')
        self.topic = Topic.objects.create(chapter=self.chapter, name='Kinematics')
        self.p1, self.p1c, self.p1w = _mkq(self.physics, self.chapter, self.topic, 'P1')
        self.p2, self.p2c, self.p2w = _mkq(self.physics, self.chapter, self.topic, 'P2')

        self.chemistry = Subject.objects.create(name='GT6M Chemistry')
        self.chem_chapter = Chapter.objects.create(subject=self.chemistry, name='Organic')
        self.chem_topic = Topic.objects.create(chapter=self.chem_chapter, name='Alkanes')
        self.c1, self.c1c, self.c1w = _mkq(self.chemistry, self.chem_chapter, self.chem_topic, 'C1')
        self.c2, self.c2c, self.c2w = _mkq(self.chemistry, self.chem_chapter, self.chem_topic, 'C2')
        self.untouched, _, _ = _mkq(self.chemistry, self.chem_chapter, self.chem_topic, 'U1')

        now = timezone.now()
        self.test = Test.objects.create(
            title='GT6M Grand', exam_type='grand', is_pro=True, price=1000, max_attempts=1, is_draft=False,
            scheduled_start=now - timezone.timedelta(hours=2), scheduled_end=now - timezone.timedelta(minutes=1),
        )
        self.test.assigned_students.set([self.student])
        for i, q in enumerate([self.p1, self.p2, self.c1, self.c2, self.untouched]):
            TestQuestion.objects.create(test=self.test, question=q, order=i)

        # score=2 out of total_marks=5 -> 40% -> 'Keep Improving' band.
        self.attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='submitted', score=2)
        Answer.objects.create(attempt=self.attempt, question=self.p1, selected_option=self.p1w, is_correct=False)
        Answer.objects.create(attempt=self.attempt, question=self.p2, selected_option=self.p2w, is_correct=False)
        Answer.objects.create(attempt=self.attempt, question=self.c1, selected_option=self.c1c, is_correct=True)
        Answer.objects.create(attempt=self.attempt, question=self.c2, selected_option=self.c2c, is_correct=True)

        _grant_access(self.student, self.test)
        self.client.force_authenticate(user=self.student)


class AppearedResultMotivationAPITests(GrandTestResultFixture):
    def test_result_response_includes_correct_motivation_band(self):
        resp = self.client.get(f'/api/attempts/{self.attempt.id}/result/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['motivation']['title'], 'Keep Improving')

    def test_result_response_includes_top_recommendations_naming_the_weak_subject(self):
        resp = self.client.get(f'/api/attempts/{self.attempt.id}/result/')
        recs = resp.data['grand_test_recommendations']
        self.assertGreater(len(recs), 0)
        self.assertLessEqual(len(recs), 3)
        first = recs[0]
        self.assertIn('GT6M Physics', first['why'])
        self.assertIn('what', first)
        self.assertIn('why', first)
        self.assertIn('how', first)
        self.assertIn('future_benefit', first)
        # Cautious language only — never a guaranteed-outcome claim.
        for rec in recs:
            self.assertNotIn('will increase your score', rec['future_benefit'].lower())

    def test_recommendation_cta_is_never_a_bare_start_action(self):
        """Grand Test itself must never become unlimited practice — a
        recommendation CTA is 'practice_now' (Smart Practice) or
        'view_exam'/'view_review', never a raw 'start' pointed back at
        the Grand Test."""
        resp = self.client.get(f'/api/attempts/{self.attempt.id}/result/')
        for rec in resp.data['grand_test_recommendations']:
            cta = rec.get('cta')
            if cta:
                self.assertNotEqual(cta['action'], 'start')

    def test_timing_recommendation_has_no_mock_test_cta_when_none_accessible(self):
        resp = self.client.get(f'/api/attempts/{self.attempt.id}/result/')
        timing = next((r for r in resp.data['grand_test_recommendations'] if r['type'] == 'timing'), None)
        self.assertIsNotNone(timing)
        self.assertIsNone(timing['cta'])

    def test_timing_recommendation_gets_a_real_mock_test_cta_when_one_is_accessible(self):
        mock_test = Test.objects.create(title='GT6M Mock', exam_type='mock', is_draft=False, is_pro=False)
        mock_test.assigned_students.set([self.student])
        resp = self.client.get(f'/api/attempts/{self.attempt.id}/result/')
        timing = next((r for r in resp.data['grand_test_recommendations'] if r['type'] == 'timing'), None)
        self.assertIsNotNone(timing['cta'])
        self.assertEqual(timing['cta']['test_id'], mock_test.id)
        self.assertEqual(timing['cta']['action'], 'view_exam')

    def test_non_grand_test_result_has_null_motivation_and_empty_recommendations(self):
        daily = Test.objects.create(title='GT6M Daily', exam_type='daily', is_draft=False)
        TestQuestion.objects.create(test=daily, question=self.c1, order=0)
        daily_attempt = TestAttempt.objects.create(user=self.student, test=daily, status='submitted', score=1)
        Answer.objects.create(attempt=daily_attempt, question=self.c1, selected_option=self.c1c, is_correct=True)
        resp = self.client.get(f'/api/attempts/{daily_attempt.id}/result/')
        self.assertIsNone(resp.data['motivation'])
        self.assertEqual(resp.data['grand_test_recommendations'], [])


class MissedStudentMotivationAPITests(GrandTestResultFixture):
    """The missed student in this fixture is a SEPARATE user who never
    started — proves no current-test performance data is ever fabricated
    for them."""

    def setUp(self):
        super().setUp()
        self.missed_student = User.objects.create_user(username='gt6m_b', email='gt6m_b@example.com', password='pw12345')
        self.test.assigned_students.add(self.missed_student)
        _grant_access(self.missed_student, self.test)
        self.client.force_authenticate(user=self.missed_student)

    def test_missed_review_includes_motivation_with_no_fabricated_performance_data(self):
        resp = self.client.get(f'/api/tests/{self.test.id}/missed-review/')
        self.assertEqual(resp.status_code, 200, resp.data)
        motivation = resp.data['motivation']
        self.assertIsNotNone(motivation)
        # Never a score/rank/accuracy/subject-weakness field anywhere in
        # the missed payload.
        forbidden_keys = {'score', 'rank', 'accuracy', 'percentile', 'weak_subject', 'weak_topics'}
        self.assertFalse(forbidden_keys & set(motivation.keys()))
        self.assertFalse(forbidden_keys & set(resp.data.keys()))
        self.assertIn('missed', motivation['title'].lower())

    def test_missed_review_mock_recommendation_only_present_when_real_and_accessible(self):
        resp = self.client.get(f'/api/tests/{self.test.id}/missed-review/')
        self.assertIsNone(resp.data['motivation']['recommended_action'])

        mock_test = Test.objects.create(title='GT6M Mock 2', exam_type='mock', is_draft=False, is_pro=False)
        mock_test.assigned_students.set([self.missed_student])
        resp2 = self.client.get(f'/api/tests/{self.test.id}/missed-review/')
        action = resp2.data['motivation']['recommended_action']
        self.assertIsNotNone(action)

    def test_missed_review_names_the_next_scheduled_grand_test_when_entitled(self):
        later = Test.objects.create(
            title='GT6M Grand II', exam_type='grand', is_pro=True, price=1000, max_attempts=1, is_draft=False,
            scheduled_start=timezone.now() + timezone.timedelta(days=3),
            scheduled_end=timezone.now() + timezone.timedelta(days=3, hours=2),
        )
        later.assigned_students.set([self.missed_student])
        _grant_access(self.missed_student, later)

        resp = self.client.get(f'/api/tests/{self.test.id}/missed-review/')
        next_grand_test = resp.data['motivation']['next_grand_test']
        self.assertIsNotNone(next_grand_test)
        self.assertEqual(next_grand_test['test_id'], later.id)


class GrandTestSeriesAnalyticsTests(APITestCase):
    """Score trend / attendance / personal best across a student's own
    Grand Test entitlements — never counting a missed test as a
    zero-score attempt."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt6s_a', email='gt6s_a@example.com', password='pw12345')
        self.subject = Subject.objects.create(name='GT6S Subject')
        self.q, self.qc, self.qw = _mkq(self.subject)
        self.client.force_authenticate(user=self.student)

    def _mk_completed(self, title, days_ago, score, total_marks_questions=1):
        now = timezone.now()
        test = Test.objects.create(
            title=title, exam_type='grand', is_pro=True, price=1000, max_attempts=1, is_draft=False,
            scheduled_start=now - timezone.timedelta(days=days_ago, hours=2),
            scheduled_end=now - timezone.timedelta(days=days_ago),
        )
        test.assigned_students.set([self.student])
        TestQuestion.objects.create(test=test, question=self.q, order=0)
        TestAttempt.objects.create(user=self.student, test=test, status='submitted', score=score)
        _grant_access(self.student, test)
        return test

    def _mk_missed(self, title, days_ago):
        now = timezone.now()
        test = Test.objects.create(
            title=title, exam_type='grand', is_pro=True, price=1000, max_attempts=1, is_draft=False,
            scheduled_start=now - timezone.timedelta(days=days_ago, hours=2),
            scheduled_end=now - timezone.timedelta(days=days_ago),
        )
        test.assigned_students.set([self.student])
        TestQuestion.objects.create(test=test, question=self.q, order=0)
        _grant_access(self.student, test)
        return test

    def _mk_upcoming(self, title):
        now = timezone.now()
        test = Test.objects.create(
            title=title, exam_type='grand', is_pro=True, price=1000, max_attempts=1, is_draft=False,
            scheduled_start=now + timezone.timedelta(days=1), scheduled_end=now + timezone.timedelta(days=1, hours=2),
        )
        test.assigned_students.set([self.student])
        TestQuestion.objects.create(test=test, question=self.q, order=0)
        _grant_access(self.student, test)
        return test

    def test_improving_trend(self):
        self._mk_completed('GT6S-1', days_ago=10, score=0)   # 0%
        self._mk_completed('GT6S-2', days_ago=5, score=1)    # 100%
        summary = grand_test_series_summary(self.student)
        self.assertEqual(summary['trend'], 'improving')
        self.assertEqual(summary['completed_count'], 2)
        self.assertEqual(summary['best_score_percentage'], 100.0)
        self.assertEqual(summary['latest_score_percentage'], 100.0)

    def test_missed_test_never_counted_as_a_zero_score(self):
        self._mk_completed('GT6S-3', days_ago=10, score=1)  # 100%
        self._mk_missed('GT6S-4', days_ago=5)
        summary = grand_test_series_summary(self.student)
        self.assertEqual(summary['completed_count'], 1)
        self.assertEqual(summary['missed_count'], 1)
        # Average must stay 100 — the missed test must never drag it down
        # to 50 as a fabricated zero.
        self.assertEqual(summary['average_score_percentage'], 100.0)

    def test_attendance_percentage_excludes_upcoming_tests(self):
        self._mk_completed('GT6S-5', days_ago=10, score=1)
        self._mk_missed('GT6S-6', days_ago=5)
        self._mk_upcoming('GT6S-7')
        summary = grand_test_series_summary(self.student)
        self.assertEqual(summary['upcoming_count'], 1)
        # attendance = completed / (completed + missed) = 1/2 = 50%
        self.assertEqual(summary['attendance_percentage'], 50.0)

    def test_series_endpoint_returns_the_same_summary(self):
        self._mk_completed('GT6S-8', days_ago=1, score=1)
        resp = self.client.get('/api/tests/grand-series/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['completed_count'], 1)

    def test_insufficient_data_trend_for_a_single_completed_test(self):
        self._mk_completed('GT6S-9', days_ago=1, score=1)
        summary = grand_test_series_summary(self.student)
        self.assertEqual(summary['trend'], 'insufficient_data')
