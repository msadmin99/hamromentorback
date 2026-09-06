"""Turns a validated ImportRow's raw_data into real Question/Option rows,
and runs a whole batch's worth of them (run_import) — the actual DB-writing
step, invoked via the Cloud Tasks handler (or its synchronous fallback)
once a batch is confirmed. Validation and dedup have already run by the
time a row gets here.

Subject/Chapter/Topic and Courses come from the ImportBatch itself (chosen
once by the admin on the Preview & Validate screen) rather than being
resolved per-row — this is a direct FK assignment to existing taxonomy rows,
never a get-or-create-by-name, so importing can never create a duplicate
Subject/Chapter/Topic."""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

from media_library.service import create_media_asset_from_file
from tests_app.models import Answer, TestQuestion

from .importers.base import load_temp_image
from .models import ImportBatch, Option, Question

logger = logging.getLogger(__name__)


def _attach_image(file_obj, image_type, uploaded_by, question_id=None, option_id=None):
    """Routes an import-extracted image through the same validate/dedup/
    optimize pipeline as a direct upload — the same diagram embedded in
    5 different teachers' CSVs only ever gets stored/processed once.
    Returns the created MediaAsset, or None if the file fails validation
    (logged as an import warning by the caller, never crashes the batch)."""
    if not file_obj:
        return None
    try:
        return create_media_asset_from_file(
            file_obj, image_type, owner=uploaded_by, owner_role='teacher', category='other',
            question_id=question_id, option_id=option_id, original_filename=getattr(file_obj, 'name', ''),
        )
    except ValidationError:
        return None


def question_is_referenced(question):
    """True if a question has already been used in a live Test or answered
    in a submitted attempt — the line rollback/replace must not cross."""
    return TestQuestion.objects.filter(question=question).exists() or Answer.objects.filter(question=question).exists()


def create_question_from_row(data, batch, course_list):
    try:
        year = int(data['year']) if data.get('year') else None
    except (TypeError, ValueError):
        year = None

    question = Question.objects.create(
        subject=batch.subject, chapter=batch.chapter, topic=batch.topic,
        text=data.get('text_html', ''),
        explanation=data.get('explanation_html', ''),
        explanation_video_url=data.get('explanation_video_url', ''),
        remarks=data.get('remarks', ''),
        year=year,
        past_exam_years=data.get('past_exam_years', ''),
        references=data.get('references') or [],
    )

    q_image = load_temp_image(data.get('question_image_path'))
    exp_image = load_temp_image(data.get('explanation_image_path'))
    if q_image or exp_image:
        # Keep the legacy ImageField as a same-request fallback (so the
        # question always has *something* to show even if async variant
        # processing later fails), while also routing through the new
        # validated/deduped/optimized pipeline via image_asset.
        if q_image:
            question.image_asset = _attach_image(q_image, 'question_image', batch.uploaded_by, question_id=question.id)
            q_image.seek(0)
            question.image = q_image
        if exp_image:
            question.explanation_image_asset = _attach_image(
                exp_image, 'explanation_image', batch.uploaded_by, question_id=question.id,
            )
            exp_image.seek(0)
            question.explanation_image = exp_image
        question.save()

    # Scalability audit (Phase 2.3): the common case (an option with no
    # image) used to issue one INSERT per option — up to 4 per question,
    # multiplied across a whole import batch. bulk_create() collapses
    # those into one. An option WITH an image is still created
    # individually: it needs its real pk immediately to backfill the
    # MediaAsset's option_id, and bulk_create() doesn't reliably return
    # primary keys on every backend — notably plain MySQL, this project's
    # production database — so risking a silently-unset FK there isn't
    # worth the saved query for what's normally the rarer case anyway.
    # `order` is set from the original enumerate index regardless of which
    # path an option takes, so interleaving the two never affects ordering.
    options_to_bulk_create = []
    for i, opt in enumerate(data.get('options') or []):
        if not (opt.get('text_html') or '').strip():
            continue
        opt_image = load_temp_image(opt.get('image_path'))
        if opt_image:
            opt_asset = _attach_image(opt_image, 'option_image', batch.uploaded_by)
            opt_image.seek(0)
            option = Option.objects.create(
                question=question, text=opt.get('text_html', ''),
                image=opt_image, image_asset=opt_asset,
                order=i, is_correct=bool(opt.get('is_correct')),
            )
            if opt_asset:
                opt_asset.option_id = option.id
                opt_asset.save(update_fields=['option_id'])
        else:
            options_to_bulk_create.append(Option(
                question=question, text=opt.get('text_html', ''),
                order=i, is_correct=bool(opt.get('is_correct')),
            ))
    if options_to_bulk_create:
        Option.objects.bulk_create(options_to_bulk_create)

    if course_list:
        question.courses.set(course_list)

    return question


def _claim_batch(batch_id):
    """Atomically claims batch_id for processing — True if this call won
    the claim (no other run currently holds it), False if another
    execution is already working on it (most likely a Cloud Tasks at-
    least-once redelivery arriving while the first attempt is still
    running). A claim older than IMPORT_CLAIM_STALE_MINUTES is treated as
    abandoned — the instance that held it most likely died (recycled/
    crashed) mid-run — and can be reclaimed, letting a retry resume from
    wherever per-row status left off."""
    now = timezone.now()
    stale_before = now - timedelta(minutes=settings.IMPORT_CLAIM_STALE_MINUTES)
    claimed = ImportBatch.objects.filter(pk=batch_id).filter(
        Q(processing_claimed_at__isnull=True) | Q(processing_claimed_at__lt=stale_before),
    ).update(processing_claimed_at=now)
    return claimed == 1


def run_import(batch_id):
    """The actual bulk-import work — invoked either synchronously (local
    dev, IMPORT_PROCESSING_ASYNC=False) or via a Cloud Tasks HTTP callback
    (see import_tasks.enqueue_import_task / ImportProcessingHandlerView in
    import_views.py). Idempotent and resumable: only processes rows not
    already in a terminal state (imported/skipped/error), so a retry —
    whether Cloud Tasks' automatic redelivery after a transient failure, a
    Cloud Run instance recycling mid-batch, or a genuinely new confirm on a
    previously-failed batch — never reprocesses a row that already
    succeeded. _claim_batch() additionally guards against two concurrent
    executions (a duplicate task delivery arriving while the first is
    still running) both processing the same batch at once.

    Scalability audit 2.3: ImportBatch's aggregate progress counters are
    persisted periodically (every FLUSH_EVERY rows), not after every
    single row — each ImportRow's own status is still saved immediately,
    since that per-row state (not a separate cursor) is exactly what makes
    a retry safe to resume from."""
    close_old_connections()
    if not _claim_batch(batch_id):
        logger.info('run_import: batch %s already claimed by another run, skipping', batch_id)
        return
    try:
        batch = ImportBatch.objects.get(pk=batch_id)
        batch.status = 'importing'
        if not batch.started_at:
            batch.started_at = timezone.now()
        batch.save(update_fields=['status', 'started_at'])

        course_list = list(batch.courses.all())
        rows = batch.rows.exclude(status__in=['error', 'imported', 'skipped']).order_by('row_number')

        since_last_flush = 0
        FLUSH_EVERY = 20

        def _flush_progress():
            ImportBatch.objects.filter(pk=batch_id).update(
                created_count=batch.created_count, failed_count=batch.failed_count,
                skipped_count=batch.skipped_count, duplicate_count=batch.duplicate_count,
            )

        for row in rows.iterator():
            try:
                if row.status == 'duplicate' and row.dedup_action == 'skip':
                    row.status = 'skipped'
                    row.save(update_fields=['status'])
                    batch.skipped_count += 1
                    since_last_flush += 1
                    if since_last_flush >= FLUSH_EVERY:
                        _flush_progress()
                        since_last_flush = 0
                    continue

                if row.status == 'duplicate' and row.dedup_action == 'replace' and row.duplicate_of_id:
                    old = Question.objects.filter(pk=row.duplicate_of_id).first()
                    if old and not question_is_referenced(old):
                        old.delete()
                    elif old:
                        row.warnings = (row.warnings or []) + [
                            'Could not replace — the existing question is already used in a Test.',
                        ]

                question = create_question_from_row(row.raw_data, batch, course_list)
                row.created_question = question
                row.status = 'imported'
                if row.duplicate_of_id:
                    batch.duplicate_count += 1
                row.save(update_fields=['created_question', 'status', 'warnings'])
                batch.created_count += 1
            except Exception as exc:  # noqa: BLE001 - one bad row must never abort the whole batch
                row.status = 'error'
                row.errors = (row.errors or []) + [str(exc)]
                row.save(update_fields=['status', 'errors'])
                batch.failed_count += 1

            since_last_flush += 1
            if since_last_flush >= FLUSH_EVERY:
                _flush_progress()
                since_last_flush = 0

        _flush_progress()
        batch.status = 'completed'
        batch.completed_at = timezone.now()
        batch.save(update_fields=['status', 'completed_at'])
    except Exception:
        # Re-raised deliberately (unlike media_library's process_media_asset,
        # which swallows its own failures) so the Cloud Tasks HTTP handler
        # can return a non-2xx response and let Cloud Tasks' built-in retry
        # do its job — safe because of the claim + resumable-row-selection
        # above, a retry never reprocesses what already succeeded.
        ImportBatch.objects.filter(pk=batch_id).update(status='failed', completed_at=timezone.now())
        raise
    finally:
        close_old_connections()
