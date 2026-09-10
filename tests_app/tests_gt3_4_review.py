"""Grand Test 3.0 / GT3-4 — Post-Exam Review, Delayed Solution Release &
Configurable Review Expiration.

Core rules under test:

1. Absolute solution-release rule: correct answer/solution/explanation are
   never visible before the exam's own scheduled window closes, even for
   a student who already submitted (tests_app.entitlements.services.
   can_view_solutions, GT3-4 fix to its Test.scheduled_end fallback path).
2. Result OVERVIEW (score/rank/percentile/accuracy) is permanent; the
   DETAILED per-question review can independently expire
   (can_view_detailed_review / TestResultSerializer.get_questions()).
3. A missed student gets a structurally distinct Missed-Exam Review
   (GrandTestMissedReviewView) — never a TestAttempt, never a student
   answer, never a score/rank/percentile.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from academics.models import Option, Question, Subject, Chapter, Topic
from billing import payment_service
from billing.models import Purchase
from tests_app.lifecycle import grand_test_review_window
from tests_app.models import Test, TestAttempt, TestQuestion

User = get_user_model()


def _mkquestion(subject, chapter=None, topic=None):
    q = Question.objects.create(subject=subject, chapter=chapter, topic=topic, text='Q?', marks=1, negative_marks=0, explanation='Because X.')
    Option.objects.create(question=q, text='Correct', is_correct=True, order=0, explanation='Right because...')
    Option.objects.create(question=q, text='Wrong', is_correct=False, order=1, explanation='Wrong because...')
    return q


def _grant_grand_test_access(user, test, price=1000):
    purchase = Purchase.objects.create(
        user=user, kind='grand_test', grand_test=test, original_amount=price, final_amount=price, status='pending',
    )
    with patch('billing.payment_service._send_grand_test_email'):
        payment_service.activate(purchase.id)
    from billing.access import get_grand_test_access

    return get_grand_test_access(user, test)


class SolutionReleaseTests(APITestCase):
    """The absolute rule: no correct answer/solution/explanation before
    scheduled_end, even for an early submitter."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt4_a', email='gt4_a@example.com', password='pw')
        self.subject = Subject.objects.create(name='GT3-4 Subject')
        self.chapter = Chapter.objects.create(subject=self.subject, name='Chapter 1')
        self.topic = Topic.objects.create(chapter=self.chapter, name='Topic 1')
        self.question = _mkquestion(self.subject, self.chapter, self.topic)
        self.client.force_authenticate(user=self.student)

    def _mktest(self, start, end, review_duration_days=30):
        test = Test.objects.create(
            title='Grand', exam_type='grand', is_pro=True, price=1000, max_attempts=1, is_draft=False,
            scheduled_start=start, scheduled_end=end, review_duration_days=review_duration_days,
        )
        test.assigned_students.set([self.student])
        TestQuestion.objects.create(test=test, question=self.question, order=0)
        return test

    def test_early_submitter_cannot_see_correct_answer_before_close(self):
        """The mandatory §37 scenario: start, submit early, request review
        before close — question/own-answer allowed, correct answer/
        solution/explanation denied."""
        now = timezone.now()
        test = self._mktest(now - timezone.timedelta(hours=2), now + timezone.timedelta(hours=1))
        _grant_grand_test_access(self.student, test)

        start_resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(start_resp.status_code, status.HTTP_201_CREATED, start_resp.data)
        attempt_id = start_resp.data['id']

        correct_option = Option.objects.get(question=self.question, is_correct=True)
        self.client.post(f'/api/attempts/{attempt_id}/answer/', {'question_id': self.question.id, 'option_id': correct_option.id})
        submit_resp = self.client.post(f'/api/attempts/{attempt_id}/submit/')
        self.assertEqual(submit_resp.status_code, 200, submit_resp.data)

        result = self.client.get(f'/api/attempts/{attempt_id}/result/')
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data['review_status'], 'locked')
        self.assertFalse(result.data['can_view_solutions'])
        q = result.data['questions'][0]
        self.assertEqual(q['selected_option_id'], correct_option.id)  # own answer IS visible
        self.assertNotIn('is_correct', q)  # correctness stripped
        for opt in q['options']:
            self.assertNotIn('is_correct', opt)
            self.assertNotIn('explanation', opt)
        self.assertNotIn('explanation', q)

    def test_solutions_become_available_after_close(self):
        now = timezone.now()
        test = self._mktest(now - timezone.timedelta(hours=2), now + timezone.timedelta(seconds=30))
        _grant_grand_test_access(self.student, test)
        start_resp = self.client.post(f'/api/tests/{test.id}/start/')
        attempt_id = start_resp.data['id']
        correct_option = Option.objects.get(question=self.question, is_correct=True)
        self.client.post(f'/api/attempts/{attempt_id}/answer/', {'question_id': self.question.id, 'option_id': correct_option.id})
        self.client.post(f'/api/attempts/{attempt_id}/submit/')

        # Move the exam's own scheduled_end into the past — the window has closed.
        test.scheduled_end = timezone.now() - timezone.timedelta(seconds=1)
        test.save(update_fields=['scheduled_end'])

        result = self.client.get(f'/api/attempts/{attempt_id}/result/')
        self.assertEqual(result.data['review_status'], 'available')
        self.assertTrue(result.data['can_view_solutions'])
        q = result.data['questions'][0]
        self.assertIn('is_correct', q)
        self.assertTrue(any(opt.get('is_correct') for opt in q['options']))

    def test_boundary_10_59_59_locked_11_00_00_available(self):
        now = timezone.now()
        test = self._mktest(now - timezone.timedelta(hours=2), now + timezone.timedelta(seconds=30))
        _grant_grand_test_access(self.student, test)
        start_resp = self.client.post(f'/api/tests/{test.id}/start/')
        attempt_id = start_resp.data['id']
        self.client.post(f'/api/attempts/{attempt_id}/submit/')

        locked = self.client.get(f'/api/attempts/{attempt_id}/result/')
        self.assertEqual(locked.data['review_status'], 'locked')

        test.scheduled_end = timezone.now()
        test.save(update_fields=['scheduled_end'])
        available = self.client.get(f'/api/attempts/{attempt_id}/result/')
        self.assertEqual(available.data['review_status'], 'available')

    def test_solutions_available_at_and_review_expires_at_exposed(self):
        now = timezone.now()
        end = now + timezone.timedelta(hours=1)
        test = self._mktest(now - timezone.timedelta(hours=2), end, review_duration_days=30)
        _grant_grand_test_access(self.student, test)
        start_resp = self.client.post(f'/api/tests/{test.id}/start/')
        attempt_id = start_resp.data['id']
        self.client.post(f'/api/attempts/{attempt_id}/submit/')

        result = self.client.get(f'/api/attempts/{attempt_id}/result/')
        self.assertIsNotNone(result.data['solutions_available_at'])
        self.assertIsNotNone(result.data['review_expires_at'])


class ReviewExpiryTests(APITestCase):
    """Detailed review can expire; result OVERVIEW never does."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt4_b', email='gt4_b@example.com', password='pw')
        self.subject = Subject.objects.create(name='GT3-4 Expiry Subject')
        self.question = _mkquestion(self.subject)
        self.client.force_authenticate(user=self.student)

    def _mktest_and_submit(self, review_duration_days):
        now = timezone.now()
        test = Test.objects.create(
            title='Grand', exam_type='grand', is_pro=True, price=1000, max_attempts=1, is_draft=False,
            scheduled_start=now - timezone.timedelta(hours=2), scheduled_end=now - timezone.timedelta(hours=1),
            review_duration_days=review_duration_days,
        )
        test.assigned_students.set([self.student])
        TestQuestion.objects.create(test=test, question=self.question, order=0)
        _grant_grand_test_access(self.student, test)
        attempt = TestAttempt.objects.create(
            user=self.student, test=test, start_time=now - timezone.timedelta(hours=2, minutes=30), status='submitted', score=1,
        )
        return test, attempt

    def test_detailed_review_expires_but_overview_remains(self):
        test, attempt = self._mktest_and_submit(review_duration_days=7)
        # Push scheduled_end far enough into the past that +7 days has already elapsed.
        test.scheduled_end = timezone.now() - timezone.timedelta(days=8)
        test.save(update_fields=['scheduled_end'])

        result = self.client.get(f'/api/attempts/{attempt.id}/result/')
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data['review_status'], 'expired')
        self.assertEqual(result.data['questions'], [])
        # Overview permanently available regardless of expiry:
        self.assertEqual(float(result.data['score']), 1.0)
        self.assertIn('rank', result.data)
        self.assertIn('percentile', result.data)
        self.assertIn('accuracy', result.data)

    def test_permanent_review_never_expires(self):
        test, attempt = self._mktest_and_submit(review_duration_days=None)
        test.scheduled_end = timezone.now() - timezone.timedelta(days=3650)  # 10 years ago
        test.save(update_fields=['scheduled_end'])

        result = self.client.get(f'/api/attempts/{attempt.id}/result/')
        self.assertEqual(result.data['review_status'], 'available')
        self.assertEqual(len(result.data['questions']), 1)
        self.assertIsNone(result.data['review_expires_at'])

    def test_various_durations_7_30_90(self):
        for days in (7, 30, 90):
            with self.subTest(days=days):
                test, attempt = self._mktest_and_submit(review_duration_days=days)
                # Just before expiry: still available.
                test.scheduled_end = timezone.now() - timezone.timedelta(days=days - 1)
                test.save(update_fields=['scheduled_end'])
                still_available = self.client.get(f'/api/attempts/{attempt.id}/result/')
                self.assertEqual(still_available.data['review_status'], 'available')

                # Just after expiry: expired.
                test.scheduled_end = timezone.now() - timezone.timedelta(days=days + 1)
                test.save(update_fields=['scheduled_end'])
                expired = self.client.get(f'/api/attempts/{attempt.id}/result/')
                self.assertEqual(expired.data['review_status'], 'expired')


class MissedReviewTests(APITestCase):
    """The genuinely new endpoint — GET /tests/{id}/missed-review/."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt4_c', email='gt4_c@example.com', password='pw')
        self.subject = Subject.objects.create(name='GT3-4 Missed Subject')
        self.question = _mkquestion(self.subject)
        self.client.force_authenticate(user=self.student)

    def _mkmissed_test(self, review_duration_days=30, end_offset_hours=1):
        now = timezone.now()
        test = Test.objects.create(
            title='Grand', exam_type='grand', is_pro=True, price=1000, max_attempts=1, is_draft=False,
            scheduled_start=now - timezone.timedelta(hours=3), scheduled_end=now - timezone.timedelta(hours=end_offset_hours),
            review_duration_days=review_duration_days,
        )
        test.assigned_students.set([self.student])
        TestQuestion.objects.create(test=test, question=self.question, order=0)
        return test

    def test_missed_review_available_after_close_no_attempt_no_score(self):
        test = self._mkmissed_test()
        _grant_grand_test_access(self.student, test)

        resp = self.client.get(f'/api/tests/{test.id}/missed-review/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['review_type'], 'missed_review')
        self.assertEqual(resp.data['review_status'], 'available')
        # ABSOLUTE architectural rule: no TestAttempt, ever.
        self.assertFalse(TestAttempt.objects.filter(test=test, user=self.student).exists())
        q = resp.data['questions'][0]
        self.assertIn('explanation', q)  # correct answer/solution ARE shown (after close)
        self.assertTrue(any(opt.get('is_correct') for opt in q['options']))
        # Structurally absent — not merely null:
        self.assertNotIn('selected_option_id', q)
        self.assertNotIn('is_correct', q)
        self.assertNotIn('score', resp.data)
        self.assertNotIn('rank', resp.data)
        self.assertNotIn('percentile', resp.data)
        self.assertNotIn('attempt_time', resp.data)
        self.assertNotIn('start_time', resp.data)

    def test_missed_review_denied_without_entitlement(self):
        test = self._mkmissed_test()
        resp = self.client.get(f'/api/tests/{test.id}/missed-review/')
        self.assertEqual(resp.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertEqual(resp.data['code'], 'purchase_required')

    def test_missed_review_denied_while_still_upcoming_or_live(self):
        now = timezone.now()
        upcoming = Test.objects.create(
            title='Upcoming', exam_type='grand', is_pro=True, price=1000, is_draft=False,
            scheduled_start=now + timezone.timedelta(hours=1), scheduled_end=now + timezone.timedelta(hours=4),
        )
        upcoming.assigned_students.set([self.student])
        _grant_grand_test_access(self.student, upcoming)
        resp = self.client.get(f'/api/tests/{upcoming.id}/missed-review/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data['code'], 'review_locked')

    def test_missed_review_denied_for_a_student_who_actually_appeared(self):
        test = self._mkmissed_test()
        _grant_grand_test_access(self.student, test)
        TestAttempt.objects.create(user=self.student, test=test, status='submitted', score=2)
        resp = self.client.get(f'/api/tests/{test.id}/missed-review/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data['code'], 'not_missed')

    def test_missed_review_expires_but_missed_status_itself_remains_derivable(self):
        from tests_app.lifecycle import grand_test_participation_status

        test = self._mkmissed_test(review_duration_days=7, end_offset_hours=24 * 8)  # ended 8 days ago
        _grant_grand_test_access(self.student, test)

        resp = self.client.get(f'/api/tests/{test.id}/missed-review/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['review_status'], 'expired')
        self.assertEqual(resp.data['questions'], [])
        # The MISSED fact itself is unaffected by review expiry — still derivable.
        self.assertEqual(grand_test_participation_status(test, self.student), 'missed')

    def test_one_grand_test_missed_review_does_not_unlock_another(self):
        """§29 — independent entitlement check per Grand Test, even for the
        same student, even when both are missed."""
        test_a = self._mkmissed_test()
        test_b = self._mkmissed_test()
        _grant_grand_test_access(self.student, test_a)  # entitled to A only

        resp_a = self.client.get(f'/api/tests/{test_a.id}/missed-review/')
        self.assertEqual(resp_a.status_code, 200)
        resp_b = self.client.get(f'/api/tests/{test_b.id}/missed-review/')
        self.assertEqual(resp_b.status_code, status.HTTP_402_PAYMENT_REQUIRED)


class ReviewIDORTests(APITestCase):
    """§28/§48 — modified IDs must not leak another student's review."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt4_d', email='gt4_d@example.com', password='pw')
        self.other = User.objects.create_user(username='gt4_other', email='gt4_other@example.com', password='pw')
        self.subject = Subject.objects.create(name='GT3-4 IDOR Subject')
        self.question = _mkquestion(self.subject)

    def test_cannot_read_another_students_appeared_result_by_attempt_id(self):
        now = timezone.now()
        test = Test.objects.create(
            title='Grand', exam_type='grand', is_pro=True, price=1000, is_draft=False,
            scheduled_start=now - timezone.timedelta(hours=2), scheduled_end=now - timezone.timedelta(hours=1),
        )
        test.assigned_students.set([self.student, self.other])
        TestQuestion.objects.create(test=test, question=self.question, order=0)
        attempt = TestAttempt.objects.create(user=self.other, test=test, status='submitted', score=1)

        self.client.force_authenticate(user=self.student)
        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')
        # This view's get_object_or_404(..., user=request.user) is the
        # existing, established ownership-scoping pattern across this
        # entire file (SubmitAnswerView/_start_attempt/etc.) — a non-owner
        # gets 404, never 403, so a guessed attempt id can't even be
        # confirmed to exist. Confirms GT3-4 didn't weaken it.
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_cannot_read_another_students_missed_review_by_test_id_alone(self):
        """Student A has no entitlement of their own; the Test's id being
        guessable must not be enough — this is already proven by
        test_missed_review_denied_without_entitlement above, restated here
        explicitly as the IDOR-framed case: knowing test_id is not
        equivalent to owning access to it."""
        now = timezone.now()
        test = Test.objects.create(
            title='Grand', exam_type='grand', is_pro=True, price=1000, is_draft=False,
            scheduled_start=now - timezone.timedelta(hours=3), scheduled_end=now - timezone.timedelta(hours=1),
        )
        test.assigned_students.set([self.student, self.other])
        _grant_grand_test_access(self.other, test)  # only `other` is entitled

        self.client.force_authenticate(user=self.student)
        resp = self.client.get(f'/api/tests/{test.id}/missed-review/')
        self.assertEqual(resp.status_code, status.HTTP_402_PAYMENT_REQUIRED)


class NonGrandTestUnaffectedTests(APITestCase):
    """§49 — Daily/Mock/PYQ solution-release/review behavior must be
    completely unchanged by GT3-4."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt4_e', email='gt4_e@example.com', password='pw')
        self.subject = Subject.objects.create(name='GT3-4 Other Subject')
        self.question = _mkquestion(self.subject)
        self.client.force_authenticate(user=self.student)

    def test_daily_test_auto_solutions_release_immediately_on_submit_unchanged(self):
        test = Test.objects.create(title='Daily', exam_type='daily', is_pro=False, is_draft=False, max_attempts=5)
        test.assigned_students.set([self.student])
        TestQuestion.objects.create(test=test, question=self.question, order=0)

        start_resp = self.client.post(f'/api/tests/{test.id}/start/')
        attempt_id = start_resp.data['id']
        self.client.post(f'/api/attempts/{attempt_id}/submit/')

        result = self.client.get(f'/api/attempts/{attempt_id}/result/')
        self.assertTrue(result.data['can_view_solutions'])  # immediate, exactly as before GT3-4
        self.assertIsNone(result.data['review_status'])  # not a Grand Test — no review-lifecycle concept
        self.assertIsNone(result.data['solutions_available_at'])
        self.assertIsNone(result.data['review_expires_at'])
