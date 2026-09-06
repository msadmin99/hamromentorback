"""Async cross-student question/option stat processing via Cloud Tasks —
same pattern as media_library/tasks.py and academics/import_tasks.py: a
task is just an authenticated HTTP POST back to this same Cloud Run
service, no broker/worker process.

This exists specifically to fix a real, reproduced deadlock: SubmitTestView
used to apply Question.total_attempts/correct_attempts and Option.
pick_count/pick_percentage updates inline, under a per-question
select_for_update(), in whatever order a student's (randomly shuffled)
answers came back in. Two students submitting exams that share questions
could lock the same questions in opposite orders and deadlock (MySQL errno
1213), a real 500 for a real student. Scoring, ranking, negative marking,
Answer/TestAttempt, and QuestionAttempt/QuestionEvent (which power Smart
Practice/mastery/revision scheduling) are completely unaffected by this —
only the cross-student aggregate stats moved here.

Unlike IMAGE_PROCESSING_ASYNC / IMPORT_PROCESSING_ASYNC, STATS_PROCESSING_
ASYNC defaults to True (not False): inline processing during a live exam
submission is exactly the bug this fixes, so it must never be the
production default. STATS_PROCESSING_ENABLED is a separate, coarser
rollback switch (default True) — set it False to stop stats processing
entirely (submissions stay fully synchronous/fast either way; this only
controls whether the non-critical aggregate stats get updated at all),
without ever falling back to the deadlock-prone inline path.
"""
import json
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def enqueue_question_stats_task(attempt_id, deltas):
    """Called only from SubmitTestView via transaction.on_commit() — never
    before the submission's own transaction has actually committed, so a
    task is never created for an attempt that didn't really get
    submitted. `deltas` is the small list of per-question dicts returned
    by academics.services.record_question_result(..., defer_stats=True)."""
    if not deltas:
        return
    if not settings.STATS_PROCESSING_ENABLED:
        logger.info('enqueue_question_stats_task: STATS_PROCESSING_ENABLED=False, skipping attempt %s', attempt_id)
        return
    if not settings.STATS_PROCESSING_ASYNC:
        process_question_stats(attempt_id, deltas)
        return

    from google.cloud import tasks_v2

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(settings.GCP_PROJECT_ID, settings.GCP_REGION, settings.CLOUD_TASKS_STATS_QUEUE)
    task = {
        'http_request': {
            'http_method': tasks_v2.HttpMethod.POST,
            'url': f'{settings.BACKEND_INTERNAL_URL}/api/attempts/stats-process/',
            'headers': {
                'Content-Type': 'application/json',
                'X-Stats-Processing-Secret': settings.STATS_PROCESSING_SECRET,
            },
            'body': json.dumps({'attempt_id': attempt_id, 'deltas': deltas}).encode(),
        },
    }
    client.create_task(request={'parent': parent, 'task': task})


def process_question_stats(attempt_id, deltas):
    """The actual work, run either inline (STATS_PROCESSING_ASYNC=False,
    local dev only) or via the Cloud Tasks HTTP callback (see
    tests_app.views.QuestionStatsProcessingHandlerView). Idempotent: a
    locked, all-or-nothing check-then-apply-then-mark means a Cloud Tasks
    at-least-once redelivery for the same attempt is a safe no-op, never a
    double-count.

    Final-review fix (pre-production stats-pipeline audit): the previous
    version wrote stats_applied_at=now() as a separate, immediately-
    committed statement *before* calling apply_question_stats_deltas(),
    then relied on a second, separate statement in an `except` block to
    reset it back to NULL on failure. That left a real (if narrow) gap: if
    the process were killed between those two statements — a gunicorn
    worker timeout/recycle, an OOM-kill, a Cloud Run instance
    force-stopped mid-deploy — the claim would stay permanently set to a
    real timestamp even though the deltas were never actually applied, and
    every later retry would see stats_applied_at already non-NULL and
    silently skip it forever: statistics loss that looks exactly like
    success. Wrapping the claim check, the apply, and the stats_applied_at
    write in one outer transaction.atomic() with select_for_update()
    closes that gap: nothing commits unless apply_question_stats_deltas()
    actually returns, so a crash anywhere in this function leaves the row
    exactly as it was (stats_applied_at still NULL, no deltas applied) —
    safe for the next Cloud Tasks retry to pick up cleanly. No separate
    "release" step is needed any more: an exception now simply rolls back
    the whole transaction, which un-claims the row for free."""
    from academics.services import apply_question_stats_deltas
    from .models import TestAttempt

    with transaction.atomic():
        attempt = TestAttempt.objects.select_for_update().filter(
            pk=attempt_id, stats_applied_at__isnull=True,
        ).first()
        if attempt is None:
            logger.info('process_question_stats: attempt %s already processed (or in progress), skipping', attempt_id)
            return

        apply_question_stats_deltas(deltas)

        attempt.stats_applied_at = timezone.now()
        attempt.save(update_fields=['stats_applied_at'])
