"""Async Web Push delivery via Cloud Tasks (Phase 3). Same shape as
media_library/tasks.py's own enqueue_processing_task — an authenticated
HTTP POST back to this same Cloud Run service, no broker/worker process —
applied here to notifications/push_service.py's PushSubscription rows
instead of media assets.

Falls back to processing inline (synchronously) when Cloud Tasks isn't
configured (PUSH_PROCESSING_ASYNC=False, the local-dev default — see
settings.py) — matching every other queue in this codebase.

Retry strategy (docs/PHASE_3_WEB_PUSH_TRACEABILITY.md, decision #4):
Cloud Tasks' own native queue retry is reused rather than a bespoke
retry-sweep. `process_push_delivery` returns True/False; the view calling
it (views.py: PushProcessDeliveryView) turns False into an HTTP 500,
which Cloud Tasks interprets as "redeliver this task" per the queue's own
retry policy — exactly like every other queue callback in this codebase.
A synchronous (non-async) call ignores this return value entirely: there
is no Cloud Tasks to retry it, and a temporary failure there is simply a
failed send this run, correctly not escalated to a permanent one (see the
per-subscription attempt_count handling below).
"""
import json
import logging

from django.conf import settings
from django.utils import timezone as dj_timezone

from . import webpush_adapter
from .events import CHANNEL_PUSH
from .models import Notification, NotificationDelivery, PushDeliveryAttempt, PushSubscription

logger = logging.getLogger(__name__)

MAX_DELIVERY_ATTEMPTS = 3  # matches services.MAX_DELIVERY_ATTEMPTS — kept as its own constant here (not imported) only to avoid a circular import between services.py and this module; both must be changed together if ever tuned.


def enqueue_push_delivery_task(delivery_id):
    if not settings.PUSH_PROCESSING_ASYNC:
        process_push_delivery(delivery_id)
        return

    from google.cloud import tasks_v2

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(settings.GCP_PROJECT_ID, settings.GCP_REGION, settings.CLOUD_TASKS_PUSH_QUEUE)
    task = {
        'http_request': {
            'http_method': tasks_v2.HttpMethod.POST,
            'url': f'{settings.BACKEND_INTERNAL_URL}/api/notifications/push/process/',
            'headers': {
                'Content-Type': 'application/json',
                'X-Push-Processing-Secret': settings.PUSH_PROCESSING_SECRET,
            },
            'body': json.dumps({'delivery_id': delivery_id}).encode(),
        }
    }
    client.create_task(request={'parent': parent, 'task': task})


def _build_payload(notification: Notification) -> dict:
    """Structured, flat payload (docs/PHASE_3_WEB_PUSH_TRACEABILITY.md,
    decision #5) — reuses Notification.action_url verbatim, the same
    field the in-app channel's NotificationClickView already returns.
    Never derived from title/body text."""
    payload = {
        'notification_id': notification.id,
        'title': notification.title,
        'body': notification.body,
        'action_url': notification.action_url,
    }
    if notification.course_id:
        payload['course'] = {'id': notification.course_id, 'name': notification.course.name}
    return payload


def process_push_delivery(delivery_id):
    """The actual work: fan out ONE NotificationDelivery(channel=push) row
    to every currently-active PushSubscription its notification's user
    has, one PushDeliveryAttempt row per subscription. Idempotent across a
    retried task execution — a subscription whose attempt already
    succeeded (STATUS_SENT) is never re-sent to.

    Returns True if every subscription attempted this run reached a
    terminal outcome (sent or permanently failed) with nothing left to
    retry; False if at least one temporary failure occurred, telling the
    caller (the Cloud Tasks callback view) to signal "please retry" —
    never raises, so one malformed row can't crash the whole task."""
    try:
        delivery = NotificationDelivery.objects.select_related('notification', 'notification__course').get(
            pk=delivery_id, channel=CHANNEL_PUSH,
        )
    except NotificationDelivery.DoesNotExist:
        logger.error('process_push_delivery: NotificationDelivery %s (channel=push) not found', delivery_id)
        return True  # nothing to retry — a missing row will never appear later

    notification = delivery.notification
    subscriptions = list(PushSubscription.objects.filter(user_id=notification.user_id, status=PushSubscription.STATUS_ACTIVE))

    if not subscriptions:
        delivery.status = NotificationDelivery.STATUS_SKIPPED
        delivery.save(update_fields=['status'])
        return True

    payload = _build_payload(notification)
    any_temporary_failure = False
    now = dj_timezone.now()

    for subscription in subscriptions:
        attempt, _created = PushDeliveryAttempt.objects.get_or_create(
            delivery=delivery, subscription=subscription,
        )
        if attempt.status == PushDeliveryAttempt.STATUS_SENT:
            continue  # already succeeded on a prior run of this same (retried) task

        result = webpush_adapter.send(subscription, payload)

        if result.outcome == webpush_adapter.PushSendResult.OUTCOME_SENT:
            attempt.status = PushDeliveryAttempt.STATUS_SENT
            attempt.sent_at = now
            attempt.error_code, attempt.error_message = '', ''
            subscription.last_seen_at = now
            subscription.save(update_fields=['last_seen_at'])

        elif result.outcome == webpush_adapter.PushSendResult.OUTCOME_PERMANENT_FAILURE:
            # No attempt-count wait — a 404/410/etc. is unambiguous, retrying
            # the identical payload can never succeed (P0-10).
            attempt.status = PushDeliveryAttempt.STATUS_FAILED
            attempt.failed_at = now
            attempt.provider_status_code = result.status_code
            attempt.error_code, attempt.error_message = result.error_code, result.error_message
            subscription.status = PushSubscription.STATUS_INVALID
            subscription.save(update_fields=['status'])

        else:  # temporary failure
            attempt.attempt_count += 1
            attempt.provider_status_code = result.status_code
            attempt.error_code, attempt.error_message = result.error_code, result.error_message
            if attempt.attempt_count >= MAX_DELIVERY_ATTEMPTS:
                attempt.status = PushDeliveryAttempt.STATUS_FAILED
                attempt.failed_at = now
            else:
                attempt.status = PushDeliveryAttempt.STATUS_QUEUED
                any_temporary_failure = True

        attempt.save()

    delivery.status = NotificationDelivery.STATUS_SENT
    delivery.sent_at = now
    delivery.save(update_fields=['status', 'sent_at'])

    return not any_temporary_failure
