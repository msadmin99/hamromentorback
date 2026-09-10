"""Grand Test 3.0 / GT3-7 — Security Final Audit + Time-Boundary Matrix.

Covers the GT3-7 §70 security regression table and the §69 mandatory
time-boundary checks, using the exact fixtures/helpers pattern already
established in tests_gt3_2_missed_exam.py / tests_gt3_4_review.py. Many
of these rules were already established and tested in GT3-2/3/4/5 — this
file is the FINAL, consolidated regression pass GT3-7 itself calls for,
not a claim that every rule here is new.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from academics.models import Option, Question, Subject
from billing import payment_service
from billing.access import get_grand_test_access
from billing.models import Purchase
from tests_app.models import Test, TestAttempt, TestQuestion

User = get_user_model()


def _mkq(subject):
    q = Question.objects.create(subject=subject, text='Q?', marks=1, negative_marks=0)
    Option.objects.create(question=q, text='Right', order=0, is_correct=True)
    Option.objects.create(question=q, text='Wrong', order=1, is_correct=False)
    return q


def _grant_access(user, test, price=1000):
    purchase = Purchase.objects.create(
        user=user, kind='grand_test', grand_test=test, original_amount=price, final_amount=price, status='pending',
    )
    with patch('billing.payment_service._send_grand_test_email'):
        payment_service.activate(purchase.id)
    return purchase, get_grand_test_access(user, test)


class IDORTests(APITestCase):
    """§70 rows 1-2 — cross-student object access must be denied, not
    merely hidden from a list."""

    def setUp(self):
        self.subject = Subject.objects.create(name='GT7 Sec Subject')
        self.q = _mkq(self.subject)
        self.test = Test.objects.create(title='GT7 Sec Exam', exam_type='mock', is_draft=False)
        TestQuestion.objects.create(test=self.test, question=self.q)
        self.owner = User.objects.create_user(username='gt7sec_owner', email='gt7sec_owner@example.com', password='pw12345')
        self.attacker = User.objects.create_user(username='gt7sec_attacker', email='gt7sec_attacker@example.com', password='pw12345')
        self.attempt = TestAttempt.objects.create(user=self.owner, test=self.test, status='submitted', score=1)
        self.client.force_authenticate(user=self.attacker)

    def test_cross_student_attempt_access_denied(self):
        resp = self.client.get(f'/api/attempts/{self.attempt.id}/')
        self.assertIn(resp.status_code, (403, 404))

    def test_cross_student_attempt_result_denied(self):
        resp = self.client.get(f'/api/attempts/{self.attempt.id}/result/')
        self.assertIn(resp.status_code, (403, 404))

    def test_cross_student_answer_save_denied(self):
        resp = self.client.post(f'/api/attempts/{self.attempt.id}/answer/', {'question_id': self.q.id})
        self.assertIn(resp.status_code, (403, 404))

    def test_cross_student_submit_denied(self):
        resp = self.client.post(f'/api/attempts/{self.attempt.id}/submit/')
        self.assertIn(resp.status_code, (403, 404))


class RefundedEntitlementSecurityTests(APITestCase):
    """§70 row: refunded entitlement -> denied, including the started-then-
    refunded case (revocation must stop a NEW start, not retroactively
    delete a result already produced)."""

    def setUp(self):
        self.subject = Subject.objects.create(name='GT7 Refund Subject')
        self.q = _mkq(self.subject)
        now = timezone.now()
        self.test = Test.objects.create(
            title='GT7 Refund Exam', exam_type='grand', is_pro=True, price=1000, max_attempts=1, is_draft=False,
            scheduled_start=now - timezone.timedelta(hours=1), scheduled_end=now + timezone.timedelta(hours=1),
        )
        TestQuestion.objects.create(test=self.test, question=self.q)
        self.student = User.objects.create_user(username='gt7_refund_stu', email='gt7_refund_stu@example.com', password='pw12345')
        self.test.assigned_students.add(self.student)
        self.client.force_authenticate(user=self.student)

    def test_refunded_entitlement_cannot_start(self):
        purchase, access = _grant_access(self.student, self.test)
        payment_service.refund(purchase.id, reason='test refund')
        access.refresh_from_db()
        self.assertIsNotNone(access.revoked_at)

        resp = self.client.post(f'/api/tests/{self.test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_402_PAYMENT_REQUIRED, resp.data)


class ClientClockHasNoEffectTests(APITestCase):
    """§70 — 'client clock changed' must have no effect: every enforcement
    decision must come from the server's own timezone.now(), never a
    client-supplied timestamp. There is no endpoint anywhere in
    tests_app that accepts a client-supplied 'now'/'client_time' field at
    all (confirmed by inspection of StartTestSerializer and every
    attempt-mutating view) — this test proves the point operationally: a
    request that tries to smuggle a spoofed timestamp into the body is
    silently ignored, not honored."""

    def test_spoofed_timestamp_in_start_payload_is_ignored(self):
        subject = Subject.objects.create(name='GT7 Clock Subject')
        q = _mkq(subject)
        now = timezone.now()
        test = Test.objects.create(
            title='GT7 Clock Exam', exam_type='grand', is_pro=True, price=1000, max_attempts=1, is_draft=False,
            scheduled_start=now + timezone.timedelta(hours=1), scheduled_end=now + timezone.timedelta(hours=3),
        )
        TestQuestion.objects.create(test=test, question=q)
        student = User.objects.create_user(username='gt7_clock_stu', email='gt7_clock_stu@example.com', password='pw12345')
        test.assigned_students.add(student)
        _grant_access(student, test)
        self.client.force_authenticate(user=student)

        # Try to claim the exam has already started via a spoofed field —
        # the server must still say 'not started yet' since the real
        # scheduled_start is an hour in the future.
        resp = self.client.post(f'/api/tests/{test.id}/start/', {'now': (now + timezone.timedelta(hours=2)).isoformat(), 'client_time': 9999999999})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data.get('code'), 'grand_test_not_started')


class ErrorMessagesDoNotLeakInternalsTests(APITestCase):
    """§51 — student-facing errors must be understandable and must never
    expose stack traces / DB errors / internal model names."""

    def test_missed_error_message_is_the_approved_student_facing_copy(self):
        subject = Subject.objects.create(name='GT7 Msg Subject')
        q = _mkq(subject)
        now = timezone.now()
        test = Test.objects.create(
            title='GT7 Msg Exam', exam_type='grand', is_pro=True, price=1000, max_attempts=1, is_draft=False,
            scheduled_start=now - timezone.timedelta(hours=3), scheduled_end=now - timezone.timedelta(hours=1),
        )
        TestQuestion.objects.create(test=test, question=q)
        student = User.objects.create_user(username='gt7_msg_stu', email='gt7_msg_stu@example.com', password='pw12345')
        test.assigned_students.add(student)
        _grant_access(student, test)
        self.client.force_authenticate(user=student)

        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn('Traceback', str(resp.data))
        self.assertNotIn('DoesNotExist', str(resp.data))
        self.assertIn('missed', resp.data.get('message', resp.data.get('detail', '')).lower())


class TimeBoundaryTests(APITestCase):
    """GT3-7 §69 — mandatory second-precision boundary checks around a
    Grand Test's scheduled_start/scheduled_end. Uses tight (well under a
    second of test-execution overhead) offsets from the real schedule
    boundary rather than literal wall-clock 07:59:59/11:00:00 — the
    relative boundary arithmetic is what's actually being verified,
    exactly as GT3-2's own tests already established (see that phase's
    'self-corrected before running' note on real-time race risk)."""

    def setUp(self):
        self.subject = Subject.objects.create(name='GT7 Boundary Subject')
        self.q = _mkq(self.subject)
        self.student = User.objects.create_user(username='gt7_boundary_stu', email='gt7_boundary_stu@example.com', password='pw12345')

    def _mktest(self, start, end):
        test = Test.objects.create(
            title='GT7 Boundary Exam', exam_type='grand', is_pro=True, price=1000, max_attempts=1, is_draft=False,
            scheduled_start=start, scheduled_end=end,
        )
        TestQuestion.objects.create(test=test, question=self.q)
        test.assigned_students.add(self.student)
        _grant_access(self.student, test)
        self.client.force_authenticate(user=self.student)
        return test

    def test_one_second_before_start_is_denied(self):
        now = timezone.now()
        test = self._mktest(now + timezone.timedelta(seconds=1), now + timezone.timedelta(hours=3))
        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data.get('code'), 'grand_test_not_started')

    def test_at_start_time_is_allowed(self):
        now = timezone.now()
        test = self._mktest(now - timezone.timedelta(seconds=1), now + timezone.timedelta(hours=3))
        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_one_second_before_end_start_is_still_allowed(self):
        now = timezone.now()
        test = self._mktest(now - timezone.timedelta(hours=1), now + timezone.timedelta(seconds=2))
        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_after_end_with_no_prior_attempt_is_missed(self):
        now = timezone.now()
        test = self._mktest(now - timezone.timedelta(hours=3), now - timezone.timedelta(seconds=1))
        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data.get('code'), 'grand_test_missed')
