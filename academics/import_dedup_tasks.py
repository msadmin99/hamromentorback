"""Async duplicate detection for bulk import via Cloud Tasks — same
pattern as academics/import_tasks.py and tests_app/stats_tasks.py: a task
is just an authenticated HTTP POST back to this same Cloud Run service, no
broker/worker process.

Moved off ImportBatchTaxonomyView.patch()'s synchronous request path
(bulk-import taxonomy audit, Phase 3): _run_dedup() can cost well over a
minute against a subject with thousands of existing questions (Phase 2's
own measurements — 133.8s for just 30 rows against 8,702 existing
questions), so running it inline blocked every genuine Subject change for
that long. Falls back to running inline when Cloud Tasks isn't configured
(local dev) — see DEDUP_PROCESSING_ASYNC.

Generation-scoped throughout: ImportBatch.dedup_generation is bumped on
every genuine Subject change (see ImportBatchTaxonomyView.patch), never on
Chapter/Topic/course-only changes or on re-selecting the same Subject.
Every step here — the claim, the periodic in-loop check _run_dedup()
performs via its `is_stale` callback, and the final completion write — is
gated on the batch's CURRENT generation still matching the generation this
specific task was enqueued for. A stale, superseded run (an older Subject
change whose task is still executing, or gets redelivered, after a newer
Subject change has already happened) can therefore never overwrite a
newer generation's results — it simply stops."""
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)


def enqueue_dedup_task(batch_id, generation):
    """Called only from ImportBatchTaxonomyView.patch(), only when the
    PATCH actually changed Subject to a genuinely different value — never
    on Chapter/Topic/course-only changes, and never on re-selecting the
    same Subject (see that view's docstring)."""
    if not settings.DEDUP_PROCESSING_ASYNC:
        run_dedup_task(batch_id, generation)
        return

    from google.cloud import tasks_v2

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(settings.GCP_PROJECT_ID, settings.GCP_REGION, settings.CLOUD_TASKS_DEDUP_QUEUE)
    task = {
        # Generation-scoped task name: gives Cloud Tasks its own dedup for
        # the same generation enqueued twice in quick succession, while a
        # genuinely new generation (a later Subject change) always gets a
        # distinct name, so it's never suppressed as a false duplicate.
        'name': client.task_path(
            settings.GCP_PROJECT_ID, settings.GCP_REGION, settings.CLOUD_TASKS_DEDUP_QUEUE,
            f'dedup-batch-{batch_id}-gen-{generation}',
        ),
        'http_request': {
            'http_method': tasks_v2.HttpMethod.POST,
            'url': f'{settings.BACKEND_INTERNAL_URL}/api/import-batches/dedup-process/',
            'headers': {
                'Content-Type': 'application/json',
                'X-Dedup-Processing-Secret': settings.DEDUP_PROCESSING_SECRET,
            },
            'body': json.dumps({'batch_id': batch_id, 'generation': generation}).encode(),
        },
        # Measured dedup runs can exceed Cloud Tasks' default dispatch
        # deadline for a large subject (Phase 2: up to ~7 minutes for just
        # 100 rows against 8,702 existing questions) — set explicitly to
        # the maximum Cloud Tasks allows rather than risk a premature
        # "failed" retry while a legitimate run is still in progress.
        'dispatch_deadline': {'seconds': 1800},
    }
    try:
        client.create_task(request={'parent': parent, 'task': task})
    except Exception as exc:  # noqa: BLE001 — a name collision (AlreadyExists) means a task for
        # this exact generation is already queued/recently ran — not a
        # failure, just Cloud Tasks' own dedup working as intended.
        if 'AlreadyExists' not in type(exc).__name__:
            raise
        logger.info(
            'enqueue_dedup_task: task for batch %s generation %s already exists, not re-enqueuing',
            batch_id, generation,
        )


def _claim_dedup(batch_id, generation):
    """Short, atomic claim — mirrors import_engine._claim_batch()'s exact
    shape, but scoped to this generation: only claims if dedup_generation
    still equals `generation` (a newer Subject change already moved past
    it otherwise) AND the current dedup_status is claimable (pending, or a
    processing claim old enough to be presumed abandoned). Deliberately
    NOT wrapped around the (potentially minutes-long) dedup computation
    itself — see run_dedup_task()'s docstring for why."""
    from .models import ImportBatch

    now = timezone.now()
    stale_before = now - timedelta(minutes=settings.DEDUP_CLAIM_STALE_MINUTES)
    claimed = ImportBatch.objects.filter(pk=batch_id, dedup_generation=generation).filter(
        Q(dedup_status='pending') | Q(dedup_status='processing', dedup_claimed_at__lt=stale_before),
    ).update(dedup_status='processing', dedup_claimed_at=now)
    return claimed == 1


def _is_stale(batch_id, generation):
    """True if a newer Subject change has moved this batch past the
    generation this run was started for. Checked by _run_dedup()'s inner
    loop every few rows — never holds a lock, just a cheap read."""
    from .models import ImportBatch

    current = ImportBatch.objects.filter(pk=batch_id).values_list('dedup_generation', flat=True).first()
    return current is None or current != generation


def run_dedup_task(batch_id, generation):
    """The actual dedup work — invoked either synchronously (local dev,
    DEDUP_PROCESSING_ASYNC=False) or via the Cloud Tasks HTTP callback
    (see ImportBatchDedupProcessingHandlerView in import_views.py).

    Deliberately NOT one big transaction around the whole run (unlike the
    deferred-stats fix's claim-then-apply-then-mark pattern in
    tests_app/stats_tasks.py) — a dedup run against a large Subject can
    take minutes (Phase 2's own measurements), and holding a row lock that
    whole time would block the very taxonomy PATCH this phase exists to
    stop blocking. Instead:
      1. A short, atomic claim (see _claim_dedup) — milliseconds.
      2. The (possibly long) comparison work itself, unlocked — _run_dedup()
         checks staleness every few rows via the `is_stale` callback and
         stops before writing further rows the moment a newer generation
         supersedes it.
      3. A short, atomic completion check — re-verifies the generation is
         still current before marking dedup_status='completed', so a run
         that finishes just as it becomes stale still never falsely
         reports success for a Subject it wasn't run against.

    Idempotent under every failure mode this needs to survive:
      - Duplicate Cloud Tasks delivery for an already-'completed'
        generation: the claim's WHERE clause only matches 'pending' or a
        stale 'processing' claim, so a redelivery after success fails the
        claim and returns immediately — never a double-run.
      - Two concurrent deliveries for the same generation: only one wins
        the atomic claim UPDATE; the other sees 0 rows affected and exits.
      - Worker crash / Cloud Run instance recycle mid-run: the claim stays
        'processing' with a stale dedup_claimed_at; once older than
        DEDUP_CLAIM_STALE_MINUTES, a later retry (or fresh delivery) can
        reclaim it and resume — resuming is safe because _run_dedup()
        recomputes every row's status from scratch each run (not a
        cursor), so re-running from the top after a partial prior run
        produces the same correct result, never a partial mix.
      - A newer generation superseding an in-flight run: covered by (2)
        above, plus the final completion write's own generation check.
    """
    from django.db import close_old_connections

    from .import_views import _run_dedup
    from .models import ImportBatch

    close_old_connections()
    if not _claim_dedup(batch_id, generation):
        logger.info(
            'run_dedup_task: batch %s generation %s not claimable (already completed, superseded, or in '
            'progress elsewhere), skipping', batch_id, generation,
        )
        return

    batch = ImportBatch.objects.filter(pk=batch_id).first()
    if batch is None or batch.dedup_generation != generation:
        # Extremely unlikely (batch deleted, or superseded between the
        # claim UPDATE and this SELECT) but checked explicitly rather than
        # assumed — same "never trust state didn't move" discipline as the
        # claim's own WHERE clause.
        return

    _run_dedup(batch, is_stale=lambda: _is_stale(batch_id, generation))

    # Final, short, atomic re-check — only this exact generation may mark
    # itself completed, and only if nothing has superseded it since. If a
    # newer generation exists, this UPDATE affects 0 rows and dedup_status
    # is left exactly as the newer generation's own task is managing it —
    # never falsely marked 'completed' for a Subject this run wasn't
    # actually verified against.
    ImportBatch.objects.filter(pk=batch_id, dedup_generation=generation).update(
        dedup_status='completed', dedup_completed_at=timezone.now(),
    )
