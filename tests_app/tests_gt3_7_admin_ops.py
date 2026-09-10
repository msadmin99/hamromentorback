"""Grand Test 3.0 / GT3-7 — Admin Grand Test Management, Live Monitoring,
Post-Exam Reporting, and Series Reporting.

Core rules under test:

1. Live monitor / participants / report endpoints are admin-only.
2. Aggregate counts are correct and computed without per-student queries
   blowing up (functional correctness here; §49's query-count discipline
   is verified separately by inspection of grand_test_admin.py's own
   values().annotate() usage).
3. 'Missed' in the admin report uses the exact same derived rule as
   grand_test_participation_status — never a fabricated attempt.
4. TestAdminSerializer's dangerous_edit_warning is advisory-only and
   correctly reflects whether real attempts exist.
5. The GrandTestPackage series report reuses grand_test_report per test.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from academics.models import Chapter, Option, Question, Subject, Topic
from billing import payment_service
from billing.access import get_grand_test_access
from billing.models import GrandTestPackage, Purchase
from tests_app.models import Answer, Test, TestAttempt, TestQuestion

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


class AdminMonitorFixture(APITestCase):
    """One closed Grand Test with 5 entitled students in every distinct
    state: submitted, auto-submitted, active (in_progress, not expired),
    not-started, and missed (entitled, no attempt, window closed)."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='gt7_staff', email='gt7_staff@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.subject = Subject.objects.create(name='GT7 Subject')
        self.chapter = Chapter.objects.create(subject=self.subject, name='Chapter')
        self.topic = Topic.objects.create(chapter=self.chapter, name='Topic')
        self.q1, self.q1c, self.q1w = _mkq(self.subject, self.chapter, self.topic, 'Q1')

        now = timezone.now()
        self.test = Test.objects.create(
            title='GT7 Monitor Exam', exam_type='grand', is_pro=True, price=1000, max_attempts=1, is_draft=False,
            scheduled_start=now - timezone.timedelta(hours=3), scheduled_end=now - timezone.timedelta(minutes=1),
        )
        TestQuestion.objects.create(test=self.test, question=self.q1, order=0)

        self.submitted_student = User.objects.create_user(username='gt7_sub', email='gt7_sub@example.com', password='pw12345')
        self.auto_student = User.objects.create_user(username='gt7_auto', email='gt7_auto@example.com', password='pw12345')
        self.not_started_student = User.objects.create_user(username='gt7_ns', email='gt7_ns@example.com', password='pw12345')
        self.missed_student = User.objects.create_user(username='gt7_missed', email='gt7_missed@example.com', password='pw12345')

        for u in (self.submitted_student, self.auto_student, self.not_started_student, self.missed_student):
            self.test.assigned_students.add(u)
            _grant_access(u, self.test)

        submitted_attempt = TestAttempt.objects.create(
            user=self.submitted_student, test=self.test, status='submitted', score=1, rank=1, percentile=100,
        )
        Answer.objects.create(attempt=submitted_attempt, question=self.q1, selected_option=self.q1c, is_correct=True)

        auto_attempt = TestAttempt.objects.create(
            user=self.auto_student, test=self.test, status='submitted', auto_submitted=True, score=0, rank=2, percentile=0,
        )
        Answer.objects.create(attempt=auto_attempt, question=self.q1, selected_option=self.q1w, is_correct=False)

        self.client.force_authenticate(user=self.staff)


class GrandTestMonitorTests(AdminMonitorFixture):
    def test_monitor_counts_are_correct(self):
        resp = self.client.get(f'/api/tests/{self.test.id}/grand_test_monitor/')
        self.assertEqual(resp.status_code, 200, resp.data)
        data = resp.data
        self.assertEqual(data['entitled'], 4)
        self.assertEqual(data['started'], 2)
        self.assertEqual(data['submitted'], 2)
        self.assertEqual(data['auto_submitted'], 1)
        self.assertEqual(data['active'], 0)
        self.assertEqual(data['not_started'], 2)  # not_started_student + missed_student both have zero attempts
        self.assertTrue(data['window_closed'])

    def test_monitor_denied_for_non_admin(self):
        self.client.force_authenticate(user=self.submitted_student)
        resp = self.client.get(f'/api/tests/{self.test.id}/grand_test_monitor/')
        self.assertEqual(resp.status_code, 403)

    def test_monitor_rejected_for_non_grand_test(self):
        daily = Test.objects.create(title='GT7 Daily', exam_type='daily', is_draft=False)
        resp = self.client.get(f'/api/tests/{daily.id}/grand_test_monitor/')
        self.assertEqual(resp.status_code, 400)

    def test_health_indicators_never_fabricated(self):
        """§12 — no failure/error log exists in this codebase; these must
        be null, never a made-up number."""
        resp = self.client.get(f'/api/tests/{self.test.id}/grand_test_monitor/')
        self.assertIsNone(resp.data['start_failures'])
        self.assertIsNone(resp.data['answer_save_failures'])
        self.assertIsNone(resp.data['submission_failures'])
        self.assertIsNone(resp.data['recent_api_errors'])


class GrandTestParticipantsTests(AdminMonitorFixture):
    def test_participants_include_zero_attempt_students(self):
        resp = self.client.get(f'/api/tests/{self.test.id}/grand_test_participants/')
        self.assertEqual(resp.status_code, 200, resp.data)
        rows_by_user = {r['user_id']: r for r in resp.data}
        self.assertEqual(len(rows_by_user), 4)

        self.assertEqual(rows_by_user[self.submitted_student.id]['status'], 'submitted')
        self.assertEqual(rows_by_user[self.auto_student.id]['status'], 'auto_submitted')
        self.assertEqual(rows_by_user[self.not_started_student.id]['status'], 'missed')
        self.assertEqual(rows_by_user[self.missed_student.id]['status'], 'missed')

        # Missed rows must never carry a fabricated score/rank.
        self.assertIsNone(rows_by_user[self.missed_student.id]['score'])
        self.assertIsNone(rows_by_user[self.missed_student.id]['rank'])


class GrandTestReportTests(AdminMonitorFixture):
    def test_report_participation_and_results(self):
        resp = self.client.get(f'/api/tests/{self.test.id}/grand_test_report/')
        self.assertEqual(resp.status_code, 200, resp.data)
        participation = resp.data['participation']
        self.assertEqual(participation['entitled'], 4)
        self.assertEqual(participation['appeared'], 2)
        self.assertEqual(participation['missed'], 2)
        self.assertEqual(participation['auto_submitted'], 1)

        results = resp.data['results']
        self.assertEqual(results['highest_score'], 1.0)
        self.assertEqual(results['lowest_score'], 0.0)
        self.assertEqual(results['average_score'], 0.5)
        self.assertEqual(results['median_score'], 0.5)

    def test_report_question_analytics(self):
        resp = self.client.get(f'/api/tests/{self.test.id}/grand_test_report/')
        questions = resp.data['question_analysis']['questions']
        self.assertEqual(len(questions), 1)
        q = questions[0]
        self.assertEqual(q['total_responses'], 2)
        self.assertEqual(q['correct'], 1)
        self.assertEqual(q['incorrect'], 1)
        self.assertEqual(q['accuracy'], 50.0)

    def test_report_subject_performance(self):
        resp = self.client.get(f'/api/tests/{self.test.id}/grand_test_report/')
        subjects = resp.data['performance']['subjects']
        self.assertEqual(len(subjects), 1)
        self.assertEqual(subjects[0]['id'], self.subject.id)
        self.assertEqual(subjects[0]['accuracy'], 50.0)


class TestAdminSerializerWarningTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='gt7_staff2', email='gt7_staff2@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(username='gt7_stu2', email='gt7_stu2@example.com', password='pw12345')
        self.subject = Subject.objects.create(name='GT7 Warn Subject')
        self.q, self.qc, self.qw = _mkq(self.subject)
        self.test = Test.objects.create(title='GT7 Warn Exam', exam_type='mock', is_draft=False)
        TestQuestion.objects.create(test=self.test, question=self.q)
        self.client.force_authenticate(user=self.staff)

    def test_no_warning_when_no_attempts_exist(self):
        resp = self.client.get(f'/api/tests/{self.test.id}/')
        self.assertEqual(resp.data['attempt_count'], 0)
        self.assertIsNone(resp.data['dangerous_edit_warning'])

    def test_warning_present_once_an_attempt_exists(self):
        TestAttempt.objects.create(user=self.student, test=self.test, status='submitted', score=1)
        resp = self.client.get(f'/api/tests/{self.test.id}/')
        self.assertEqual(resp.data['attempt_count'], 1)
        self.assertIsNotNone(resp.data['dangerous_edit_warning'])
        self.assertIn('1 student attempt', resp.data['dangerous_edit_warning'])

    def test_warning_is_advisory_only_edit_still_succeeds(self):
        """§8 — 'do not rely on the warning alone' also means the reverse:
        the warning must never itself block a legitimate edit."""
        TestAttempt.objects.create(user=self.student, test=self.test, status='submitted', score=1)
        resp = self.client.patch(f'/api/tests/{self.test.id}/', {'title': 'Renamed Exam'})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.test.refresh_from_db()
        self.assertEqual(self.test.title, 'Renamed Exam')


class GrandTestSeriesReportTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='gt7_staff3', email='gt7_staff3@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.subject = Subject.objects.create(name='GT7 Series Subject')
        self.q, self.qc, self.qw = _mkq(self.subject)
        self.student = User.objects.create_user(username='gt7_series_stu', email='gt7_series_stu@example.com', password='pw12345')

        now = timezone.now()
        self.test1 = Test.objects.create(
            title='GT7 Series I', exam_type='grand', is_pro=True, price=500, max_attempts=1, is_draft=False,
            scheduled_start=now - timezone.timedelta(days=2, hours=1), scheduled_end=now - timezone.timedelta(days=2),
        )
        TestQuestion.objects.create(test=self.test1, question=self.q)
        self.test1.assigned_students.add(self.student)
        _grant_access(self.student, self.test1)
        attempt = TestAttempt.objects.create(user=self.student, test=self.test1, status='submitted', score=1)
        Answer.objects.create(attempt=attempt, question=self.q, selected_option=self.qc, is_correct=True)

        self.package = GrandTestPackage.objects.create(name='GT7 Series Package', price=800)
        self.package.tests.set([self.test1])
        self.client.force_authenticate(user=self.staff)

    def test_series_report_reuses_per_test_report(self):
        resp = self.client.get(f'/api/grand-test-packages/{self.package.id}/series_report/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(len(resp.data['tests']), 1)
        self.assertEqual(resp.data['tests'][0]['test_id'], self.test1.id)
        self.assertEqual(resp.data['total_appeared'], 1)
        self.assertEqual(resp.data['overall_average_percentage'], 100.0)
