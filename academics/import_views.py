import logging
import threading
import zipfile

from courses.models import Course
from django.db import close_old_connections, transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from tests_app.serializers import TestAdminSerializer

from .import_dedup import existing_texts_for_subject, find_duplicate, normalize_option_set, normalize_text
from .import_engine import create_question_from_row, question_is_referenced
from .import_validation import validate_parsed_question
from .importers import PARSERS
from .models import Chapter, ImportBatch, ImportRow, Question, Subject, Topic

# Observability fix: a plain module-level logger, deliberately NOT relying
# on Django's default LOGGING config — that config only attaches a console
# handler to `django`-prefixed loggers when DEBUG=True (see
# django.utils.log.DEFAULT_LOGGING's `console` handler, filtered by
# require_debug_true). With DEBUG=False in every real environment, a
# logger under this app's own name has no configured handler either, so it
# falls through to Python's own built-in `logging.lastResort` — a
# stderr StreamHandler always present regardless of Django's config — which
# Cloud Run captures unconditionally. This is what actually makes
# logger.exception() below visible in Cloud Logging without a broader
# LOGGING config change (evaluated and intentionally deferred — see the
# Phase report accompanying this commit for why a project-wide LOGGING
# dict is a good follow-up but broader than this targeted fix needs).
logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB
ALLOWED_EXTENSIONS = {'docx', 'xlsx', 'csv', 'json'}
ALLOWED_CONTENT_TYPES = {
    'docx': {'application/vnd.openxmlformats-officedocument.wordprocessingml.document'},
    'xlsx': {'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/vnd.ms-excel'},
    'csv': {'text/csv', 'application/vnd.ms-excel', 'text/plain'},
    'json': {'application/json', 'text/plain'},
}


class ImportUploadThrottle(UserRateThrottle):
    # DRF's SimpleRateThrottle reads the rate from settings.REST_FRAMEWORK's
    # DEFAULT_THROTTLE_RATES[scope], not a class attribute — overriding
    # get_rate() keeps this self-contained without touching global settings.
    scope = 'import_upload'

    def get_rate(self):
        return '20/hour'


MAX_DECOMPRESSED_BYTES = 200 * 1024 * 1024  # 200MB — guards xlsx/docx (both zip containers) against zip bombs


def _check_not_zip_bomb(file_obj):
    """xlsx/docx are zip containers — inspect the central directory's declared
    uncompressed sizes before ever handing the file to openpyxl/python-docx,
    which would otherwise fully decompress an arbitrarily large payload."""
    try:
        with zipfile.ZipFile(file_obj) as zf:
            total_uncompressed = sum(info.file_size for info in zf.infolist())
    except zipfile.BadZipFile:
        raise ValueError('That file is not a valid Office document (corrupt or not really a docx/xlsx).') from None
    finally:
        file_obj.seek(0)
    if total_uncompressed > MAX_DECOMPRESSED_BYTES:
        raise ValueError('That file expands to an implausibly large size when decompressed and was rejected.')


def _detect_format(filename):
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    return ext if ext in ALLOWED_EXTENSIONS else None


class ImportUploadView(APIView):
    """POST /import-batches/upload/ — parses the file and creates the batch +
    per-row preview (structure extraction only; no Question rows are created
    until a separate /confirm/ call)."""
    permission_classes = [IsAdminUser]
    throttle_classes = [ImportUploadThrottle]

    def post(self, request):
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({'detail': 'No file uploaded.'}, status=400)

        if file_obj.size > MAX_UPLOAD_BYTES:
            return Response({'detail': f'File is too large — the limit is {MAX_UPLOAD_BYTES // (1024 * 1024)}MB.'}, status=400)

        file_format = _detect_format(file_obj.name)
        if not file_format:
            return Response({'detail': f'Unsupported file type. Allowed: {", ".join(sorted(ALLOWED_EXTENSIONS))}.'}, status=400)

        # "application/octet-stream" is a common generic fallback many clients
        # (curl, some browsers) send when they can't sniff a specific type —
        # treated as inconclusive, not a mismatch. The extension check above
        # already gates the format; this only catches a clear cross-format
        # mismatch (e.g. an image content-type on a claimed .json upload).
        content_type = (file_obj.content_type or '').split(';')[0].strip()
        allowed_types = ALLOWED_CONTENT_TYPES.get(file_format, set())
        if content_type and content_type != 'application/octet-stream' and allowed_types and content_type not in allowed_types:
            return Response({'detail': f'That file\'s content type ({content_type}) doesn\'t match a {file_format} file.'}, status=400)

        if file_format in ('xlsx', 'docx'):
            try:
                _check_not_zip_bomb(file_obj)
            except ValueError as exc:
                return Response({'detail': str(exc)}, status=400)

        import_mode = request.data.get('import_mode') or 'question_bank'
        if import_mode not in dict(ImportBatch.MODE_CHOICES):
            return Response({'detail': f'Unknown import_mode "{import_mode}".'}, status=400)

        parser = PARSERS[file_format]
        batch = ImportBatch.objects.create(
            uploaded_by=request.user, file_name=file_obj.name, file_format=file_format, status='validating',
            import_mode=import_mode,
        )
        try:
            parsed_questions = parser(file_obj) if file_format == 'json' else parser(file_obj, batch_id=batch.id)
        except ValueError as exc:
            batch.status = 'failed'
            batch.save(update_fields=['status'])
            return Response({'detail': str(exc), 'batch_id': batch.id}, status=400)
        except Exception as exc:  # noqa: BLE001 - never let a malformed upload 500 the request
            batch.status = 'failed'
            batch.save(update_fields=['status'])
            return Response({'detail': f'Could not process that file: {exc}', 'batch_id': batch.id}, status=400)

        if not parsed_questions:
            batch.status = 'failed'
            batch.save(update_fields=['status'])
            return Response({'detail': 'No questions could be found in that file.', 'batch_id': batch.id}, status=400)

        # Duplicate detection is deferred until the admin picks this batch's
        # Subject on the Preview & Validate screen (see ImportBatchTaxonomyView
        # / _run_dedup below) — the subject isn't known yet at upload time
        # since it no longer comes from the file.
        #
        # Observability fix: this final stretch (per-row validation, the
        # bulk_create, batch.save, and building the response summary) was
        # the one part of this view with no exception handling at all — a
        # failure here fell straight through to Django's generic 500 with
        # nothing recorded anywhere (see the Phase report: Django's own
        # default logging only prints to console when DEBUG=True, so in
        # every real environment these were entirely silent). The try/except
        # below changes NOTHING about the response: on success it returns
        # the exact same 201 + summary as before; on an unexpected error it
        # re-raises the identical exception, so Django's normal
        # exception-to-500 handling still produces the same 500 response a
        # client sees today. The only difference is that the exception is
        # now logged, with full traceback, before it propagates. Never logs
        # file contents, request headers, or any credential — only the
        # batch id (already a non-secret, admin-visible identifier), the
        # detected file format, and how many questions the parser found.
        try:
            rows_to_create = []
            for i, pq in enumerate(parsed_questions, start=1):
                errors, warnings = validate_parsed_question(pq)
                row_status = 'error' if errors else ('warning' if warnings else 'valid')
                rows_to_create.append(ImportRow(
                    batch=batch, row_number=i, raw_data=pq, status=row_status,
                    errors=errors, warnings=warnings,
                ))

            ImportRow.objects.bulk_create(rows_to_create)
            batch.total_rows = len(rows_to_create)
            batch.status = 'ready'
            batch.save(update_fields=['total_rows', 'status'])

            return Response(_batch_summary(batch), status=status.HTTP_201_CREATED)
        except Exception:
            logger.exception(
                'ImportUploadView: unexpected error finalizing batch id=%s file_format=%s parsed_question_count=%s',
                batch.id, file_format, len(parsed_questions),
            )
            raise


def _batch_summary(batch):
    counts = {s: batch.rows.filter(status=s).count() for s, _ in ImportRow.STATUS_CHOICES}
    return {
        'id': batch.id, 'file_name': batch.file_name, 'file_format': batch.file_format,
        'status': batch.status, 'total_rows': batch.total_rows,
        'import_mode': batch.import_mode, 'created_test_id': batch.created_test_id,
        'created_count': batch.created_count, 'failed_count': batch.failed_count,
        'skipped_count': batch.skipped_count, 'duplicate_count': batch.duplicate_count,
        'progress_percent': batch.progress_percent,
        'row_counts': counts,
        'subject_id': batch.subject_id, 'chapter_id': batch.chapter_id, 'topic_id': batch.topic_id,
        'course_ids': list(batch.courses.values_list('id', flat=True)),
        'started_at': batch.started_at, 'completed_at': batch.completed_at, 'created_at': batch.created_at,
    }


def _run_dedup(batch):
    """(Re)computes duplicate flags for every non-error row against the
    batch's currently-selected Subject plus the other rows in this same
    file. Called whenever the taxonomy selection changes — a row previously
    flagged duplicate under one Subject choice is un-flagged if it no longer
    matches under a different one."""
    if not batch.subject_id:
        return

    existing = existing_texts_for_subject(batch.subject)
    rows = list(batch.rows.exclude(status='error').order_by('row_number'))
    batch_texts = {
        row.id: {
            'text': normalize_text(row.raw_data.get('text_html')),
            'options': normalize_option_set(o.get('text_html') for o in (row.raw_data.get('options') or [])),
        }
        for row in rows
    }

    for row in rows:
        dup_id, score = find_duplicate(row.raw_data, existing, batch_texts, self_index=row.id)
        if dup_id is not None:
            row.status = 'duplicate'
            row.duplicate_of_id = dup_id if isinstance(dup_id, int) else None
            note = f'{int(score * 100)}% similar to an existing question.'
            if note not in (row.warnings or []):
                row.warnings = (row.warnings or []) + [note]
            row.save(update_fields=['status', 'duplicate_of_id', 'warnings'])
        elif row.status == 'duplicate':
            row.status = 'warning' if row.warnings else 'valid'
            row.duplicate_of_id = None
            row.dedup_action = ''
            row.save(update_fields=['status', 'duplicate_of_id', 'dedup_action'])


class ImportBatchTaxonomyView(APIView):
    """PATCH /import-batches/<id>/taxonomy/ — sets this batch's Subject,
    Chapter, Topic and Course(s), all picked from existing Subject Management
    data (never created here). Every question in the batch gets this same
    taxonomy on confirm. Always send the full desired state (subject_id,
    chapter_id, topic_id, course_ids) — a field sent as null clears it."""
    permission_classes = [IsAdminUser]

    def patch(self, request, batch_id):
        try:
            batch = ImportBatch.objects.get(pk=batch_id)
        except ImportBatch.DoesNotExist:
            return Response({'detail': 'Batch not found.'}, status=404)
        if batch.status not in ('ready', 'failed'):
            return Response({'detail': f'Batch is currently "{batch.status}" — cannot change its taxonomy.'}, status=400)

        data = request.data
        subject = chapter = topic = None
        if data.get('subject_id') is not None:
            subject = Subject.objects.filter(pk=data['subject_id']).first()
            if not subject:
                return Response({'detail': 'Selected Subject was not found.'}, status=400)
        if data.get('chapter_id') is not None:
            chapter = Chapter.objects.filter(pk=data['chapter_id']).first()
            if not chapter or (subject and chapter.subject_id != subject.id):
                return Response({'detail': 'Selected Chapter does not belong to the selected Subject.'}, status=400)
        if data.get('topic_id') is not None:
            topic = Topic.objects.filter(pk=data['topic_id']).first()
            if not topic or (chapter and topic.chapter_id != chapter.id):
                return Response({'detail': 'Selected Topic does not belong to the selected Chapter.'}, status=400)

        batch.subject = subject
        batch.chapter = chapter
        batch.topic = topic
        batch.save(update_fields=['subject', 'chapter', 'topic'])

        if 'course_ids' in data:
            batch.courses.set(Course.objects.filter(id__in=data.get('course_ids') or []))

        _run_dedup(batch)

        return Response(_batch_summary(batch))


class ImportBatchListView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        batches = ImportBatch.objects.select_related('uploaded_by').all()[:100]
        return Response([{**_batch_summary(b), 'uploaded_by_email': b.uploaded_by.email if b.uploaded_by else None} for b in batches])


class ImportBatchRowsView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request, batch_id):
        page = max(int(request.query_params.get('page', 1)), 1)
        page_size = min(int(request.query_params.get('page_size', 25)), 100)
        rows = ImportRow.objects.filter(batch_id=batch_id).order_by('row_number')
        status_filter = request.query_params.get('status')
        if status_filter:
            rows = rows.filter(status=status_filter)
        total = rows.count()
        start = (page - 1) * page_size
        page_rows = rows[start:start + page_size]
        return Response({
            'total': total, 'page': page, 'page_size': page_size,
            'results': [
                {
                    'id': r.id, 'row_number': r.row_number, 'status': r.status,
                    'errors': r.errors, 'warnings': r.warnings, 'duplicate_of_id': r.duplicate_of_id,
                    'dedup_action': r.dedup_action, 'data': r.raw_data,
                }
                for r in page_rows
            ],
        })


class ImportRowDetailView(APIView):
    permission_classes = [IsAdminUser]

    def patch(self, request, batch_id, row_id):
        try:
            row = ImportRow.objects.get(pk=row_id, batch_id=batch_id)
        except ImportRow.DoesNotExist:
            return Response({'detail': 'Row not found.'}, status=404)

        if 'data' in request.data:
            row.raw_data = request.data['data']
            errors, warnings = validate_parsed_question(row.raw_data)
            row.errors, row.warnings = errors, warnings
            if row.status != 'duplicate':
                row.status = 'error' if errors else ('warning' if warnings else 'valid')
        if 'dedup_action' in request.data:
            row.dedup_action = request.data['dedup_action']
        row.save()
        return Response({
            'id': row.id, 'status': row.status, 'errors': row.errors, 'warnings': row.warnings,
            'dedup_action': row.dedup_action, 'data': row.raw_data,
        })

    def delete(self, request, batch_id, row_id):
        """Removes one invalid/unwanted row from the batch before import —
        e.g. a row that can't be fixed, or a genuine duplicate the admin
        doesn't want to bring in at all. Only allowed pre-import: once a
        batch has started importing, its rows are what the run/rollback
        history refers to."""
        try:
            row = ImportRow.objects.get(pk=row_id, batch_id=batch_id)
        except ImportRow.DoesNotExist:
            return Response({'detail': 'Row not found.'}, status=404)

        batch = row.batch
        if batch.status != 'ready':
            return Response(
                {'detail': f'Batch is currently "{batch.status}" — rows can only be removed before import starts.'},
                status=400,
            )

        row.delete()
        batch.total_rows = max(0, batch.total_rows - 1)
        batch.save(update_fields=['total_rows'])
        return Response(_batch_summary(batch))


def _run_import(batch_id):
    close_old_connections()
    try:
        batch = ImportBatch.objects.get(pk=batch_id)
        batch.status = 'importing'
        batch.started_at = timezone.now()
        batch.save(update_fields=['status', 'started_at'])

        course_list = list(batch.courses.all())
        rows = batch.rows.exclude(status='error').order_by('row_number')
        for row in rows.iterator():
            try:
                if row.status == 'duplicate' and row.dedup_action == 'skip':
                    row.status = 'skipped'
                    row.save(update_fields=['status'])
                    ImportBatch.objects.filter(pk=batch_id).update(skipped_count=batch.skipped_count + 1)
                    batch.skipped_count += 1
                    continue

                if row.status == 'duplicate' and row.dedup_action == 'replace' and row.duplicate_of_id:
                    old = Question.objects.filter(pk=row.duplicate_of_id).first()
                    if old and not question_is_referenced(old):
                        old.delete()
                    elif old:
                        row.warnings = (row.warnings or []) + ['Could not replace — the existing question is already used in a Test.']

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
            batch.save(update_fields=['created_count', 'failed_count', 'skipped_count', 'duplicate_count'])

        batch.status = 'completed'
        batch.completed_at = timezone.now()
        batch.save(update_fields=['status', 'completed_at'])
    except Exception:
        ImportBatch.objects.filter(pk=batch_id).update(status='failed', completed_at=timezone.now())
        raise
    finally:
        close_old_connections()


class ImportConfirmView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, batch_id):
        try:
            batch = ImportBatch.objects.get(pk=batch_id)
        except ImportBatch.DoesNotExist:
            return Response({'detail': 'Batch not found.'}, status=404)
        if batch.status not in ('ready', 'failed'):
            return Response({'detail': f'Batch is currently "{batch.status}" — cannot confirm.'}, status=400)

        if not (batch.subject_id and batch.chapter_id and batch.topic_id):
            return Response({'detail': 'Please select Subject, Chapter and Topic before importing.'}, status=400)

        blocked = batch.rows.filter(status='duplicate', dedup_action='').exists()
        if blocked:
            return Response({'detail': 'Some duplicate rows still need a Skip/Replace/Keep Both decision before importing.'}, status=400)

        thread = threading.Thread(target=_run_import, args=(batch.id,), daemon=True)
        thread.start()
        return Response({'detail': 'Import started.', 'batch_id': batch.id})


class RowImportError(Exception):
    """Raised by _create_questions_for_test to identify exactly which row
    broke — never swallowed like _run_import does, since Import & Create
    Test must abort (and roll back) the whole attempt on any row failure."""

    def __init__(self, row_number, reason):
        self.row_number = row_number
        self.reason = reason
        super().__init__(f'Row {row_number}: {reason}')


def _create_questions_for_test(batch):
    """Creates a Question for every eligible row, in row order, for the
    Import & Create Test flow. Unlike _run_import, a row failure is never
    caught here — it propagates as RowImportError so the caller's
    transaction.atomic() block rolls back everything already done in this
    attempt (no half-created test, no orphaned questions). Returns
    (question_ids_in_order, skipped_count, duplicate_count)."""
    course_list = list(batch.courses.all())
    rows = list(batch.rows.exclude(status='error').order_by('row_number'))
    question_ids = []
    skipped_count = 0
    duplicate_count = 0

    for row in rows:
        if row.status == 'duplicate' and row.dedup_action == 'skip':
            row.status = 'skipped'
            row.save(update_fields=['status'])
            skipped_count += 1
            continue

        if row.status == 'duplicate' and row.dedup_action == 'replace' and row.duplicate_of_id:
            old = Question.objects.filter(pk=row.duplicate_of_id).first()
            if old and not question_is_referenced(old):
                old.delete()
            elif old:
                row.warnings = (row.warnings or []) + ['Could not replace — the existing question is already used in a Test.']

        try:
            question = create_question_from_row(row.raw_data, batch, course_list)
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed error carrying the row number
            raise RowImportError(row.row_number, str(exc)) from exc

        row.created_question = question
        row.status = 'imported'
        if row.duplicate_of_id:
            duplicate_count += 1
        row.save(update_fields=['created_question', 'status', 'warnings'])
        question_ids.append(question.id)

    return question_ids, skipped_count, duplicate_count


class ImportBatchCreateTestView(APIView):
    """POST /import-batches/<id>/create-test/ — the "Import & Create Test"
    workflow: creates every eligible row's Question AND a new Test containing
    them (in import order), all inside one database transaction. If anything
    fails partway through, everything already written in this attempt is
    rolled back — no half-created test, no orphaned questions — and the
    failing row is flagged so the admin can fix it (via the existing
    ImportRowDetailView PATCH) and retry without re-uploading the file.

    Deliberately synchronous, unlike the background-thread /confirm/ flow:
    atomicity across a spawned thread and the request/response cycle doesn't
    compose cleanly, and "no half-completed test" is far simpler to
    guarantee inside one request-scoped transaction.atomic() block. The
    existing Import to Question Bank flow is completely untouched."""
    permission_classes = [IsAdminUser]

    def post(self, request, batch_id):
        try:
            batch = ImportBatch.objects.get(pk=batch_id)
        except ImportBatch.DoesNotExist:
            return Response({'detail': 'Batch not found.'}, status=404)

        # import_mode is recorded once, at upload time, from the Admin's
        # "Import Type" selector. A 'ready' batch has never been run —
        # nothing has been written for it yet — so status is the real
        # safety invariant and it's safe to proceed on the admin's evident
        # intent (they're calling this endpoint) even if import_mode was
        # somehow recorded as 'question_bank' at upload (a since-fixed
        # frontend race let the selector change while a file was still
        # uploading). A 'failed' batch is different: it can only be safely
        # retried here if it failed *inside this same synchronous,
        # transactional flow* (nothing partially committed, see
        # _create_questions_for_test's docstring) — a 'question_bank'-mode
        # batch that failed mid-run via the separate background /confirm/
        # flow may have already committed some rows' Questions, and
        # reprocessing those here would create duplicates.
        if batch.status == 'failed' and batch.import_mode != 'create_test':
            return Response({'detail': f'Batch is currently "{batch.status}" — cannot create a test.'}, status=400)
        if batch.status not in ('ready', 'failed'):
            return Response({'detail': f'Batch is currently "{batch.status}" — cannot create a test.'}, status=400)
        if not (batch.subject_id and batch.chapter_id and batch.topic_id):
            return Response({'detail': 'Please select Subject, Chapter and Topic before importing.'}, status=400)
        blocked = batch.rows.filter(status='duplicate', dedup_action='').exists()
        if blocked:
            return Response({'detail': 'Some duplicate rows still need a Skip/Replace/Keep Both decision before importing.'}, status=400)

        config = {k: v for k, v in request.data.items() if k != 'question_ids'}
        if not config.get('courses'):
            config['courses'] = list(batch.courses.values_list('id', flat=True))
        config['subject'] = batch.subject_id

        try:
            with transaction.atomic():
                question_ids, skipped_count, duplicate_count = _create_questions_for_test(batch)

                serializer = TestAdminSerializer(data={**config, 'question_ids': question_ids}, context={'request': request})
                serializer.is_valid(raise_exception=True)
                test = serializer.save()

                batch.created_test = test
                batch.status = 'completed'
                batch.import_mode = 'create_test'
                batch.created_count = len(question_ids)
                batch.skipped_count = skipped_count
                batch.duplicate_count = duplicate_count
                batch.completed_at = timezone.now()
                batch.save(update_fields=[
                    'created_test', 'status', 'import_mode', 'created_count', 'skipped_count',
                    'duplicate_count', 'completed_at',
                ])
        except RowImportError as exc:
            row = batch.rows.filter(row_number=exc.row_number).first()
            if row:
                row.status = 'error'
                row.errors = (row.errors or []) + [exc.reason]
                row.save(update_fields=['status', 'errors'])
            batch.status = 'failed'
            batch.save(update_fields=['status'])
            return Response({
                'detail': f'Import failed at row {exc.row_number}: {exc.reason}. No test was created.',
                'failed_row_number': exc.row_number,
            }, status=400)
        except ValidationError as exc:
            # The rows/questions were never touched (still inside the atomic
            # block when this raised) — only the Test Configuration form was
            # invalid, so the batch stays "ready" and the admin can just fix
            # the form and resubmit, no confusing "Import Failed" state.
            return Response({'detail': f'Test configuration is invalid: {exc.detail}'}, status=400)
        except Exception as exc:  # noqa: BLE001 - never leave the batch stuck in "ready" on an unexpected failure
            batch.status = 'failed'
            batch.save(update_fields=['status'])
            return Response({'detail': f'Could not create the test: {exc}'}, status=400)

        return Response({**_batch_summary(batch), 'test_id': test.id})


class ImportStatusView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request, batch_id):
        try:
            batch = ImportBatch.objects.get(pk=batch_id)
        except ImportBatch.DoesNotExist:
            return Response({'detail': 'Batch not found.'}, status=404)

        estimated_remaining_seconds = None
        if batch.status == 'importing' and batch.started_at and batch.processed_count:
            elapsed = (timezone.now() - batch.started_at).total_seconds()
            per_row = elapsed / batch.processed_count
            remaining_rows = max(batch.total_rows - batch.processed_count, 0)
            estimated_remaining_seconds = round(per_row * remaining_rows)

        return Response({**_batch_summary(batch), 'estimated_remaining_seconds': estimated_remaining_seconds})


class ImportRollbackView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, batch_id):
        try:
            batch = ImportBatch.objects.get(pk=batch_id)
        except ImportBatch.DoesNotExist:
            return Response({'detail': 'Batch not found.'}, status=404)
        if batch.status != 'completed':
            return Response({'detail': 'Only a completed batch can be rolled back.'}, status=400)

        imported_rows = batch.rows.filter(status='imported', created_question__isnull=False)
        referenced = [r for r in imported_rows if question_is_referenced(r.created_question)]
        if referenced:
            return Response({
                'detail': f'{len(referenced)} of this batch\'s questions have already been used in a Test and cannot be rolled back.',
            }, status=400)

        question_ids = list(imported_rows.values_list('created_question_id', flat=True))
        Question.objects.filter(id__in=question_ids).delete()
        batch.status = 'rolled_back'
        batch.save(update_fields=['status'])
        return Response({'detail': f'Rolled back {len(question_ids)} question(s).'})


class ImportTemplateView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request, file_format):
        if file_format == 'xlsx':
            from .importers.xlsx_parser import build_template_workbook
            import io
            wb = build_template_workbook()
            buf = io.BytesIO()
            wb.save(buf)
            buf.seek(0)
            response = HttpResponse(buf.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response['Content-Disposition'] = 'attachment; filename="question-import-template.xlsx"'
            return response
        if file_format == 'csv':
            from .importers.csv_parser import build_template_csv
            response = HttpResponse(build_template_csv(), content_type='text/csv')
            response['Content-Disposition'] = 'attachment; filename="question-import-template.csv"'
            return response
        if file_format == 'docx':
            from .importers.docx_parser import build_template_docx
            import io
            doc = build_template_docx()
            buf = io.BytesIO()
            doc.save(buf)
            buf.seek(0)
            response = HttpResponse(buf.read(), content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
            response['Content-Disposition'] = 'attachment; filename="question-import-template.docx"'
            return response
        if file_format == 'json':
            import json
            sample = json.dumps([{
                'text': '<p>A ball is thrown vertically upward. What is its acceleration at the highest point?</p>',
                'options': [
                    {'text': 'Zero', 'is_correct': False}, {'text': 'g, downward', 'is_correct': True},
                    {'text': 'g, upward', 'is_correct': False}, {'text': 'Depends on mass', 'is_correct': False},
                ],
                'explanation': '<p>At the highest point velocity is zero but gravity still acts downward.</p>',
                'past_exam_years': '2078,2080',
            }], indent=2)
            response = HttpResponse(sample, content_type='application/json')
            response['Content-Disposition'] = 'attachment; filename="question-import-template.json"'
            return response
        return Response({'detail': 'Unknown format.'}, status=404)
