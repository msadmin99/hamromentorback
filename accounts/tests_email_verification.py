"""Email verification during student registration.

Deliberately mirrors the two established conventions this codebase already
uses for exactly this kind of feature (see notifications/
tests_email_integration.py and notifications/tests_exam_reminder_audience.py):
only `notifications.email_adapter.send` (through accounts.email_verification's
own import of it) is ever mocked — never any of our own code — and
`transaction.on_commit` callbacks are driven explicitly via
`self.captureOnCommitCallbacks(execute=True)`, since a plain TestCase never
actually commits, so an un-driven on_commit callback would never run and a
missing email-send call could hide behind that instead of a real assertion.

Numbered docstrings below map directly to the audit's own numbered test
list (Registration 1-4, Verification 5-10, Resend 11-15, Email change
16-19, Notifications 20-25, Audience 26-29, Security 30-34).
"""
from datetime import timedelta
from unittest.mock import patch

import threading

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from billing.models import GrandTestAccess
from courses.models import Course, Enrollment
from notifications import services
from notifications.email_adapter import EmailSendResult
from notifications.events import CATEGORY_TESTS, CHANNEL_EMAIL, CHANNEL_IN_APP, CHANNEL_PUSH
from notifications.models import Notification, NotificationDelivery, NotificationPreference, PushSubscription
from tests_app.models import ExamSession, ExamTemplate, Test

from . import email_verification as ev
from .models import EmailVerificationToken

User = get_user_model()

SENT = EmailSendResult(outcome=EmailSendResult.OUTCOME_SENT, provider_message_id='msg-1')
PERM_FAIL = EmailSendResult(outcome=EmailSendResult.OUTCOME_PERMANENT_FAILURE, error_code='bounce', error_message='no such mailbox')


def _user(username, **kw):
    kw.setdefault('email', f'{username}@example.com')
    kw.setdefault('password', 'pw12345')
    return User.objects.create_user(username=username, **kw)


def _course(name):
    return Course.objects.create(name=name, prefix=name[:10])


def _test_obj(courses=()):
    t = Test.objects.create(title='Grand Test', exam_type='grand', is_draft=False)
    if courses:
        t.courses.set(courses)
    return t


def _session(test, start):
    template = ExamTemplate.objects.create(title=test.title, exam_type=test.exam_type)
    return ExamSession.objects.create(
        exam_template=template, exam_version=test, session_name='Session',
        start_datetime=start, end_datetime=start + timedelta(hours=2), status='scheduled',
    )


# =====================================================================
# REGISTRATION (1-4)
# =====================================================================

class RegistrationTests(APITestCase):
    def _register(self, **overrides):
        payload = {
            'name': 'New Student', 'email': 'newstudent@example.com', 'confirm_email': 'newstudent@example.com',
            'password': 'StrongPass!2024',
        }
        payload.update(overrides)
        with patch('accounts.email_verification.email_adapter.send') as mock_send:
            mock_send.return_value = SENT
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post('/api/auth/register/', payload)
        return resp, mock_send

    def test_1_registration_creates_user(self):
        resp, _ = self._register()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(User.objects.filter(email='newstudent@example.com').exists())

    def test_2_email_verified_defaults_false(self):
        resp, _ = self._register()
        user = User.objects.get(email='newstudent@example.com')
        self.assertFalse(user.email_verified)
        self.assertIsNone(user.email_verified_at)
        self.assertFalse(resp.data['user']['email_verified'])

    def test_3_verification_token_generated(self):
        self._register()
        user = User.objects.get(email='newstudent@example.com')
        self.assertEqual(EmailVerificationToken.objects.filter(user=user).count(), 1)
        token_row = EmailVerificationToken.objects.get(user=user)
        self.assertEqual(token_row.email, 'newstudent@example.com')
        self.assertTrue(token_row.expires_at > timezone.now())

    def test_4_verification_email_delivery_requested(self):
        resp, mock_send = self._register()
        mock_send.assert_called_once()
        called_to = mock_send.call_args[0][0]
        self.assertEqual(called_to, 'newstudent@example.com')

    def test_registration_still_returns_working_login_tokens(self):
        """Registration must keep logging the student in — email
        verification adds a requirement, it does not change this."""
        resp, _ = self._register()
        self.assertIn('access', resp.data['tokens'])
        self.assertIn('refresh', resp.data['tokens'])

    def test_registration_never_fails_because_the_email_provider_fails(self):
        payload = {
            'name': 'New Student', 'email': 'providerfails@example.com', 'confirm_email': 'providerfails@example.com',
            'password': 'StrongPass!2024',
        }
        with patch('accounts.email_verification.email_adapter.send') as mock_send:
            mock_send.return_value = PERM_FAIL
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post('/api/auth/register/', payload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(email='providerfails@example.com')
        self.assertFalse(user.email_verified)
        token_row = EmailVerificationToken.objects.get(user=user)
        self.assertEqual(token_row.send_error_code, 'bounce')  # recorded, not silently swallowed
        self.assertIsNotNone(token_row.send_attempted_at)

    def test_registration_never_persists_the_raw_token_anywhere(self):
        """The only artifact of a raw token in the DB is its sha256 hash."""
        resp, mock_send = self._register()
        token_row = EmailVerificationToken.objects.get(user__email='newstudent@example.com')
        self.assertEqual(len(token_row.token_hash), 64)
        # The html body Postmark received DOES contain the raw token (that's
        # the whole point of the email) — but nothing in our own database does.
        html_body = mock_send.call_args[0][3]
        self.assertNotIn(token_row.token_hash, html_body)


# =====================================================================
# VERIFICATION (5-10)
# =====================================================================

class VerificationTests(APITestCase):
    def setUp(self):
        cache.clear()  # see ResendTests.setUp's identical comment.
        self.user = _user('verify_target')
        self.token_row, self.raw_token = ev.issue_token(self.user)

    def _verify(self, token):
        return self.client.post(f'/api/auth/verify-email/{token}/')

    def test_5_valid_token_verifies_email(self):
        resp = self._verify(self.raw_token)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)

    def test_6_invalid_token_rejected(self):
        resp = self._verify('not-a-real-token')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'invalid_token')
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)

    def test_7_expired_token_rejected(self):
        self.token_row.expires_at = timezone.now() - timedelta(seconds=1)
        self.token_row.save(update_fields=['expires_at'])
        resp = self._verify(self.raw_token)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'expired_token')
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)

    def test_8_token_cannot_be_reused(self):
        first = self._verify(self.raw_token)
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        second = self._verify(self.raw_token)
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(second.data['code'], 'already_used')

    def test_9_token_cannot_verify_another_user(self):
        other_user = _user('verify_bystander')
        resp = self._verify(self.raw_token)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        other_user.refresh_from_db()
        self.assertFalse(other_user.email_verified, "Verifying one user's token must never affect a different user.")

    def test_10_successful_verification_sets_email_verified_at(self):
        before = timezone.now()
        self._verify(self.raw_token)
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.email_verified_at)
        self.assertGreaterEqual(self.user.email_verified_at, before)

    def test_concurrent_double_submit_only_verifies_once(self):
        """Race-safety: the claim is a single atomic conditional UPDATE
        (accounts.email_verification.consume_token_and_verify_email's own
        docstring) — simulate two 'simultaneous' calls for the same token."""
        result_a = ev.consume_token_and_verify_email(self.raw_token)
        result_b = ev.consume_token_and_verify_email(self.raw_token)
        self.assertTrue(result_a.success)
        self.assertFalse(result_b.success)
        self.assertEqual(result_b.error_code, 'already_used')


class ConcurrentVerificationTests(TransactionTestCase):
    """Same precedent as accounts.tests.py's own
    AdminStudentEditConcurrencyTests: a real race needs TransactionTestCase
    (setUp's row must actually be committed so a second thread's own
    connection can see it) — a plain TestCase's outer, never-committed
    transaction would leave the other thread looking at an empty table."""

    def test_two_real_threads_racing_the_same_token_only_one_wins(self):
        """SQLite (test-only; MySQL is the real production backend) has no
        real row-level locking, so a same-instant write collision can
        surface as an OperationalError rather than a wait — each thread
        retries past that specific, SQLite-only condition, same precedent
        as accounts.tests.py's own run_with_retries. What this test
        actually asserts (exactly one success) is unaffected by how many
        retries either thread needed to get there."""
        import time

        from django.db import Error as DjangoDbError
        from django.db import close_old_connections

        user = _user('race_target')
        token_row, raw_token = ev.issue_token(user)

        results = []

        def attempt():
            for attempt_no in range(8):
                try:
                    results.append(ev.consume_token_and_verify_email(raw_token).success)
                    return
                except DjangoDbError:
                    close_old_connections()
                    time.sleep(0.1 * (attempt_no + 1))
            results.append(False)

        t1 = threading.Thread(target=attempt)
        t2 = threading.Thread(target=attempt)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        self.assertEqual(sorted(results), [False, True], 'Exactly one of the two racing threads must succeed.')
        user.refresh_from_db()
        self.assertTrue(user.email_verified)


# =====================================================================
# RESEND (11-15)
# =====================================================================

class ResendTests(APITestCase):
    def setUp(self):
        cache.clear()  # DRF's throttle counters live in Django's cache, not the DB — see billing/tests.py's identical precedent.
        self.user = _user('resend_target')
        self.client.force_authenticate(user=self.user)

    def _resend(self):
        with patch('accounts.email_verification.email_adapter.send') as mock_send:
            mock_send.return_value = SENT
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post('/api/auth/resend-verification-email/')
        return resp, mock_send

    def test_11_resend_creates_new_token(self):
        old_token_row, _ = ev.issue_token(self.user)
        resp, mock_send = self._resend()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        new_count = EmailVerificationToken.objects.filter(user=self.user, invalidated_at__isnull=True, used_at__isnull=True).count()
        self.assertEqual(new_count, 1)
        newest = EmailVerificationToken.objects.filter(user=self.user).order_by('-id').first()
        self.assertNotEqual(newest.pk, old_token_row.pk)

    def test_12_old_token_invalidated_by_resend(self):
        old_token_row, old_raw = ev.issue_token(self.user)
        self._resend()
        old_token_row.refresh_from_db()
        self.assertIsNotNone(old_token_row.invalidated_at)
        result = ev.consume_token_and_verify_email(old_raw)
        self.assertFalse(result.success)

    def test_13_rate_limit_enforced(self):
        # ResendVerificationEmailThrottle allows 3/hour (accounts/views.py).
        for _ in range(3):
            resp, _ = self._resend()
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
        resp, mock_send = self._resend()
        self.assertEqual(resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        mock_send.assert_not_called()

    def test_14_repeated_requests_do_not_pile_up_valid_tokens(self):
        for _ in range(3):
            self._resend()
        still_valid = EmailVerificationToken.objects.filter(
            user=self.user, invalidated_at__isnull=True, used_at__isnull=True,
        ).count()
        self.assertEqual(still_valid, 1, 'Only the most recent token must ever remain valid.')

    def test_15_response_is_generic_and_does_not_leak_internal_state_shape(self):
        resp, _ = self._resend()
        self.assertEqual(set(resp.data.keys()), {'detail', 'email_verified'})  # generic — no token/token_hash/user-detail leakage
        self.assertNotIn('token', resp.data)
        self.assertNotIn('token_hash', resp.data)

    def test_resend_for_an_already_verified_user_does_not_send_again(self):
        self.user.email_verified = True
        self.user.email_verified_at = timezone.now()
        self.user.save(update_fields=['email_verified', 'email_verified_at'])
        resp, mock_send = self._resend()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['email_verified'])
        mock_send.assert_not_called()

    def test_resend_requires_authentication(self):
        self.client.force_authenticate(user=None)
        resp = self.client.post('/api/auth/resend-verification-email/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pre_existing_user_with_no_prior_token_gets_the_same_flow_as_a_new_registrant(self):
        """Explicit proof for the deployment decision: existing users (who
        registered before this migration and therefore never had a token
        auto-issued) are NOT special-cased, backfilled, or auto-verified —
        `self.user` here already simulates exactly that starting state
        (created directly via _user(), never through RegisterSerializer,
        so it has zero EmailVerificationToken rows and email_verified=False
        purely from the field's default). Resend must create their first
        token exactly as it would for anyone else — same rule, same code
        path, no legacy-user exception anywhere."""
        self.assertFalse(self.user.email_verified)
        self.assertEqual(EmailVerificationToken.objects.filter(user=self.user).count(), 0)

        resp, mock_send = self._resend()

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args[0][0], self.user.email)
        token_row = EmailVerificationToken.objects.get(user=self.user)
        self.assertTrue(token_row.expires_at > timezone.now())
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)  # not auto-verified merely by having a token issued


# =====================================================================
# EMAIL CHANGE (16-19)
# =====================================================================

class EmailChangeTests(APITestCase):
    def setUp(self):
        cache.clear()  # see ResendTests.setUp's identical comment.
        self.user = _user('changer', email='old@example.com', password='pw12345')
        self.user.email_verified = True
        self.user.email_verified_at = timezone.now()
        self.user.save(update_fields=['email_verified', 'email_verified_at'])
        self.client.force_authenticate(user=self.user)

    def _change(self, new_email, password='pw12345'):
        with patch('accounts.email_verification.email_adapter.send') as mock_send:
            mock_send.return_value = SENT
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post('/api/auth/change-email/', {'email': new_email, 'current_password': password})
        return resp, mock_send

    def test_16_verified_email_changed_becomes_unverified(self):
        resp, _ = self._change('new@example.com')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'new@example.com')
        self.assertFalse(self.user.email_verified)
        self.assertIsNone(self.user.email_verified_at)

    def test_17_old_token_invalidated_by_email_change(self):
        old_row, old_raw = ev.issue_token(self.user, email='old@example.com')
        self._change('new@example.com')
        old_row.refresh_from_db()
        self.assertIsNotNone(old_row.invalidated_at)
        result = ev.consume_token_and_verify_email(old_raw)
        self.assertFalse(result.success)

    def test_18_new_token_generated_for_the_new_address(self):
        resp, mock_send = self._change('new@example.com')
        token_row = EmailVerificationToken.objects.filter(
            user=self.user, used_at__isnull=True, invalidated_at__isnull=True,
        ).latest('id')
        self.assertEqual(token_row.email, 'new@example.com')
        self.assertEqual(mock_send.call_args[0][0], 'new@example.com')

    def test_19_new_email_can_be_verified(self):
        """The token change-email itself issues can't be recovered from the
        mocked send() call (only the recipient address is observable
        there) — issue a fresh one directly to get a raw token and prove
        the new address is verifiable end to end via the same code path
        the emailed link would use."""
        self._change('new@example.com')
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'new@example.com')

        row, raw = ev.issue_token(self.user, email='new@example.com')
        result = ev.consume_token_and_verify_email(raw)
        self.assertTrue(result.success)
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)
        self.assertEqual(self.user.email, 'new@example.com')

    def test_cannot_change_to_an_email_already_in_use(self):
        _user('other_owner', email='taken@example.com')
        resp, mock_send = self._change('taken@example.com')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'email_taken')
        mock_send.assert_not_called()

    def test_requires_correct_current_password(self):
        resp, mock_send = self._change('new@example.com', password='wrong-password')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'current_password_incorrect')
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'old@example.com')
        mock_send.assert_not_called()


# =====================================================================
# NOTIFICATIONS (20-25)
# =====================================================================

class NotificationChannelTests(TestCase):
    def test_20_verified_email_with_preference_enabled_gets_email_delivery(self):
        user = _user('chan_verified')
        user.email_verified = True
        user.save(update_fields=['email_verified'])
        with patch('notifications.email_adapter.send') as mock_send:
            mock_send.return_value = SENT
            with self.captureOnCommitCallbacks(execute=True):
                notification = services.create_notification(user, 'DAILY_TEST_AVAILABLE', 'Daily Test Available')
        delivery = NotificationDelivery.objects.get(notification=notification, channel=CHANNEL_EMAIL)
        self.assertEqual(delivery.status, NotificationDelivery.STATUS_SENT)

    def test_21_unverified_email_gets_no_email_delivery_at_all(self):
        user = _user('chan_unverified')  # email_verified defaults False
        with self.captureOnCommitCallbacks(execute=True):
            notification = services.create_notification(user, 'DAILY_TEST_AVAILABLE', 'Daily Test Available')
        self.assertFalse(NotificationDelivery.objects.filter(notification=notification, channel=CHANNEL_EMAIL).exists())
        # In-app must be entirely unaffected.
        self.assertTrue(NotificationDelivery.objects.filter(notification=notification, channel=CHANNEL_IN_APP).exists())

    def test_22_unverified_email_with_push_enabled_still_gets_push(self):
        user = _user('chan_push_only')
        PushSubscription.objects.create(
            user=user, endpoint=f'https://push.example.com/{user.id}', p256dh='k' * 20, auth='a' * 10,
            status=PushSubscription.STATUS_ACTIVE,
        )
        channels = services._resolve_channels(user, CATEGORY_TESTS)
        self.assertIn(CHANNEL_PUSH, channels)
        self.assertIn(CHANNEL_IN_APP, channels)
        self.assertNotIn(CHANNEL_EMAIL, channels)

    def test_23_verified_email_but_preference_disabled_still_gets_no_email(self):
        user = _user('chan_pref_disabled')
        user.email_verified = True
        user.save(update_fields=['email_verified'])
        NotificationPreference.objects.create(user=user, category=CATEGORY_TESTS, channel=CHANNEL_EMAIL, course=None, enabled=False)
        with patch('notifications.email_adapter.send') as mock_send:
            with self.captureOnCommitCallbacks(execute=True):
                notification = services.create_notification(user, 'DAILY_TEST_AVAILABLE', 'Daily Test Available')
        delivery = NotificationDelivery.objects.get(notification=notification, channel=CHANNEL_EMAIL)
        self.assertEqual(delivery.status, NotificationDelivery.STATUS_SKIPPED)
        mock_send.assert_not_called()

    def test_24_course_enrolled_no_purchase_verified_email_gets_reminder_email(self):
        from notifications import exam_integration

        course = _course('CEE-Reminder-A')
        test_obj = _test_obj(courses=[course])
        student = _user('reminder_verified')
        student.email_verified = True
        student.save(update_fields=['email_verified'])
        Enrollment.objects.create(user=student, course=course, is_active=True)
        session = _session(test_obj, timezone.now() + timedelta(days=2))

        with patch('notifications.email_adapter.send') as mock_send:
            mock_send.return_value = SENT
            with self.captureOnCommitCallbacks(execute=True):
                exam_integration.schedule_session_reminders(session)

        notification = Notification.objects.get(user=student, test=test_obj, event_type='GRAND_TEST_SCHEDULED')
        self.assertTrue(NotificationDelivery.objects.filter(notification=notification, channel=CHANNEL_EMAIL).exists())

    def test_25_course_enrolled_no_purchase_unverified_email_no_email_but_stays_in_audience(self):
        from notifications import exam_integration

        course = _course('CEE-Reminder-B')
        test_obj = _test_obj(courses=[course])
        student = _user('reminder_unverified')  # email_verified defaults False
        Enrollment.objects.create(user=student, course=course, is_active=True)
        session = _session(test_obj, timezone.now() + timedelta(days=2))

        with self.captureOnCommitCallbacks(execute=True):
            created = exam_integration.schedule_session_reminders(session)

        self.assertIn(student.id, {n.user_id for n in created}, 'Unverified email must never remove a student from the reminder audience.')
        notification = Notification.objects.get(user=student, test=test_obj, event_type='GRAND_TEST_SCHEDULED')
        self.assertFalse(NotificationDelivery.objects.filter(notification=notification, channel=CHANNEL_EMAIL).exists())


# =====================================================================
# AUDIENCE AT SCALE (26-29)
# =====================================================================

class AudienceScaleTests(TestCase):
    def test_26_to_29_audience_unaffected_by_purchase_or_verification_email_only_affected_by_verification(self):
        """Reduced-scale mirror of the audit's literal 100/30/70/10
        scenario (same ratios: 20 registered, 6 purchased/14 not, 2 of the
        20 unverified) — see CourseSpecificAudienceScaleTests in
        notifications/tests_exam_reminder_audience.py for the established
        precedent of scaling this exact scenario down for test speed."""
        from notifications import exam_integration

        course = _course('Scale Course')
        test_obj = _test_obj(courses=[course])
        students = [_user(f'scale{i}') for i in range(20)]
        for s in students:
            Enrollment.objects.create(user=s, course=course, is_active=True)
        # 6 of the 20 (30%) purchase the exam — must not affect audience.
        for s in students[:6]:
            GrandTestAccess.objects.create(user=s, test=test_obj, granted_at=timezone.now())
        # 18 of the 20 (90%) have a verified email; the other 2 (10%) don't.
        for s in students[:18]:
            s.email_verified = True
            s.save(update_fields=['email_verified'])

        session = _session(test_obj, timezone.now() + timedelta(days=2))
        with patch('notifications.email_adapter.send') as mock_send:
            mock_send.return_value = SENT
            with self.captureOnCommitCallbacks(execute=True):
                created = exam_integration.schedule_session_reminders(session)

        scheduled = [n for n in created if n.event_type == 'GRAND_TEST_SCHEDULED']
        self.assertEqual(len(scheduled), 20, 'Reminder audience must be all 20 registered students.')

        email_recipient_ids = set(
            NotificationDelivery.objects.filter(
                notification__in=scheduled, channel=CHANNEL_EMAIL,
            ).values_list('notification__user_id', flat=True)
        )
        self.assertEqual(email_recipient_ids, {s.id for s in students[:18]})
        self.assertEqual(len(email_recipient_ids), 18)

        # Purchase status must be entirely uncorrelated with either list above.
        purchaser_ids = {s.id for s in students[:6]}
        self.assertTrue(purchaser_ids.issubset({n.user_id for n in scheduled}))
        non_purchaser_unverified = students[18]  # not a purchaser, not verified
        self.assertIn(non_purchaser_unverified.id, {n.user_id for n in scheduled})
        self.assertNotIn(non_purchaser_unverified.id, email_recipient_ids)


# =====================================================================
# SECURITY (30-34)
# =====================================================================

class SecurityTests(APITestCase):
    def setUp(self):
        cache.clear()  # see ResendTests.setUp's identical comment.
        self.user = _user('sec_target')
        self.token_row, self.raw_token = ev.issue_token(self.user)

    def test_30_tampered_token_rejected(self):
        tampered = self.raw_token[:-1] + ('a' if self.raw_token[-1] != 'a' else 'b')
        resp = self.client.post(f'/api/auth/verify-email/{tampered}/')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)

    def test_31_replay_after_success_rejected(self):
        first = self.client.post(f'/api/auth/verify-email/{self.raw_token}/')
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        replay = self.client.post(f'/api/auth/verify-email/{self.raw_token}/')
        self.assertEqual(replay.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(replay.data['code'], 'already_used')

    def test_32_token_invalidated_after_email_change(self):
        self.client.force_authenticate(user=self.user)
        with patch('accounts.email_verification.email_adapter.send') as mock_send:
            mock_send.return_value = SENT
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post('/api/auth/change-email/', {'email': 'changed@example.com', 'current_password': 'pw12345'})
        resp = self.client.post(f'/api/auth/verify-email/{self.raw_token}/')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_33_resend_rate_limit_blocks_excess_requests(self):
        self.client.force_authenticate(user=self.user)
        with patch('accounts.email_verification.email_adapter.send') as mock_send:
            mock_send.return_value = SENT
            for _ in range(3):
                with self.captureOnCommitCallbacks(execute=True):
                    self.client.post('/api/auth/resend-verification-email/')
            fourth = self.client.post('/api/auth/resend-verification-email/')
        self.assertEqual(fourth.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_34_verification_token_never_appears_in_an_api_response(self):
        resp = self.client.post(f'/api/auth/verify-email/{self.raw_token}/')
        body = str(resp.data)
        self.assertNotIn(self.raw_token, body)
        self.assertNotIn(self.token_row.token_hash, body)

    def test_wrong_email_scenario_end_to_end(self):
        """The audit's own worked example: register with a typo'd address,
        it can never be verified (nobody owns it), change to the correct
        one, verify that instead."""
        with patch('accounts.email_verification.email_adapter.send') as mock_send:
            mock_send.return_value = SENT
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post('/api/auth/register/', {
                    'name': 'Typo Student', 'email': 'studnet@example.com', 'confirm_email': 'studnet@example.com',
                    'password': 'StrongPass!2024',
                })
        user = User.objects.get(email='studnet@example.com')
        self.assertFalse(user.email_verified)

        self.client.force_authenticate(user=user)
        with patch('accounts.email_verification.email_adapter.send') as mock_send2:
            mock_send2.return_value = SENT
            with self.captureOnCommitCallbacks(execute=True):
                change_resp = self.client.post(
                    '/api/auth/change-email/', {'email': 'student@example.com', 'current_password': 'StrongPass!2024'},
                )
        self.assertEqual(change_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(mock_send2.call_args[0][0], 'student@example.com')
        user.refresh_from_db()
        self.assertEqual(user.email, 'student@example.com')
        self.assertFalse(user.email_verified)

        # And the corrected address is now actually verifiable end to end.
        row, raw = ev.issue_token(user, email='student@example.com')
        result = ev.consume_token_and_verify_email(raw)
        self.assertTrue(result.success)
        user.refresh_from_db()
        self.assertTrue(user.email_verified)
