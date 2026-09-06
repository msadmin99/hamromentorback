"""Async bulk-import processing via Cloud Tasks — same pattern as
media_library/tasks.py's image processing: a task is just an authenticated
HTTP POST back to this same Cloud Run service, no broker/worker process.

Falls back to processing inline (synchronously) when Cloud Tasks isn't
configured, e.g. local development — see IMPORT_PROCESSING_ASYNC.

Replaces the previous `threading.Thread(..., daemon=True)` used to run a
bulk import: a background thread doesn't survive the request completing (a
Cloud Run instance can be frozen/recycled the moment the response is sent,
silently killing the thread partway through a large batch, with no retry —
the exact scalability-audit finding this module fixes.
"""
import json
import logging

from django.conf import settings

from .import_engine import run_import

logger = logging.getLogger(__name__)


def enqueue_import_task(batch_id):
    if not settings.IMPORT_PROCESSING_ASYNC:
        run_import(batch_id)
        return

    from google.cloud import tasks_v2

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(settings.GCP_PROJECT_ID, settings.GCP_REGION, settings.CLOUD_TASKS_IMPORT_QUEUE)
    task = {
        # A deterministic task name (per batch) gives Cloud Tasks its own
        # dedup for the common case (this endpoint called twice in quick
        # succession for the same batch) — Cloud Tasks rejects a second
        # task with a name it has seen recently. This is a first line of
        # defense, not the only one: run_import()'s own DB-level claim
        # (ImportBatch.processing_claimed_at) is what actually guarantees
        # correctness against a genuine at-least-once redelivery outside
        # that dedup window.
        'name': client.task_path(
            settings.GCP_PROJECT_ID, settings.GCP_REGION, settings.CLOUD_TASKS_IMPORT_QUEUE, f'import-batch-{batch_id}',
        ),
        'http_request': {
            'http_method': tasks_v2.HttpMethod.POST,
            'url': f'{settings.BACKEND_INTERNAL_URL}/api/import-batches/process/',
            'headers': {
                'Content-Type': 'application/json',
                'X-Import-Processing-Secret': settings.IMPORT_PROCESSING_SECRET,
            },
            'body': json.dumps({'batch_id': batch_id}).encode(),
        },
    }
    try:
        client.create_task(request={'parent': parent, 'task': task})
    except Exception as exc:  # noqa: BLE001 — a name collision (AlreadyExists) means a task for
        # this batch is already queued/recently ran — not a failure to
        # report, just the dedup working as intended. Anything else is a
        # genuine enqueue failure and should surface.
        if 'AlreadyExists' not in type(exc).__name__:
            raise
        logger.info('enqueue_import_task: task for batch %s already exists, not re-enqueuing', batch_id)
