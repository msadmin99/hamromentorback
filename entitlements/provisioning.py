"""Free Starter provisioning and atomic consumption.

Both functions are idempotent / race-safe by design (Phase 2 spec Steps
12 and 14) — see the docstrings below for exactly how.
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import EntitlementEventLog, FreeStarterEntitlement, FreeStarterPolicy


def provision_free_starter(user):
    """Idempotently ensure `user` has a FreeStarterEntitlement row for every
    currently-active FreeStarterPolicy resource_type.

    Safe to call more than once — registration retry, a lazy re-check on
    first access (see services._try_free_starter), or a future backfill
    command all call this same function. get_or_create() on the
    (user, resource_type) unique constraint means a second call is a no-op
    for any resource_type already provisioned; the unique constraint itself
    (not application-level locking) is what makes this safe even under two
    genuinely concurrent first-time calls for the same user — one of the
    two get_or_create() calls will hit the constraint and Django will
    surface the already-created row instead of a duplicate.

    Deliberately does NOT run inside the caller's own transaction (e.g.
    registration) — a failure here must never fail registration itself.
    Call sites should treat this as best-effort; entitlements.services'
    read path re-attempts it lazily for any student who somehow reached an
    access check without ever having this called for them.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return []

    created_rows = []
    for policy in FreeStarterPolicy.objects.filter(is_active=True):
        expires_at = (timezone.now() + timedelta(days=policy.validity_days)) if policy.validity_days else None
        row, created = FreeStarterEntitlement.objects.get_or_create(
            user=user, resource_type=policy.resource_type,
            defaults={'quantity': policy.quantity, 'unlimited': policy.unlimited, 'expires_at': expires_at},
        )
        if created:
            created_rows.append(row)
            EntitlementEventLog.objects.create(
                user=user, resource_type=policy.resource_type, event='created',
                detail=f'Free Starter provisioned: {"unlimited" if policy.unlimited else policy.quantity}.',
            )
    return created_rows


def consume_free_starter(user, resource_type, amount=1):
    """Atomically consumes `amount` units of a student's Free Starter
    allocation for resource_type. Returns True if consumption succeeded,
    False if there was insufficient remaining quota (or no entitlement row,
    or the row is expired/revoked) — never raises for the "not enough
    left" case, that is an ordinary, expected outcome the caller checks
    for and turns into a "free allocation exhausted" response.

    Race-safety: select_for_update() inside transaction.atomic(), matching
    the exact pattern already used correctly elsewhere in this codebase
    (academics.services._apply_question_stat_delta,
    billing.payment_service's payment-reference locking) rather than a
    naive read-then-write — two concurrent callers can never both consume
    past the limit (see entitlements.tests for a genuine multi-thread
    regression test, not just a sequential one)."""
    with transaction.atomic():
        row = (
            FreeStarterEntitlement.objects.select_for_update()
            .filter(user=user, resource_type=resource_type)
            .first()
        )
        if not row or not row.is_currently_valid:
            return False
        if not row.unlimited and row.used + amount > row.quantity:
            return False

        row.used += amount
        newly_exhausted = (not row.unlimited) and row.used >= row.quantity
        if newly_exhausted:
            row.status = 'exhausted'
            row.save(update_fields=['used', 'status', 'updated_at'])
        else:
            row.save(update_fields=['used', 'updated_at'])

    EntitlementEventLog.objects.create(user=user, resource_type=resource_type, event='consumed', detail=f'-{amount}')
    if newly_exhausted:
        EntitlementEventLog.objects.create(user=user, resource_type=resource_type, event='exhausted')
    return True


def has_free_starter_available(user, resource_type):
    """Non-consuming check: would consume_free_starter currently succeed?
    Lazily provisions first (Step 14), same as
    ensure_and_consume_free_starter, but never mutates usage.

    Exists for callers that must confirm eligibility BEFORE committing to
    consumption, because something else (a password check, an attempt-limit
    check) could still deny the request for an unrelated reason after this
    check passes — tests_app._start_attempt's Grand Test branch is the
    concrete case this fixes: consuming immediately on "no purchase found"
    and only afterward checking test.access_password meant a wrong-password
    guess against a free-starter-eligible Grand Test could burn the
    student's one free grant before they ever got to enter the real
    password. Check with this function, consume with consume_free_starter
    only once every other gate has also passed."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    provision_free_starter(user)
    row = FreeStarterEntitlement.objects.filter(user=user, resource_type=resource_type).first()
    return bool(row and row.is_currently_valid)


def ensure_and_consume_free_starter(user, resource_type, amount=1):
    """Convenience wrapper for live consumption call sites (Phase 3:
    tests_app._start_attempt, academics.QuestionViewSet.answer) —
    lazily provisions (idempotent, cheap: a handful of get_or_create
    calls against currently-active policies) then attempts atomic
    consumption in one call, so a student whose registration-time
    provisioning was ever missed still gets a fair shot at their free
    allocation the first time they actually try to use it."""
    provision_free_starter(user)
    return consume_free_starter(user, resource_type, amount=amount)
