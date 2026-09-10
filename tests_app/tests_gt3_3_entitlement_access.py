"""Grand Test 3.0 / GT3-3 — Entitlement-Based Grand Test Access.

Core rule under test: for a student who already holds a valid, non-revoked
billing.GrandTestAccess, the per-student GrandTestAccess.password is no
longer part of authorization at all — access is
`authenticated user + valid GrandTestAccess + valid schedule` (the
schedule half being GT3-2's already-tested UPCOMING/LIVE/MISSED machinery,
untouched and reused here, never re-implemented).

The password FIELD, generator, and confirmation email are all
deliberately left in place (no destructive migration) — these tests prove
the CHECK is gone, not that the DATA is gone; a separate test confirms the
column and email mechanism still exist untouched.

The free-starter path's Test.access_password gate (a different,
Test-level, admin-set password for a student with no paid entitlement at
all) is a completely separate mechanism and is explicitly NOT touched by
GT3-3 — one test below proves it still works exactly as before.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from academics.models import Option, Question, Subject
from billing import payment_service
from billing.access import get_grand_test_access
from billing.models import GrandTestAccess, Purchase
from tests_app.models import Test, TestAttempt, TestQuestion

User = get_user_model()


def _mkquestion(subject):
    q = Question.objects.create(subject=subject, text='Q?', marks=1, negative_marks=0)
    Option.objects.create(question=q, text='Correct', is_correct=True, order=0)
    Option.objects.create(question=q, text='Wrong', is_correct=False, order=1)
    return q


def _grant_grand_test_access(user, test, price=1000):
    """Real purchase → real entitlement, through the actual, unmodified
    payment_service flow — not a hand-built GrandTestAccess row — so
    these tests exercise the same code path production traffic does.
    _send_grand_test_email is mocked purely to avoid an actual SMTP call
    in a test process; the email TEXT/behavior itself is covered by its
    own dedicated test below."""
    purchase = Purchase.objects.create(
        user=user, kind='grand_test', grand_test=test, original_amount=price, final_amount=price, status='pending',
    )
    with patch('billing.payment_service._send_grand_test_email'):
        payment_service.activate(purchase.id)
    return get_grand_test_access(user, test)


class EntitledAccessNoPasswordTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='gt3_a', email='gt3_a@example.com', password='pw')
        self.subject = Subject.objects.create(name='GT3-3 Subject')
        self.question = _mkquestion(self.subject)
        self.client.force_authenticate(user=self.student)

    def _mklive_grand_test(self, price=1000):
        now = timezone.now()
        test = Test.objects.create(
            title='Grand', exam_type='grand', is_pro=True, price=price, max_attempts=1, is_draft=False,
            scheduled_start=now - timezone.timedelta(minutes=30), scheduled_end=now + timezone.timedelta(hours=2),
        )
        test.assigned_students.set([self.student])
        TestQuestion.objects.create(test=test, question=self.question, order=0)
        return test

    # --- Core: entitled + live → start without password ---
    def test_entitled_student_starts_live_grand_test_with_no_password_sent(self):
        test = self._mklive_grand_test()
        access = _grant_grand_test_access(self.student, test)
        self.assertTrue(access.password)  # a real password WAS generated and stored

        resp = self.client.post(f'/api/tests/{test.id}/start/')  # no access_password field at all
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertTrue(TestAttempt.objects.filter(test=test, user=self.student).exists())

    def test_entitled_student_starts_even_with_a_wrong_password_submitted(self):
        """A stale Frontend build, or a student who still types the old
        emailed value, must not be REJECTED for it either — the field is
        simply not consulted anymore for this population, in either
        direction."""
        test = self._mklive_grand_test()
        _grant_grand_test_access(self.student, test)

        resp = self.client.post(f'/api/tests/{test.id}/start/', {'access_password': 'DEFINITELY-WRONG'})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

    def test_requires_password_field_is_false_for_an_entitled_grand_test_student(self):
        test = self._mklive_grand_test()
        _grant_grand_test_access(self.student, test)
        resp = self.client.get(f'/api/tests/{test.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['requires_password'])

    # --- Non-entitled user ---
    def test_non_entitled_student_cannot_start_even_knowing_nothing(self):
        test = self._mklive_grand_test()
        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertEqual(resp.data['code'], 'purchase_required')
        self.assertFalse(TestAttempt.objects.filter(test=test, user=self.student).exists())

    def test_non_entitled_student_cannot_start_by_knowing_another_students_password(self):
        """Password sharing must not create a bypass — a non-entitled
        student who somehow learns another student's per-student password
        still cannot start; entitlement, not the password, is what
        matters, and this student has none."""
        test = self._mklive_grand_test()
        other = User.objects.create_user(username='gt3_owner', email='gt3_owner@example.com', password='pw')
        access = _grant_grand_test_access(other, test)

        resp = self.client.post(f'/api/tests/{test.id}/start/', {'access_password': access.password})
        self.assertEqual(resp.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertEqual(resp.data['code'], 'purchase_required')
        self.assertFalse(TestAttempt.objects.filter(test=test, user=self.student).exists())
        self.assertFalse(TestAttempt.objects.filter(test=test, user=other).exists())  # other student untouched

    def test_requires_password_field_is_true_for_a_not_yet_entitled_pro_grand_test_student(self):
        test = self._mklive_grand_test()
        resp = self.client.get(f'/api/tests/{test.id}/')
        self.assertTrue(resp.data['requires_password'])

    # --- Revoked access ---
    def test_revoked_access_cannot_start(self):
        test = self._mklive_grand_test()
        access = _grant_grand_test_access(self.student, test)
        access.revoked_at = timezone.now()
        access.save(update_fields=['revoked_at'])

        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertFalse(TestAttempt.objects.filter(test=test, user=self.student).exists())

    def test_refunded_purchase_revokes_access_and_blocks_start(self):
        """End-to-end through the real refund flow, not a hand-set
        revoked_at — confirms GT3-3 didn't accidentally bypass the
        existing, already-tested refund→revoke mechanism (billing.
        tests_phase9.py's own test_grand_test_refund_revokes_access)."""
        test = self._mklive_grand_test()
        purchase = Purchase.objects.create(
            user=self.student, kind='grand_test', grand_test=test,
            original_amount=1000, final_amount=1000, status='pending',
        )
        with patch('billing.payment_service._send_grand_test_email'):
            payment_service.activate(purchase.id)
        payment_service.refund(purchase.id, 'Test refund')

        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertFalse(TestAttempt.objects.filter(test=test, user=self.student).exists())

    # --- GT3-2 schedule states, unaffected by GT3-3 ---
    def test_entitled_but_upcoming_cannot_start(self):
        now = timezone.now()
        test = Test.objects.create(
            title='Grand', exam_type='grand', is_pro=True, price=1000, max_attempts=1, is_draft=False,
            scheduled_start=now + timezone.timedelta(hours=1), scheduled_end=now + timezone.timedelta(hours=4),
        )
        test.assigned_students.set([self.student])
        TestQuestion.objects.create(test=test, question=self.question, order=0)
        _grant_grand_test_access(self.student, test)

        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data['code'], 'grand_test_not_started')
        self.assertFalse(TestAttempt.objects.filter(test=test, user=self.student).exists())

    def test_entitled_but_missed_cannot_start(self):
        now = timezone.now()
        test = Test.objects.create(
            title='Grand', exam_type='grand', is_pro=True, price=1000, max_attempts=1, is_draft=False,
            scheduled_start=now - timezone.timedelta(hours=3), scheduled_end=now - timezone.timedelta(hours=1),
        )
        test.assigned_students.set([self.student])
        TestQuestion.objects.create(test=test, question=self.question, order=0)
        _grant_grand_test_access(self.student, test)

        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data['code'], 'grand_test_missed')
        self.assertFalse(TestAttempt.objects.filter(test=test, user=self.student).exists())

    def test_entitled_student_with_existing_in_progress_attempt_can_resume(self):
        test = self._mklive_grand_test()
        _grant_grand_test_access(self.student, test)
        first = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)

        resumed = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resumed.status_code, status.HTTP_200_OK)
        self.assertEqual(resumed.data['id'], first.data['id'])
        self.assertEqual(TestAttempt.objects.filter(test=test, user=self.student).count(), 1)

    # --- IDOR ---
    def test_student_cannot_read_another_students_grand_test_access(self):
        test = self._mklive_grand_test()
        other = User.objects.create_user(username='gt3_idor_owner', email='gt3_idor_owner@example.com', password='pw')
        _grant_grand_test_access(other, test)

        resp = self.client.get('/api/my-subscriptions/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['grand_test_access'], [])  # this student has none of their own

    def test_grand_test_access_response_no_longer_includes_password(self):
        test = self._mklive_grand_test()
        _grant_grand_test_access(self.student, test)
        resp = self.client.get('/api/my-subscriptions/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['grand_test_access']), 1)
        self.assertNotIn('password', resp.data['grand_test_access'][0])


class FreeStarterPasswordUnaffectedTests(APITestCase):
    """GT3-3 explicitly does not touch the free-starter/Test.access_password
    mechanism — a completely different population and password field."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt3_fs', email='gt3_fs@example.com', password='pw')
        self.subject = Subject.objects.create(name='GT3-3 FS Subject')
        self.question = _mkquestion(self.subject)
        self.client.force_authenticate(user=self.student)

    def test_non_entitled_student_still_gated_by_test_level_access_password_on_free_starter_path(self):
        from entitlements.models import FreeStarterPolicy

        FreeStarterPolicy.objects.update_or_create(
            resource_type='grand_test', defaults={'quantity': 5, 'unlimited': False, 'is_active': True},
        )
        now = timezone.now()
        test = Test.objects.create(
            title='Grand FS', exam_type='grand', is_pro=True, price=1000, max_attempts=5, is_draft=False,
            access_password='SHARED123', scheduled_start=now - timezone.timedelta(minutes=5),
            scheduled_end=now + timezone.timedelta(hours=2),
        )
        test.assigned_students.set([self.student])
        TestQuestion.objects.create(test=test, question=self.question, order=0)

        wrong = self.client.post(f'/api/tests/{test.id}/start/', {'access_password': 'nope'})
        self.assertEqual(wrong.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(wrong.data['code'], 'invalid_test_password')

        right = self.client.post(f'/api/tests/{test.id}/start/', {'access_password': 'SHARED123'})
        self.assertEqual(right.status_code, status.HTTP_201_CREATED, right.data)


class OtherExamTypesPasswordUnaffectedTests(APITestCase):
    """Existing password behavior for non-Grand-Test exams must be
    completely unchanged by GT3-3."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt3_other', email='gt3_other@example.com', password='pw')
        self.subject = Subject.objects.create(name='GT3-3 Other Subject')
        self.question = _mkquestion(self.subject)
        self.client.force_authenticate(user=self.student)

    def test_daily_test_password_gate_unchanged(self):
        test = Test.objects.create(
            title='Daily', exam_type='daily', is_pro=False, access_password='DAILY-PW', max_attempts=5, is_draft=False,
        )
        test.assigned_students.set([self.student])
        TestQuestion.objects.create(test=test, question=self.question, order=0)

        wrong = self.client.post(f'/api/tests/{test.id}/start/', {'access_password': 'nope'})
        self.assertEqual(wrong.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(wrong.data['code'], 'invalid_test_password')

        right = self.client.post(f'/api/tests/{test.id}/start/', {'access_password': 'DAILY-PW'})
        self.assertEqual(right.status_code, status.HTTP_201_CREATED, right.data)

    def test_requires_password_unchanged_for_non_grand_test(self):
        test = Test.objects.create(
            title='Daily', exam_type='daily', is_pro=False, access_password='X', is_draft=False,
        )
        test.assigned_students.set([self.student])
        self.client.force_authenticate(user=self.student)
        resp = self.client.get(f'/api/tests/{test.id}/')
        self.assertTrue(resp.data['requires_password'])

        no_pw_test = Test.objects.create(title='Daily 2', exam_type='daily', is_pro=False, is_draft=False)
        no_pw_test.assigned_students.set([self.student])
        resp2 = self.client.get(f'/api/tests/{no_pw_test.id}/')
        self.assertFalse(resp2.data['requires_password'])


class PasswordDataPreservedTests(TestCase):
    """Confirms GT3-3 removed the CHECK, not the DATA — no destructive
    migration, generator and email mechanism both still function exactly
    as before."""

    def setUp(self):
        self.student = User.objects.create_user(username='gt3_data', email='gt3_data@example.com', password='pw')

    def test_password_is_still_generated_and_stored_on_a_new_grant(self):
        test = Test.objects.create(title='Grand', exam_type='grand', is_pro=True, price=500)
        purchase = Purchase.objects.create(
            user=self.student, kind='grand_test', grand_test=test, original_amount=500, final_amount=500, status='pending',
        )
        with patch('billing.payment_service._send_grand_test_email'):
            payment_service.activate(purchase.id)
        access = GrandTestAccess.objects.get(user=self.student, test=test)
        self.assertTrue(access.password)
        self.assertTrue(access.password.startswith('HM-'))

    def test_confirmation_email_mechanism_is_still_invoked_on_grant(self):
        """Not disabled this phase (see GT3-3 report) — still called
        exactly once per grant, unchanged."""
        test = Test.objects.create(title='Grand', exam_type='grand', is_pro=True, price=500)
        purchase = Purchase.objects.create(
            user=self.student, kind='grand_test', grand_test=test, original_amount=500, final_amount=500, status='pending',
        )
        with patch('billing.payment_service._send_grand_test_email') as mock_email:
            payment_service.activate(purchase.id)
        mock_email.assert_called_once()
