"""Postmark HTTP API email adapter.

This module is the only notification-layer module that knows Postmark's HTTP
API contract. The existing notification pipeline continues to call
``email_adapter.send(...)``; only the provider boundary changes.

A small Django EmailBackend compatibility layer lives in postmark_backend.py
so legacy ``send_mail()`` call sites (renewal reminders, invoices, Grand Test
emails) also use the same Postmark HTTP API without a broad business-logic
rewrite.
"""
import json
import logging
import socket
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

logger = logging.getLogger(__name__)

MAX_RECIPIENTS = 50


@dataclass
class EmailSendResult:
    """Normalized provider result consumed by the existing delivery state machine."""

    OUTCOME_SENT = 'sent'
    OUTCOME_TEMPORARY_FAILURE = 'temporary_failure'
    OUTCOME_PERMANENT_FAILURE = 'permanent_failure'

    outcome: str
    error_code: str = ''
    error_message: str = ''
    provider_message_id: str = ''


def _as_addresses(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(',') if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def _validate_addresses(addresses, field_name):
    for address in addresses:
        try:
            validate_email(address)
        except ValidationError:
            return EmailSendResult(
                outcome=EmailSendResult.OUTCOME_PERMANENT_FAILURE,
                error_code=f'invalid_{field_name}',
                error_message=f'{field_name.capitalize()} address failed validation.',
            )
    return None


def _redact(value):
    """Return a bounded, secret-safe diagnostic string."""
    text = str(value or '')
    token = getattr(settings, 'POSTMARK_SERVER_TOKEN', '')
    if token:
        text = text.replace(token, '[REDACTED]')
    text = text.replace('X-Postmark-Server-Token', '[REDACTED_HEADER]')
    return text[:500]


def _provider_error(raw_body, status_code):
    try:
        payload = json.loads(raw_body.decode('utf-8', errors='replace'))
    except (TypeError, ValueError):
        return str(status_code), f'Postmark HTTP {status_code}.'
    if isinstance(payload, dict):
        return str(payload.get('ErrorCode', status_code)), _redact(
            payload.get('Message', f'Postmark HTTP {status_code}.')
        )
    return str(status_code), f'Postmark HTTP {status_code}.'


def _classify_http_failure(status_code, error_code, message):
    temporary = status_code in (408, 425, 429) or status_code >= 500
    return EmailSendResult(
        outcome=(
            EmailSendResult.OUTCOME_TEMPORARY_FAILURE
            if temporary else EmailSendResult.OUTCOME_PERMANENT_FAILURE
        ),
        error_code=f'postmark_{error_code}',
        error_message=message,
    )


def _configuration_error(message):
    return EmailSendResult(
        outcome=EmailSendResult.OUTCOME_PERMANENT_FAILURE,
        error_code='configuration_error',
        error_message=message,
    )


def send(
    to_email,
    subject,
    text_body,
    html_body,
    *,
    from_email=None,
    cc=None,
    bcc=None,
    reply_to=None,
):
    """Send one message through Postmark's transactional HTTP API.

    The function never raises provider/network exceptions. Callers receive a
    stable EmailSendResult so the current notification retry state machine can
    remain unchanged.
    """
    token = getattr(settings, 'POSTMARK_SERVER_TOKEN', '')
    if not token:
        return _configuration_error(
            'POSTMARK_SERVER_TOKEN is not configured; email delivery cannot reach Postmark.'
        )

    to_addresses = _as_addresses(to_email)
    cc_addresses = _as_addresses(cc)
    bcc_addresses = _as_addresses(bcc)
    reply_to_addresses = _as_addresses(reply_to)

    if not to_addresses and not cc_addresses and not bcc_addresses:
        return EmailSendResult(
            outcome=EmailSendResult.OUTCOME_PERMANENT_FAILURE,
            error_code='no_recipient',
            error_message='At least one email recipient is required.',
        )

    for field_name, addresses in (
        ('recipient', to_addresses),
        ('cc', cc_addresses),
        ('bcc', bcc_addresses),
        ('reply_to', reply_to_addresses),
    ):
        result = _validate_addresses(addresses, field_name)
        if result:
            return result

    if sum(len(items) for items in (to_addresses, cc_addresses, bcc_addresses)) > MAX_RECIPIENTS:
        return EmailSendResult(
            outcome=EmailSendResult.OUTCOME_PERMANENT_FAILURE,
            error_code='too_many_recipients',
            error_message=f'Postmark allows at most {MAX_RECIPIENTS} recipients per message.',
        )

    if not subject:
        return EmailSendResult(
            outcome=EmailSendResult.OUTCOME_PERMANENT_FAILURE,
            error_code='missing_subject',
            error_message='Email subject is required.',
        )

    api_url = getattr(settings, 'POSTMARK_API_URL', 'https://api.postmarkapp.com/email')
    message_stream = getattr(settings, 'POSTMARK_MESSAGE_STREAM', 'outbound')
    timeout = getattr(settings, 'EMAIL_TIMEOUT', 10)

    payload = {
        'From': from_email or settings.DEFAULT_FROM_EMAIL,
        'To': ','.join(to_addresses) if to_addresses else '',
        'Subject': subject,
        'TextBody': text_body or '',
        'HtmlBody': html_body or '',
        'MessageStream': message_stream,
    }
    if cc_addresses:
        payload['Cc'] = ','.join(cc_addresses)
    if bcc_addresses:
        payload['Bcc'] = ','.join(bcc_addresses)
    if reply_to_addresses:
        payload['ReplyTo'] = ','.join(reply_to_addresses)

    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = Request(
        api_url,
        data=body,
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'X-Postmark-Server-Token': token,
            'User-Agent': 'DrGutka/EmailAdapter',
        },
        method='POST',
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            status_code = getattr(response, 'status', response.getcode())
            raw_body = response.read(64 * 1024)

        try:
            result = json.loads(raw_body.decode('utf-8', errors='replace'))
        except (TypeError, ValueError):
            logger.error('Postmark returned a non-JSON response (HTTP %s).', status_code)
            return EmailSendResult(
                outcome=EmailSendResult.OUTCOME_TEMPORARY_FAILURE,
                error_code='invalid_provider_response',
                error_message=f'Postmark returned an invalid response (HTTP {status_code}).',
            )

        error_code = result.get('ErrorCode', 0) if isinstance(result, dict) else status_code
        if status_code != 200 or error_code != 0:
            provider_message = result.get('Message', f'Postmark returned HTTP {status_code}.') if isinstance(result, dict) else f'Postmark returned HTTP {status_code}.'
            return _classify_http_failure(status_code, error_code, _redact(provider_message))

        message_id = str(result.get('MessageID') or '') if isinstance(result, dict) else ''
        if not message_id:
            logger.error('Postmark returned HTTP 200 without MessageID.')
            return EmailSendResult(
                outcome=EmailSendResult.OUTCOME_TEMPORARY_FAILURE,
                error_code='missing_message_id',
                error_message='Postmark accepted the request without returning a MessageID.',
            )

        logger.info(
            'Postmark accepted email recipient_domains=%s message_id=%s',
            ','.join(sorted({address.rsplit('@', 1)[-1] for address in (to_addresses + cc_addresses + bcc_addresses)})),
            message_id,
        )
        return EmailSendResult(
            outcome=EmailSendResult.OUTCOME_SENT,
            provider_message_id=message_id,
        )

    except HTTPError as exc:
        raw_body = exc.read(64 * 1024)
        error_code, message = _provider_error(raw_body, exc.code)
        result = _classify_http_failure(exc.code, error_code, message)
        logger.warning(
            'Postmark HTTP error status=%s code=%s outcome=%s',
            exc.code, error_code, result.outcome,
        )
        return result
    except (socket.timeout, TimeoutError) as exc:
        logger.warning('Postmark request timed out after %ss: %s', timeout, _redact(exc))
        return EmailSendResult(
            outcome=EmailSendResult.OUTCOME_TEMPORARY_FAILURE,
            error_code='timeout',
            error_message=f'No response within EMAIL_TIMEOUT={timeout}s.',
        )
    except URLError as exc:
        logger.warning('Postmark network error: %s', _redact(exc.reason))
        return EmailSendResult(
            outcome=EmailSendResult.OUTCOME_TEMPORARY_FAILURE,
            error_code='connection_error',
            error_message=_redact(exc.reason),
        )
    except OSError as exc:
        logger.warning('Postmark OS/network error: %s', _redact(exc))
        return EmailSendResult(
            outcome=EmailSendResult.OUTCOME_TEMPORARY_FAILURE,
            error_code='connection_error',
            error_message=_redact(exc),
        )
    except Exception as exc:  # noqa: BLE001 — normalized for bounded retry logic.
        logger.exception('Unexpected Postmark adapter failure.')
        return EmailSendResult(
            outcome=EmailSendResult.OUTCOME_TEMPORARY_FAILURE,
            error_code='unknown_error',
            error_message=_redact(exc),
        )
