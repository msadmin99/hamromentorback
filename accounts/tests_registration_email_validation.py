"""Registration Email + Confirm Email (email-verification-finalization
audit, §2/§3): Confirm Email is a TYPO GUARD only, never ownership proof —
matching Email/Confirm Email here never sets or implies email_verified;
only accounts.email_verification's token flow can do that (see
accounts/tests_email_verification.py, which this file deliberately does
not duplicate).

Uses Django's own EmailValidator via RegisterSerializer's plain
EmailField(s) — no custom regex anywhere in this feature."""
from unittest.mock import patch

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from notifications.email_adapter import EmailSendResult

SENT = EmailSendResult(outcome=EmailSendResult.OUTCOME_SENT, provider_message_id='msg-1')


class RegistrationEmailValidationTests(APITestCase):
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
        return resp

    def test_valid_email_and_matching_confirmation_accepted(self):
        resp = self._register()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_missing_email_rejected(self):
        resp = self._register(email='')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('email', resp.data)

    def test_missing_confirm_email_rejected(self):
        payload = {'name': 'New Student', 'email': 'x@example.com', 'password': 'StrongPass!2024'}
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post('/api/auth/register/', payload)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('confirm_email', resp.data)

    def test_syntactically_invalid_email_rejected(self):
        for bad in ('student', 'student@', '@gmail.com', 'student@gmail', 'invalid..address@gmail.com'):
            resp = self._register(email=bad, confirm_email=bad)
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, f'{bad!r} should have been rejected')

    def test_syntactically_invalid_confirm_email_rejected_even_when_email_itself_is_valid(self):
        resp = self._register(confirm_email='not-an-email')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('confirm_email', resp.data)

    def test_legitimate_addresses_are_never_rejected_by_an_overly_strict_check(self):
        for good, name in (
            ('student@gmail.com', 'g1'), ('student.name@example.com', 'g2'), ('student123@college.edu.np', 'g3'),
        ):
            resp = self._register(email=good, confirm_email=good, name=name)
            self.assertEqual(resp.status_code, status.HTTP_201_CREATED, f'{good!r} should have been accepted: {resp.data}')

    def test_confirmation_mismatch_rejected(self):
        resp = self._register(confirm_email='different@example.com')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('confirm_email', resp.data)

    def test_confirmation_mismatch_never_creates_a_user(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self._register(email='nevercreated@example.com', confirm_email='different@example.com')
        self.assertFalse(User.objects.filter(email='nevercreated@example.com').exists())

    def test_leading_and_trailing_whitespace_is_normalized_before_comparison(self):
        resp = self._register(email='  spacey@example.com  ', confirm_email='spacey@example.com')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

    def test_case_difference_alone_does_not_count_as_a_mismatch(self):
        """Matches this codebase's own existing case-insensitive email
        equivalence (RegisterSerializer.validate_email's own
        email__iexact uniqueness check uses the identical rule)."""
        resp = self._register(email='CaseTest@Example.com', confirm_email='casetest@example.com')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

    def test_confirm_email_never_appears_in_the_stored_user_or_response(self):
        resp = self._register()
        self.assertNotIn('confirm_email', resp.data.get('user', {}))

    def test_matching_confirm_email_never_sets_email_verified(self):
        """The audit's central distinction: Confirm Email is a typo guard,
        NOT ownership verification — a brand-new registration with a
        perfectly matching Email/Confirm Email must still start
        email_verified=False, exactly like before Confirm Email existed."""
        resp = self._register()
        self.assertFalse(resp.data['user']['email_verified'])
