"""Email verification: token issue/consume + the verification email itself.

Deliberately separate from `notifications/` (per the governing audit's own
"Implementation Boundary" requirement — account email verification and
notification-channel eligibility are kept as two distinct concerns) and
deliberately does NOT create a Notification/NotificationDelivery row for
the verification link email itself: that pipeline is Admin-visible
(AdminNotificationDetailSerializer et al. show a notification's
action_url/body to staff), and the verification link necessarily contains
the raw, still-valid token — putting it there would be exactly the token
exposure the audit explicitly prohibited ("Never expose verification
tokens"). Sending goes straight through the existing, already-shared
Postmark call site, `notifications.email_adapter.send()` — no second
provider integration, per "Do NOT create a second notification framework."

Token security shape — see EmailVerificationToken's own docstring
(accounts/models.py) for the model-level guarantees (hash-only storage,
256-bit CSPRNG token, per-address binding, one-time use, independent
invalidation). This module is the only code that ever sees a raw token.
"""
import hashlib
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from notifications import email_adapter
from notifications.email_content import build_absolute_url

from .models import EmailVerificationToken

logger = logging.getLogger(__name__)

TOKEN_TTL = timedelta(hours=24)


def _hash(raw_token):
    return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()


def issue_token(user, *, email=None):
    """Creates and returns (token_row, raw_token) for `user` verifying
    `email` (defaults to `user.email`). Invalidates every previously
    issued, still-usable token for this user first (any address) — a
    resend or an email change must retire the old link; two
    simultaneously-valid tokens for the same user are never left
    outstanding. The raw token is returned ONLY here, never persisted."""
    email = email or user.email
    now = timezone.now()

    EmailVerificationToken.objects.filter(
        user=user, used_at__isnull=True, invalidated_at__isnull=True,
    ).update(invalidated_at=now)

    raw_token = secrets.token_urlsafe(32)  # 256 bits of CSPRNG entropy
    token_row = EmailVerificationToken.objects.create(
        user=user, email=email, token_hash=_hash(raw_token), expires_at=now + TOKEN_TTL,
    )
    return token_row, raw_token


def _render_verification_email(user, verify_url):
    subject = 'Verify your Dr. Gutka email address'
    body = (
        'Please verify your email address to confirm that this account belongs to you.\n\n'
        'This link expires in 24 hours. If you did not create a Dr. Gutka account, '
        'you can safely ignore this email — no further action is needed.'
    )
    html_body = render_to_string('notifications/email_base.html', {
        'title': 'Verify your email address',
        'body': body,
        'course_name': '',
        'cta_url': verify_url,
        'cta_label': 'Verify Email',
    })
    return subject, body, html_body


def send_verification_email(token_row, raw_token):
    """Defers the real Postmark call to `transaction.on_commit` (same
    discipline as notifications/services.py's push/email on_commit calls)
    so a provider outage can never roll back or block the caller's own
    transaction. `email_adapter.send()` never raises (see its own
    docstring) — a failure here is recorded on the token row for
    operational visibility and otherwise swallowed: per the audit's
    explicit instruction, a transient email-provider failure must never
    fail registration, and the resend endpoint is the user-facing
    recovery path."""
    verify_url = build_absolute_url(f'/verify-email/{raw_token}')

    def _send():
        subject, text_body, html_body = _render_verification_email(token_row.user, verify_url)
        result = email_adapter.send(token_row.email, subject, text_body, html_body)
        now = timezone.now()
        error_code = '' if result.outcome == email_adapter.EmailSendResult.OUTCOME_SENT else result.error_code
        EmailVerificationToken.objects.filter(pk=token_row.pk).update(
            send_attempted_at=now, send_error_code=error_code,
        )
        if result.outcome != email_adapter.EmailSendResult.OUTCOME_SENT:
            logger.warning(
                'Verification email send failed for token id=%s: %s', token_row.pk, result.error_code,
            )

    transaction.on_commit(_send)


def issue_and_send(user, *, email=None):
    """The one call site every caller (registration, resend, email-change)
    should use — issues a fresh token and schedules its email in one step."""
    token_row, raw_token = issue_token(user, email=email)
    send_verification_email(token_row, raw_token)
    return token_row


class VerificationResult:
    __slots__ = ('success', 'error_code', 'user')

    def __init__(self, success, error_code, user):
        self.success = success
        self.error_code = error_code
        self.user = user


def consume_token_and_verify_email(raw_token):
    """Validates `raw_token` and, if valid, marks the owning user's email
    verified. Returns a VerificationResult; never raises for a bad token
    (invalid/expired/already-used are all ordinary, expected outcomes,
    not exceptions).

    error_code is one of: 'invalid_token', 'expired_token', 'already_used'
    (empty string on success).

    Race-safety (concurrent double-submit of the same link, e.g. a user
    clicking twice or an email client prefetching then the user clicking):
    the actual claim is a single atomic conditional UPDATE — identical
    pattern to notifications/email_tasks.py's `_claim_delivery` — so two
    concurrent calls for the same token can never both succeed, on any
    database backend this project runs (this deliberately does NOT rely on
    select_for_update(), which silently no-ops on SQLite)."""
    if not raw_token:
        return VerificationResult(False, 'invalid_token', None)

    now = timezone.now()
    token_hash = _hash(raw_token)
    row = EmailVerificationToken.objects.filter(token_hash=token_hash).select_related('user').first()

    if row is None:
        return VerificationResult(False, 'invalid_token', None)
    if row.used_at is not None:
        return VerificationResult(False, 'already_used', None)
    if row.invalidated_at is not None:
        return VerificationResult(False, 'invalid_token', None)
    if row.expires_at <= now:
        return VerificationResult(False, 'expired_token', None)
    if row.email.lower() != row.user.email.lower():
        # The address this token verifies no longer matches the user's
        # current address (e.g. it changed again after this token was
        # issued) — never verify an address the user no longer has.
        return VerificationResult(False, 'invalid_token', None)

    # Both writes below (claiming the token AND flipping the user's
    # verified flag) must land together or not at all — wrapped in one
    # atomic block found by a genuine concurrency test (two real racing
    # threads): without it, a DB error surfacing on the user.save() half
    # (e.g. sqlite lock contention under a concurrent writer — the same
    # class of condition this project's own precedent, accounts/tests.py's
    # AdminStudentEditConcurrencyTests, documents for the real MySQL
    # backend too) could leave the token permanently claimed as "used"
    # while the user was never actually marked verified — a silent
    # inconsistency, not just a lost race.
    with transaction.atomic():
        claimed = EmailVerificationToken.objects.filter(
            pk=row.pk, used_at__isnull=True, invalidated_at__isnull=True,
        ).update(used_at=now)
        if claimed == 0:
            # Lost a race to a concurrent call for this exact token.
            return VerificationResult(False, 'already_used', None)

        user = row.user
        user.email_verified = True
        user.email_verified_at = now
        user.save(update_fields=['email_verified', 'email_verified_at'])
    return VerificationResult(True, '', user)
