"""Async Email delivery via Cloud Tasks (Phase 4). Same shape as
notifications/push_tasks.py (itself modeled on media_library/tasks.py) —
an authenticated HTTP POST back to this same Cloud Run service, no
broker/worker process, a dedicated queue so an email backlog/failure can
never throttle push/stats/import traffic.

Falls back to processing inline (synchronously) when Cloud Tasks isn't
configured (EMAIL_PROCESSING_ASYNC=False, the local-dev default).

Idempotency (docs/PHASE_4_EMAIL_TRACEABILITY_AND_DESIGN.md §7/§11/§12,
P0-3) — THE central engineering requirement of this phase, and the one
place this module deliberately does NOT mirror push_tasks.py's own
approach: Web Push's retry-safety lives on a separate PushDeliveryAttempt
row (one per device); Email has no multi-device concept, so its
safeguard lives directly on NotificationDelivery via `_claim_delivery`'s
atomic conditional UPDATE, documented in full on that function.
"""
import json
import logging

from django.conf import settings
from django.utils import timezone as dj_timezone

from . import email_adapter, email_content
from .events import CHANNEL_EMAIL
from .models import NotificationDelivery

logger = logging.getLogger(__name__)

# Matches services.MAX_DELIVERY_ATTEMPTS — kept as its own constant here
# (not imported) only to avoid a circular import between services.py and
# this module, exactly like push_tasks.py's own identical constant; both
# must be changed together if ever tuned.
MAX_DELIVERY_ATTEMPTS = 3


def enqueue_email_delivery_task(delivery_id):
    if not settings.EMAIL_PROCESSING_ASYNC:
        process_email_delivery(delivery_id)
        return

    from google.cloud import tasks_v2

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(settings.GCP_PROJECT_ID, settings.GCP_REGION, settings.CLOUD_TASKS_EMAIL_QUEUE)
    task = {
        'http_request': {
            'http_method': tasks_v2.HttpMethod.POST,
            'url': f'{settings.BACKEND_INTERNAL_URL}/api/notifications/email/process/',
            'headers': {
                'Content-Type': 'application/json',
                'X-Email-Processing-Secret': settings.EMAIL_PROCESSING_SECRET,
            },
            'body': json.dumps({'delivery_id': delivery_id}).encode(),
        }
    }
    client.create_task(request={'parent': parent, 'task': task})


def _claim_delivery(delivery_id):
    """Atomically claims one NotificationDelivery(channel=email) row for
    sending — a single conditional UPDATE (`WHERE id=... AND status=
    'queued'`), deliberately NOT `select_for_update()`. This project's
    local/test database is SQLite (see hamromentor/settings.py's DB
    fallback), and Django's `select_for_update()` silently no-ops on
    SQLite (no row-level locking support at all) — relying on it would
    make this guarantee real only in MySQL/production and silently
    absent in every test run and local dev session, which is exactly the
    kind of gap that hides a real bug until production. A conditional
    UPDATE's WHERE clause is atomic at the SQL level on every backend
    this project runs — MySQL and SQLite alike — so the guarantee below
    is uniformly real everywhere, and is directly, deterministically
    testable without any database-specific locking behavior.

    Returns the claimed row (now STATUS_PROCESSING) if this call won the
    claim; None if another worker, a concurrent call, or a previous run
    already claimed/sent/failed/skipped it — in which case the caller
    must send nothing further.

    This is the actual, precise duplicate-send guard for Phase 4. It
    provides application-level, at-least-once-effectively-once duplicate
    suppression: two Cloud Tasks executions (concurrent or a genuine
    retry) racing to claim the same delivery_id can never both win this
    UPDATE, so at most one of them ever proceeds to call the real email
    provider for this delivery. It does NOT provide, and this module
    never claims, true distributed exactly-once delivery — see
    process_email_delivery's own docstring for the one narrow window
    (a crash between provider acceptance and this row's own status
    write) where that distinction actually matters."""
    updated = NotificationDelivery.objects.filter(
        pk=delivery_id, channel=CHANNEL_EMAIL, status=NotificationDelivery.STATUS_QUEUED,
    ).update(status=NotificationDelivery.STATUS_PROCESSING, updated_at=dj_timezone.now())
    if updated == 0:
        return None
    return NotificationDelivery.objects.select_related(
        'notification', 'notification__course', 'notification__user',
    ).get(pk=delivery_id)


def process_email_delivery(delivery_id):
    """The actual work: claim the delivery, render its content, send it,
    record the real outcome. Returns True if this delivery reached a
    terminal state this run (sent, permanently failed, or was already
    claimed by someone else) with nothing left to retry; False if a
    temporary failure occurred and the caller (the Cloud Tasks callback
    view) should signal "please retry" — never raises, so one malformed
    row can never crash the whole task.

    PROCESSING is only ever the resting state while a send is genuinely
    in flight inside this one function call — both outcomes (success ->
    SENT, temporary failure -> back to QUEUED so a retry can reclaim it)
    resolve out of PROCESSING before this function returns. The ONLY way
    a row is left stuck at PROCESSING is a hard process crash strictly
    between the provider accepting the message and this function's own
    status write below — a narrow, honestly-documented risk window
    (docs/PHASE_4_EMAIL_TRACEABILITY_AND_DESIGN.md §7), not a case this
    module silently pretends cannot happen. Recovering a stuck
    PROCESSING row automatically is a deliberately out-of-scope v2 item,
    not solved here by inventing a distributed transaction this
    project's real scale does not need."""
    delivery = _claim_delivery(delivery_id)
    if delivery is None:
        logger.info('process_email_delivery: delivery %s already claimed/processed — skipping', delivery_id)
        return True

    notification = delivery.notification
    recipient = notification.user.email
    subject, text_body, html_body = email_content.render_email(notification)

    result = email_adapter.send(recipient, subject, text_body, html_body)
    now = dj_timezone.now()

    if result.outcome == email_adapter.EmailSendResult.OUTCOME_SENT:
        delivery.status = NotificationDelivery.STATUS_SENT
        delivery.sent_at = now
        delivery.attempt_count += 1
        delivery.error_code, delivery.error_message = '', ''
        delivery.save(update_fields=['status', 'sent_at', 'attempt_count', 'error_code', 'error_message', 'updated_at'])
        return True

    delivery.attempt_count += 1
    delivery.error_code = result.error_code
    delivery.error_message = result.error_message

    if result.outcome == email_adapter.EmailSendResult.OUTCOME_PERMANENT_FAILURE or delivery.attempt_count >= MAX_DELIVERY_ATTEMPTS:
        # Permanent, or temporary-but-exhausted: terminal either way, no more retries.
        delivery.status = NotificationDelivery.STATUS_FAILED
        delivery.failed_at = now
        delivery.save(update_fields=['status', 'failed_at', 'attempt_count', 'error_code', 'error_message', 'updated_at'])
        return True

    # Temporary failure, attempts remain — release the claim back to
    # QUEUED so a retried Cloud Tasks execution (or a future
    # dispatch_due_notifications sweep) can pick it up again. Never left
    # at PROCESSING for a resolved failure — only a genuine mid-flight
    # crash leaves that state, never this branch.
    delivery.status = NotificationDelivery.STATUS_QUEUED
    delivery.save(update_fields=['status', 'attempt_count', 'error_code', 'error_message', 'updated_at'])
    return False
