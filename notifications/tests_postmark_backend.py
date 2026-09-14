from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import User

from .email_adapter import EmailSendResult


class PostmarkDjangoBackendTests(TestCase):
    @override_settings(POSTMARK_SERVER_TOKEN='test-token', EMAIL_BACKEND='notifications.postmark_backend.EmailBackend')
    def test_send_mail_uses_postmark_adapter(self):
        from django.core.mail import send_mail

        with patch('notifications.email_adapter.send') as mock_send:
            mock_send.return_value = EmailSendResult(
                outcome=EmailSendResult.OUTCOME_SENT,
                provider_message_id='pm-legacy-1',
            )
            count = send_mail(
                'Subject', 'Body', 'Dr. Gutka <support@drgutka.com>', ['student@example.com'],
                fail_silently=False,
            )

        self.assertEqual(count, 1)
        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs['from_email'], 'Dr. Gutka <support@drgutka.com>')

    @override_settings(POSTMARK_SERVER_TOKEN='test-token', EMAIL_BACKEND='notifications.postmark_backend.EmailBackend')
    def test_attachments_are_not_silently_discarded(self):
        from django.core.mail import EmailMessage, get_connection

        message = EmailMessage('Subject', 'Body', 'Dr. Gutka <support@drgutka.com>', ['student@example.com'])
        message.attach('hello.txt', 'hello', 'text/plain')
        with self.assertRaises(ValueError):
            get_connection(fail_silently=False).send_messages([message])
