"""Web Push provider adapter (Phase 3).

The ONLY file in this app that imports `pywebpush` or knows what a VAPID
key is — per the Phase 3 brief's rule #5 ("provider-specific logic must be
isolated behind an adapter... the notification engine must not contain
provider-specific HTTP logic"). `push_tasks.py` calls `send()` below and
never touches `pywebpush` directly.

Provider choice (docs/PHASE_3_WEB_PUSH_READINESS.md §I): plain W3C Web
Push + VAPID via `pywebpush`, not Firebase/FCM — no new vendor, no new
credential type beyond one VAPID keypair, and every major browser
(Chrome, Firefox, Edge, Safari) implements this natively. If a future
phase ever needs FCM for native mobile, that becomes a second adapter
behind the same interface, never a rewrite of this one.
"""
import json
from dataclasses import dataclass

from django.conf import settings
from pywebpush import WebPushException, webpush


@dataclass
class PushSendResult:
    """The adapter's own small result type — push_tasks.py branches on
    `outcome`, never on a raw HTTP status code or exception class, so the
    provider (pywebpush today) stays fully swappable."""

    OUTCOME_SENT = 'sent'
    OUTCOME_TEMPORARY_FAILURE = 'temporary_failure'
    OUTCOME_PERMANENT_FAILURE = 'permanent_failure'

    outcome: str
    status_code: int = None
    error_code: str = ''
    error_message: str = ''


# Per RFC 8030 §5.2 / the W3C Push spec: 404 (Not Found) and 410 (Gone) are
# the standard, unambiguous "this subscription no longer exists" signals a
# push service returns — retrying can never succeed. Every other 4xx (400
# malformed request, 401/403 bad VAPID auth) is ALSO not something a bare
# retry of the identical payload fixes, so it is classified permanent too
# (a genuinely different failure mode — a misconfigured VAPID key — should
# surface loudly via error_code/error_message, not spin forever as a
# "temporary" retry). Only 429 (rate limited) and 5xx (provider-side
# outage) are temporary/retryable.
_PERMANENT_STATUS_CODES = frozenset({400, 401, 403, 404, 410, 413})


def send(subscription, payload: dict) -> PushSendResult:
    """Sends one push message to one real PushSubscription. Never raises —
    every failure mode pywebpush can produce is caught and classified into
    a PushSendResult, so a caller (push_tasks.process_push_delivery) never
    needs its own try/except around provider specifics."""
    if not settings.VAPID_PRIVATE_KEY:
        # Fails loudly in logs, not silently — matches this app's existing
        # "never a silent pass" convention (see services.py's own
        # `# noqa: BLE001` comment) — but is still a classified result, not
        # an uncaught exception, so one misconfigured environment can't
        # crash a whole delivery task for every subscription in it.
        return PushSendResult(
            outcome=PushSendResult.OUTCOME_TEMPORARY_FAILURE,
            error_code='vapid_not_configured',
            error_message='VAPID_PRIVATE_KEY is not set — cannot send Web Push.',
        )

    subscription_info = {
        'endpoint': subscription.endpoint,
        'keys': {'p256dh': subscription.p256dh, 'auth': subscription.auth},
    }
    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={'sub': f'mailto:{settings.VAPID_CLAIM_EMAIL}'},
            timeout=10,
        )
        return PushSendResult(outcome=PushSendResult.OUTCOME_SENT)
    except WebPushException as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        outcome = (
            PushSendResult.OUTCOME_PERMANENT_FAILURE
            if status_code in _PERMANENT_STATUS_CODES
            else PushSendResult.OUTCOME_TEMPORARY_FAILURE
        )
        return PushSendResult(
            outcome=outcome,
            status_code=status_code,
            error_code=f'http_{status_code}' if status_code else 'webpush_exception',
            error_message=str(exc)[:500],
        )
    except Exception as exc:  # noqa: BLE001 — network/timeout/etc.: always recorded, never a silent pass.
        return PushSendResult(
            outcome=PushSendResult.OUTCOME_TEMPORARY_FAILURE,
            error_code='unexpected_error',
            error_message=str(exc)[:500],
        )
