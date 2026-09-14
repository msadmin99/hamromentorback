from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import User

from .email_adapter import EmailSendResult


class AdminEmailTestSendViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username='email_test_admin',
            email='admin@example.com',
            password='strong-password',
            is_staff=True,
        )
        self.admin.admin_role = 'admin'
        self.admin.save(update_fields=['admin_role'])

    def test_anonymous_request_is_rejected(self):
        response = self.client.post('/api/notifications/email/test-send/', {})
        self.assertIn(response.status_code, (401, 403))

    def test_wrong_confirmation_is_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            '/api/notifications/email/test-send/',
            {'to_email': 'admin@example.com', 'confirmation': 'YES'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)

    @override_settings(DEBUG=False, EMAIL_TEST_ALLOWED_RECIPIENTS=('admin@example.com',))
    def test_allowed_admin_can_trigger_one_test_send(self):
        self.client.force_authenticate(self.admin)
        with patch('notifications.views.email_adapter.send') as mock_send:
            mock_send.return_value = EmailSendResult(
                outcome=EmailSendResult.OUTCOME_SENT,
                provider_message_id='pm-admin-1',
            )
            response = self.client.post(
                '/api/notifications/email/test-send/',
                {'to_email': 'admin@example.com', 'confirmation': 'SEND_TEST_EMAIL'},
                format='json',
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'sent', 'message_id': 'pm-admin-1'})
        mock_send.assert_called_once()

    @override_settings(DEBUG=False, EMAIL_TEST_ALLOWED_RECIPIENTS=('admin@example.com',))
    def test_non_allowlisted_admin_recipient_is_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            '/api/notifications/email/test-send/',
            {'to_email': 'other@example.com', 'confirmation': 'SEND_TEST_EMAIL'},
            format='json',
        )
        self.assertEqual(response.status_code, 403)
