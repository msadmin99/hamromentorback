"""Grand Test 3.0 — Release Candidate: backend additions for student
frontend integration + the scheduler hook.

Covers:
1. /api/cron/finalize-expired-attempts/ — the Cloud Scheduler hook for
   the best-effort expired-attempt sweep (auth guard + it actually
   finalizes, sharing sweep_expired_attempts with the management command).
2. The card-access contract, as the /api/tests/ list endpoint actually
   returns it, now reports upcoming/missed for a Grand Test scheduled via
   the Test.scheduled_start/end fallback (previously it lied 'start').
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from academics.models import Option, Question, Subject
from billing import payment_service
from billing.models import Purchase
from courses.models import Course, Enrollment
from tests_app.models import Answer, Test, TestAttempt, TestQuestion

User = get_user_model()


def _mkq(subject):
    q = Question.objects.create(subject=subject, text='Q?', marks=1, negative_marks=0)
    Option.objects.create(question=q, text='Right', order=0, is_correct=True)
    Option.objects.create(question=q, text='Wrong', order=1, is_correct=False)
    return q


@override_settings(CRON_SECRET='rc-test-cron-secret')
class FinalizeExpiredAttemptsCronTests(APITestCase):
    URL = '/api/cron/finalize-expired-attempts/'

    def setUp(self):
        self.student = User.objects.create_user(username='rc_stu', email='rc_stu@example.com', password='pw12345')
        self.subject = Subject.objects.create(name='RC Subject')
        self.q = _mkq(self.subject)
        self.test = Test.objects.create(title='RC Exam', exam_type='mock', is_draft=False, duration_minutes=30)
        TestQuestion.objects.create(test=self.test, question=self.q)

    def _expired_attempt(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test, status='in_progress')
        Answer.objects.create(attempt=attempt, question=self.q, selected_option=self.q.options.get(is_correct=True), is_correct=True)
        TestAttempt.objects.filter(pk=attempt.pk).update(start_time=timezone.now() - timezone.timedelta(hours=2))
        return attempt

    def test_missing_secret_is_rejected(self):
        self._expired_attempt()
        resp = self.client.post(self.URL)
        self.assertEqual(resp.status_code, 401)

    def test_wrong_secret_is_rejected(self):
        resp = self.client.post(self.URL, HTTP_X_CRON_SECRET='nope')
        self.assertEqual(resp.status_code, 401)

    @override_settings(CRON_SECRET='')
    def test_unconfigured_secret_fails_closed(self):
        resp = self.client.post(self.URL, HTTP_X_CRON_SECRET='')
        self.assertEqual(resp.status_code, 401)

    def test_valid_secret_finalizes_expired_attempt(self):
        expired = self._expired_attempt()
        active = TestAttempt.objects.create(user=self.student, test=self.test, status='in_progress')

        resp = self.client.post(self.URL, HTTP_X_CRON_SECRET='rc-test-cron-secret')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['finalized'], 1)

        expired.refresh_from_db()
        active.refresh_from_db()
        self.assertEqual(expired.status, 'submitted')
        self.assertTrue(expired.auto_submitted)
        self.assertEqual(active.status, 'in_progress')

    def test_dry_run_reports_without_writing(self):
        expired = self._expired_attempt()
        resp = self.client.post(f'{self.URL}?dry_run=1', HTTP_X_CRON_SECRET='rc-test-cron-secret')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['finalized'], 1)
        self.assertTrue(resp.data['dry_run'])
        expired.refresh_from_db()
        self.assertEqual(expired.status, 'in_progress')

    def test_running_twice_is_a_safe_no_op(self):
        self._expired_attempt()
        self.client.post(self.URL, HTTP_X_CRON_SECRET='rc-test-cron-secret')
        resp = self.client.post(self.URL, HTTP_X_CRON_SECRET='rc-test-cron-secret')
        self.assertEqual(resp.data['finalized'], 0)


class GrandTestCardAccessApiTests(APITestCase):
    """The RC card-access fix as the frontend actually receives it from
    GET /api/tests/?exam_type=grand."""

    def setUp(self):
        self.student = User.objects.create_user(username='rc_gt_stu', email='rc_gt_stu@example.com', password='pw12345')
        self.course = Course.objects.create(name='RC GT Course', prefix='RCGT')
        Enrollment.objects.create(user=self.student, course=self.course, is_active=True)
        self.subject = Subject.objects.create(name='RC GT Subject', is_free=False)
        self.subject.courses.set([self.course])
        self.client.force_authenticate(user=self.student)

    def _grand(self, start, end):
        test = Test.objects.create(
            title='RC GT', exam_type='grand', is_pro=True, price=50, max_attempts=1, is_draft=False,
            scheduled_start=start, scheduled_end=end,
        )
        test.courses.set([self.course])
        TestQuestion.objects.create(test=test, question=_mkq(self.subject))
        purchase = Purchase.objects.create(
            user=self.student, kind='grand_test', grand_test=test, original_amount=50, final_amount=50, status='pending',
        )
        with patch('billing.payment_service._send_grand_test_email'):
            payment_service.activate(purchase.id)
        return test

    def _access_state(self, test_id):
        resp = self.client.get('/api/tests/?exam_type=grand')
        self.assertEqual(resp.status_code, 200)
        rows = resp.data['results'] if isinstance(resp.data, dict) else resp.data
        row = next(r for r in rows if r['id'] == test_id)
        return row['access']['state']

    def test_upcoming_fallback_scheduled_grand_test_is_not_startable(self):
        now = timezone.now()
        test = self._grand(now + timezone.timedelta(hours=2), now + timezone.timedelta(hours=5))
        self.assertEqual(self._access_state(test.id), 'upcoming')

    def test_missed_fallback_scheduled_grand_test_reports_missed(self):
        now = timezone.now()
        test = self._grand(now - timezone.timedelta(hours=5), now - timezone.timedelta(hours=1))
        self.assertEqual(self._access_state(test.id), 'missed')

    def test_live_fallback_scheduled_grand_test_is_startable(self):
        now = timezone.now()
        test = self._grand(now - timezone.timedelta(hours=1), now + timezone.timedelta(hours=2))
        self.assertEqual(self._access_state(test.id), 'start')
