"""Django email-backend compatibility layer backed by Postmark HTTP API.

This keeps existing legacy ``send_mail()`` call sites working while routing
through the same provider adapter as the notification system. It is intended
for the current simple email messages in billing; attachments are rejected
explicitly rather than silently discarded.
"""
import logging

from django.core.mail.backends.base import BaseEmailBackend

from . import email_adapter

logger = logging.getLogger(__name__)


class EmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        sent_count = 0
        for message in email_messages:
            if not message.recipients():
                continue

            if message.attachments:
                error = 'PostmarkEmailBackend currently does not support attachments.'
                if self.fail_silently:
                    logger.warning(error)
                    continue
                raise ValueError(error)

            html_body = ''
            for alternative, mimetype in getattr(message, 'alternatives', ()):
                if mimetype == 'text/html':
                    html_body = alternative
                    break

            result = email_adapter.send(
                message.to,
                message.subject,
                message.body,
                html_body,
                from_email=message.from_email,
                cc=message.cc,
                bcc=message.bcc,
                reply_to=message.reply_to,
            )
            if result.outcome == email_adapter.EmailSendResult.OUTCOME_SENT:
                sent_count += 1
                continue

            if self.fail_silently:
                logger.warning(
                    'Postmark legacy email send failed code=%s detail=%s',
                    result.error_code,
                    result.error_message,
                )
                continue

            raise RuntimeError(
                f'Postmark email send failed ({result.error_code}): {result.error_message}'
            )

        return sent_count
