"""Part A/C diagnostic reproduction — real DOCX upload -> real duplicate
detection -> real API response shape, exactly as the production admin
would experience it (same `DEDUP_PROCESSING_ASYNC` value as production:
unset/False, confirmed via `gcloud run services describe` against the
actual deployed backend service — see the accompanying report).

This is written as instrumentation/evidence first, per the task's own
"do not immediately change the algorithm" instruction — every assertion
here either PASSES (proving that part of the pipeline is fine) or FAILS
with a clear message naming the exact broken step, rather than any
assumption about where the break is.
"""
import io

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from docx import Document as DocxDocument
from rest_framework.test import APITestCase

from academics.models import Chapter, ImportBatch, ImportRow, Option, Question, Subject, Topic

User = get_user_model()


def _build_docx(lines):
    doc = DocxDocument()
    for line in lines:
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


class DuplicateDetectionRealFlowTests(APITestCase):
    """One real duplicate example, traced through the exact production
    request sequence: upload -> select Subject (triggers dedup) -> read
    back the rows. Mirrors what an admin does in the browser."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='staff1', email='staff1@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.client.force_authenticate(user=self.staff)
        self.subject = Subject.objects.create(name='Physics')
        self.chapter = Chapter.objects.create(subject=self.subject, name='Mechanics')
        self.topic = Topic.objects.create(chapter=self.chapter, name='Kinematics')

        # The "already in the question bank" side of the duplicate pair —
        # created directly as a Question, the way a PRIOR import or manual
        # entry would have left it (plain <p>-wrapped text/options, no
        # docx_parser involvement at all for this side).
        self.existing_question = Question.objects.create(
            subject=self.subject, chapter=self.chapter, topic=self.topic,
            text='<p>What is the acceleration due to gravity at the highest point of a vertically thrown ball?</p>',
        )
        Option.objects.create(question=self.existing_question, text='<p>Zero</p>', order=0, is_correct=False)
        Option.objects.create(question=self.existing_question, text='<p>g, downward</p>', order=1, is_correct=True)
        Option.objects.create(question=self.existing_question, text='<p>g, upward</p>', order=2, is_correct=False)
        Option.objects.create(question=self.existing_question, text='<p>Depends on mass</p>', order=3, is_correct=False)

    def _upload_docx(self, lines):
        buf = _build_docx(lines)
        return self.client.post(
            '/api/import-batches/upload/',
            {'file': SimpleUploadedFile(
                'q.docx', buf.read(),
                content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            )},
            format='multipart',
        )

    def test_the_same_mcq_reuploaded_via_a_real_docx_is_flagged_duplicate_end_to_end(self):
        # 1. Real DOCX, parsed via the real docx_parser (word-for-word the
        #    same question already in the DB above).
        upload_resp = self._upload_docx([
            'Q1. What is the acceleration due to gravity at the highest point of a vertically thrown ball?',
            'A) Zero', 'B) g, downward', 'C) g, upward', 'D) Depends on mass',
            'Answer: B',
        ])
        self.assertEqual(upload_resp.status_code, 201, upload_resp.data)
        batch_id = upload_resp.data['id']

        # Sanity check documenting the real, by-design pipeline: dedup is
        # deferred until Subject selection, so nothing is 'duplicate' yet.
        self.assertEqual(upload_resp.data['row_counts']['duplicate'], 0)
        row = ImportRow.objects.get(batch_id=batch_id)
        self.assertIn(row.status, ('valid', 'warning'))  # 'warning' expected: no explanation in this fixture

        # 2. Selecting Subject/Chapter/Topic — the exact production
        #    request that triggers _run_dedup (via enqueue_dedup_task,
        #    which runs synchronously in-process here because
        #    DEDUP_PROCESSING_ASYNC is False in this settings module by
        #    default — verified this matches production, which never sets
        #    that env var on the deployed Cloud Run service either).
        taxonomy_resp = self.client.patch(
            f'/api/import-batches/{batch_id}/taxonomy/',
            {'subject_id': self.subject.id, 'chapter_id': self.chapter.id, 'topic_id': self.topic.id, 'course_ids': []},
            format='json',
        )
        self.assertEqual(taxonomy_resp.status_code, 200, taxonomy_resp.data)

        # 3. Ground truth: did _run_dedup actually mark the ImportRow?
        row.refresh_from_db()
        self.assertEqual(
            row.status, 'duplicate',
            f'find_duplicate()/​_run_dedup did not flag a word-for-word identical question as a duplicate. '
            f'row.warnings={row.warnings!r}',
        )
        self.assertEqual(row.duplicate_of_id, self.existing_question.id)

        # 4. THE ACTUAL API CONTRACT the frontend receives — Part C.
        #    row_counts.duplicate queries ImportRow fresh from the DB via
        #    batch.rows (a related manager, not a cached field), so it
        #    must already be correct in this SAME response regardless of
        #    dedup_status below.
        self.assertEqual(
            taxonomy_resp.data['row_counts']['duplicate'], 1,
            f'row_counts under-reports the real, already-committed duplicate row. Full response: {taxonomy_resp.data}',
        )

        # 5. dedup_status in THIS SAME response — this is the field
        #    PreviewStep.js's dedupComplete check and the "Duplicate check
        #    in progress" banner key off. Regression guard for the
        #    stale-in-memory-batch-object bug fixed in
        #    ImportBatchTaxonomyView.patch() (batch.refresh_from_db()
        #    after enqueue_dedup_task()) — when DEDUP_PROCESSING_ASYNC is
        #    off (confirmed via `gcloud run services describe` to be the
        #    actual, real value on the deployed backend), dedup already
        #    ran synchronously and fully completed by this point, so the
        #    response must reflect that, not the 'pending' value set
        #    moments earlier in the same request.
        self.assertEqual(
            taxonomy_resp.data['dedup_status'], 'completed',
            f'Stale dedup_status returned even though the duplicate was already correctly detected and '
            f'committed (row.status={row.status!r}, row_counts.duplicate=1 in the same response). Full '
            f'response: {taxonomy_resp.data}',
        )

        # 6. The row-list endpoint the "Duplicate" tab actually calls —
        #    ground truth for what PreviewStep.js's Duplicates filter shows.
        rows_resp = self.client.get(f'/api/import-batches/{batch_id}/rows/?status=duplicate')
        self.assertEqual(rows_resp.status_code, 200)
        self.assertEqual(
            rows_resp.data['total'], 1,
            f'GET .../rows/?status=duplicate — the exact call the Duplicates tab makes — does not return the '
            f'row PreviewStep.js should display. Full response: {rows_resp.data}',
        )
        self.assertEqual(rows_resp.data['results'][0]['duplicate_of_id'], self.existing_question.id)

    def test_two_copies_of_the_same_mcq_inside_one_docx_are_cross_detected(self):
        """Part D, case 2: the same MCQ appears twice in one file (no
        existing DB question involved at all) — the in-batch comparison
        path in find_duplicate()/_run_dedup, not the existing-questions
        path."""
        upload_resp = self._upload_docx([
            'Q1. Molecular weight of a tribasic acid is W. Its equivalent weight is:',
            'A) W/2', 'B) W/3', 'C) W', 'D) 3W',
            'Answer: B',
            'Q2. Molecular weight of a tribasic acid is W. Its equivalent weight is:',
            'A) W/2', 'B) W/3', 'C) W', 'D) 3W',
            'Answer: B',
        ])
        self.assertEqual(upload_resp.status_code, 201, upload_resp.data)
        batch_id = upload_resp.data['id']
        self.assertEqual(upload_resp.data['total_rows'], 2)

        self.client.patch(
            f'/api/import-batches/{batch_id}/taxonomy/',
            {'subject_id': self.subject.id, 'chapter_id': self.chapter.id, 'topic_id': self.topic.id, 'course_ids': []},
            format='json',
        )

        rows = list(ImportRow.objects.filter(batch_id=batch_id).order_by('row_number'))
        self.assertEqual(len(rows), 2)
        statuses = [r.status for r in rows]
        self.assertIn(
            'duplicate', statuses,
            f'Neither of two identical in-batch rows was flagged duplicate. Row statuses: {statuses}',
        )

    def test_harmless_word_formatting_differences_still_match(self):
        """Part D, case 3: bold/whitespace/paragraph differences that a
        real re-typed or re-copied Word question commonly picks up must
        not defeat normalize_text()'s tag-stripping."""
        upload_resp = self._upload_docx([
            'Q1. What   is the acceleration due to gravity at the highest point of a vertically thrown ball?',
            'A) Zero', 'B) g, downward', 'C) g, upward', 'D) Depends on mass',
            'Answer: B',
        ])
        batch_id = upload_resp.data['id']
        self.client.patch(
            f'/api/import-batches/{batch_id}/taxonomy/',
            {'subject_id': self.subject.id, 'chapter_id': self.chapter.id, 'topic_id': self.topic.id, 'course_ids': []},
            format='json',
        )

        row = ImportRow.objects.get(batch_id=batch_id)
        self.assertEqual(
            row.status, 'duplicate',
            'Collapsed whitespace alone (normalize_text\'s own documented job) defeated duplicate matching.',
        )

    def test_status_poll_immediately_after_taxonomy_patch_self_heals_the_stale_dedup_status(self):
        """Confirms (or refutes) that TaxonomyPanel.js's poll-until-
        completed fallback actually recovers from the stale dedup_status
        in the PATCH response itself, within one poll — i.e. whether the
        bug above is cosmetic/transient or actually blocks the UI."""
        upload_resp = self._upload_docx([
            'Q1. What is the acceleration due to gravity at the highest point of a vertically thrown ball?',
            'A) Zero', 'B) g, downward', 'C) g, upward', 'D) Depends on mass',
            'Answer: B',
        ])
        batch_id = upload_resp.data['id']
        taxonomy_resp = self.client.patch(
            f'/api/import-batches/{batch_id}/taxonomy/',
            {'subject_id': self.subject.id, 'chapter_id': self.chapter.id, 'topic_id': self.topic.id, 'course_ids': []},
            format='json',
        )
        generation = taxonomy_resp.data['dedup_generation']

        # Exactly what TaxonomyPanel.js's poll() does immediately after.
        status_resp = self.client.get(f'/api/import-batches/{batch_id}/status/')

        self.assertEqual(status_resp.data['dedup_generation'], generation)
        self.assertEqual(
            status_resp.data['dedup_status'], 'completed',
            f'The very next /status/ poll still does not see dedup as completed: {status_resp.data}',
        )

    def test_same_stem_but_meaningfully_different_options_is_not_flagged_duplicate(self):
        """Documents real, by-design behavior (see import_dedup.py's own
        module docstring): a question is only flagged a duplicate when
        BOTH the stem AND the option set cross SIMILARITY_THRESHOLD,
        specifically to avoid template-reused-stem false positives
        ("Normality of X M solution of Y?" reused across many genuinely
        different questions). An admin who visually recognizes two
        questions as "the same" despite reworded distractors will see
        this as correctly-conservative, not broken — this test exists so
        that distinction is backed by evidence, not asserted from theory."""
        upload_resp = self._upload_docx([
            'Q1. What is the acceleration due to gravity at the highest point of a vertically thrown ball?',
            'A) None at all', 'B) Same as g, pointing toward Earth', 'C) Same as g, pointing away from Earth', 'D) Cannot be determined',
            'Answer: B',
        ])
        batch_id = upload_resp.data['id']

        self.client.patch(
            f'/api/import-batches/{batch_id}/taxonomy/',
            {'subject_id': self.subject.id, 'chapter_id': self.chapter.id, 'topic_id': self.topic.id, 'course_ids': []},
            format='json',
        )

        row = ImportRow.objects.get(batch_id=batch_id)
        self.assertNotEqual(
            row.status, 'duplicate',
            'A reworded-options question was flagged duplicate despite the options-similarity gate — this '
            'would be a real regression in the intentional anti-false-positive design.',
        )
