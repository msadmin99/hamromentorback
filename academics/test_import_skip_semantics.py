"""Bulk Import: DELETE vs SKIP semantics on errored rows.

  DELETE = do not import this question (row removed from the batch).
  SKIP   = ignore this validation error and IMPORT THE QUESTION ANYWAY.

Root cause these tests pin down: "Skip" used to only stop an Error row from
blocking the batch, while every import path (Add to Exam / Create Test /
question-bank run_import) still excluded every status='error' row — so a
skipped question silently vanished. Skip must now import the row, but only
for errors that are safe to import through (import_validation.
BYPASSABLE_ISSUE_CODES), decided server-side from the row's own data.

Everything here goes through the real API endpoints (no mocking of the
importer), plus run_import() for the asynchronous question-bank path.
"""
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from academics.import_engine import run_import
from academics.models import Chapter, ImportBatch, ImportRow, Option, Question, Subject, Topic
from tests_app.models import TestQuestion

User = get_user_model()

LATEX_OPT = r'$\frac{G}{g}R_E^2$'


def _raw(n, options=None, text=None, explanation='<p>Because.</p>'):
    if options is None:
        options = [('A', True), ('B', False)]
    return {
        'text_html': f'<p>{text if text is not None else f"Question {n}"}</p>',
        'options': [{'text_html': f'<p>{t}</p>', 'is_correct': ok, 'image_path': ''} for t, ok in options],
        'explanation_html': explanation,
        'question_image_path': '', 'explanation_image_path': '',
    }


# The exact example from the bug report: two identical LaTeX options.
def _dup_latex_raw(n):
    return _raw(n, [(LATEX_OPT, True), (LATEX_OPT, False), (r'$\frac{G}{g}R_E$', False), (r'$\frac{G}{g}R_E^3$', False)],
                text='One can easily calculate the mass of the Earth by using the formula (in usual notation):')


class SkipSemanticsBase(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='staff1', email='staff1@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.client.force_authenticate(user=self.staff)
        self.subject = Subject.objects.create(name='Physics')
        self.chapter = Chapter.objects.create(subject=self.subject, name='Mechanics')
        self.topic = Topic.objects.create(chapter=self.chapter, name='Kinematics')
        self.batch = ImportBatch.objects.create(
            uploaded_by=self.staff, file_name='q.docx', file_format='docx', status='ready', total_rows=0,
            import_mode='question_bank', subject=self.subject, chapter=self.chapter, topic=self.topic,
            dedup_status='completed',
        )

    # ---- factories -------------------------------------------------------
    def row(self, n, status, raw, **kw):
        r = ImportRow.objects.create(batch=self.batch, row_number=n, status=status, raw_data=raw, **kw)
        self.batch.total_rows = self.batch.rows.count()
        self.batch.save(update_fields=['total_rows'])
        return r

    def valid(self, n):
        return self.row(n, 'valid', _raw(n))

    def dup_error(self, n, raw=None):
        raw = raw or _dup_latex_raw(n)
        return self.row(n, 'error', raw, errors=['Option 2 duplicates option 1 ("' + LATEX_OPT + '").'])

    def blank_error(self, n):
        return self.row(n, 'error', {'text_html': '', 'options': [
            {'text_html': '<p>A</p>', 'is_correct': True}, {'text_html': '<p>B</p>', 'is_correct': False}]},
            errors=['Question text is blank.'])

    # ---- API shortcuts ---------------------------------------------------
    def row_url(self, row):
        return f'/api/import-batches/{self.batch.id}/rows/{row.id}/'

    def skip(self, row):
        return self.client.patch(self.row_url(row), {'error_skipped': True}, format='json')

    def add_to_exam(self):
        return self.client.post(f'/api/import-batches/{self.batch.id}/create-questions/', {}, format='json')

    def status(self):
        return self.client.get(f'/api/import-batches/{self.batch.id}/status/').data


class DeleteTests(SkipSemanticsBase):
    def test_1_delete_excludes_the_row_and_it_is_never_created(self):
        keep = self.valid(1)
        bad = self.dup_error(2)

        resp = self.client.delete(self.row_url(bad))

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ImportRow.objects.filter(pk=bad.pk).exists())
        self.assertEqual(resp.data['total_rows'], 1)
        result = self.add_to_exam()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.data['question_ids']), 1)
        self.assertEqual(Question.objects.count(), 1)
        self.assertEqual(Question.objects.get().text, '<p>Question 1</p>')
        self.assertEqual(TestQuestion.objects.count(), 0)  # Add to Exam never writes TestQuestion itself
        keep.refresh_from_db()
        self.assertEqual(keep.status, 'imported')

    def test_delete_still_works_on_a_row_that_was_skipped(self):
        bad = self.dup_error(1)
        self.assertEqual(self.skip(bad).status_code, 200)

        resp = self.client.delete(self.row_url(bad))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.add_to_exam().data['question_ids'], [])


class SkipTests(SkipSemanticsBase):
    def test_2_skip_imports_the_question_and_its_options(self):
        bad = self.dup_error(1)
        self.assertEqual(bad.status, 'error')

        resp = self.skip(bad)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['error_skipped'])
        self.assertTrue(resp.data['bypass_eligible'])

        result = self.add_to_exam()

        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.data['question_ids']), 1)
        q = Question.objects.get(pk=result.data['question_ids'][0])
        self.assertEqual(q.options.count(), 4)
        bad.refresh_from_db()
        self.assertEqual(bad.status, 'imported')
        self.assertEqual(bad.created_question_id, q.id)
        self.assertTrue(bad.error_skipped)            # the decision is kept as an audit trail
        self.assertTrue(bad.errors)                    # ...and so is the error that was ignored

    def test_4_duplicate_latex_option_example_is_imported_unchanged(self):
        bad = self.dup_error(1)
        self.skip(bad)

        result = self.add_to_exam()

        q = Question.objects.get(pk=result.data['question_ids'][0])
        texts = list(q.options.order_by('order').values_list('text', flat=True))
        self.assertEqual(texts, [f'<p>{LATEX_OPT}</p>', f'<p>{LATEX_OPT}</p>', r'<p>$\frac{G}{g}R_E$</p>', r'<p>$\frac{G}{g}R_E^3$</p>'])
        self.assertEqual(texts[0], texts[1])          # the duplicated option is NOT "fixed"

    def test_5_source_preservation_text_latex_order_and_answer_key_unchanged(self):
        raw = _dup_latex_raw(1)
        bad = self.dup_error(1, raw)
        self.skip(bad)

        result = self.add_to_exam()

        q = Question.objects.get(pk=result.data['question_ids'][0])
        self.assertEqual(q.text, raw['text_html'])
        self.assertEqual(q.explanation, raw['explanation_html'])
        got = [(o.text, o.is_correct, o.order) for o in q.options.order_by('order')]
        want = [(o['text_html'], o['is_correct'], i) for i, o in enumerate(raw['options'])]
        self.assertEqual(got, want)
        bad.refresh_from_db()
        self.assertEqual(bad.raw_data, raw)           # the stored source itself is untouched

    def test_skip_does_not_alter_the_row_before_import(self):
        raw = _dup_latex_raw(1)
        bad = self.dup_error(1, raw)

        self.skip(bad)

        bad.refresh_from_db()
        self.assertEqual(bad.raw_data, raw)
        self.assertEqual(bad.status, 'error')
        self.assertTrue(bad.errors)

    def test_an_unresolved_error_is_still_not_imported(self):
        """Only an explicit Skip imports an error row; untouched errors stay out."""
        self.valid(1)
        untouched = self.dup_error(2)

        result = self.add_to_exam()

        self.assertEqual(len(result.data['question_ids']), 1)
        untouched.refresh_from_db()
        self.assertEqual(untouched.status, 'error')
        self.assertIsNone(untouched.created_question_id)

    def test_create_test_flow_also_imports_a_skipped_error(self):
        """Add to Exam and Create Test share one creation function; both must honor Skip."""
        bad = self.dup_error(1)
        self.skip(bad)
        from academics.import_views import _create_questions_for_test
        ids, skipped, dups = _create_questions_for_test(self.batch)
        self.assertEqual(len(ids), 1)


class MixedBatchTests(SkipSemanticsBase):
    def test_3_mixed_batch_valid_plus_skipped_imported_deleted_omitted(self):
        valid_rows = [self.valid(i) for i in range(1, 11)]                                   # 10 valid
        warn_rows = [self.row(i, 'warning', _raw(i), warnings=['No explanation provided.']) for i in (11, 12)]  # 2 warning
        skipped = self.dup_error(13)
        deleted = self.dup_error(14)

        self.assertEqual(self.skip(skipped).status_code, 200)
        self.assertEqual(self.client.delete(self.row_url(deleted)).status_code, 200)
        before = self.status()

        self.assertEqual(before['importable_count'], 13)      # 10 valid + 2 warning + 1 skipped error
        self.assertEqual(before['total_rows'], 13)            # the deleted row is gone
        self.assertEqual(before['unskipped_error_count'], 0)
        self.assertEqual(before['skipped_error_count'], 1)
        self.assertEqual(before['skipped_projected_count'], 0)  # a skipped error is NOT "omitted"

        result = self.add_to_exam()

        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.data['question_ids']), 13)
        self.assertEqual(Question.objects.count(), 13)
        skipped.refresh_from_db()
        self.assertEqual(skipped.status, 'imported')
        self.assertFalse(ImportRow.objects.filter(pk=deleted.pk).exists())
        self.assertEqual(result.data['created_count'], 13)


class CountersTests(SkipSemanticsBase):
    def test_counters_after_skip_undo_and_delete_stay_consistent(self):
        self.valid(1)
        a = self.dup_error(2)
        b = self.dup_error(3)
        s0 = self.status()
        self.assertEqual((s0['importable_count'], s0['unskipped_error_count'], s0['skipped_error_count']), (1, 2, 0))

        self.skip(a)
        s1 = self.status()
        self.assertEqual((s1['importable_count'], s1['unskipped_error_count'], s1['skipped_error_count']), (2, 1, 1))
        self.assertEqual(s1['row_counts']['error'], 2)          # raw classification is unchanged by Skip

        self.client.patch(self.row_url(a), {'error_skipped': False}, format='json')  # Undo Skip
        s2 = self.status()
        self.assertEqual((s2['importable_count'], s2['unskipped_error_count'], s2['skipped_error_count']), (1, 2, 0))

        self.skip(a)
        self.client.delete(self.row_url(b))
        s3 = self.status()
        self.assertEqual((s3['total_rows'], s3['importable_count'], s3['unskipped_error_count']), (2, 2, 0))

    def test_skip_survives_reading_the_rows_again(self):
        """Persistence: the decision lives on the row, not in UI state — listing
        the rows again (tab change / reopen / refresh) still shows it."""
        bad = self.dup_error(1)
        self.skip(bad)

        listing = self.client.get(f'/api/import-batches/{self.batch.id}/rows/?status=error').data['results']

        self.assertTrue(listing[0]['error_skipped'])
        self.assertTrue(listing[0]['bypass_eligible'])


class RevalidationTests(SkipSemanticsBase):
    def test_6_edit_that_fixes_the_duplicate_clears_skip_and_row_becomes_valid(self):
        bad = self.dup_error(1)
        self.skip(bad)
        fixed = _dup_latex_raw(1)
        fixed['options'][1]['text_html'] = r'<p>$\frac{g}{G}R_E^2$</p>'

        resp = self.client.patch(self.row_url(bad), {'data': fixed}, format='json')

        self.assertEqual(resp.data['status'], 'valid')
        self.assertFalse(resp.data['error_skipped'])   # nothing left to skip

    def test_6_unrelated_edit_keeps_an_explicit_skip(self):
        bad = self.dup_error(1)
        self.skip(bad)
        edited = _dup_latex_raw(1)
        edited['explanation_html'] = '<p>Edited explanation.</p>'

        resp = self.client.patch(self.row_url(bad), {'data': edited}, format='json')

        self.assertEqual(resp.data['status'], 'error')
        self.assertTrue(resp.data['error_skipped'])    # the decision does not silently disappear
        self.assertEqual(self.add_to_exam().status_code, 200)
        self.assertEqual(Question.objects.count(), 1)

    def test_6_an_edit_that_makes_the_error_unsafe_clears_the_skip(self):
        bad = self.dup_error(1)
        self.skip(bad)
        broken = _dup_latex_raw(1)
        broken['text_html'] = ''

        resp = self.client.patch(self.row_url(bad), {'data': broken}, format='json')

        self.assertEqual(resp.data['status'], 'error')
        self.assertFalse(resp.data['error_skipped'])
        self.assertFalse(resp.data['bypass_eligible'])


class UnsafeErrorTests(SkipSemanticsBase):
    def test_7_skip_is_refused_for_an_error_that_cannot_be_ignored(self):
        bad = self.blank_error(1)

        resp = self.skip(bad)

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'error_cannot_be_skipped')
        self.assertIn("can't be skipped", resp.data['detail'])
        bad.refresh_from_db()
        self.assertFalse(bad.error_skipped)

    def test_7_every_structural_error_is_unbypassable_but_duplicate_option_is(self):
        cases = {
            'blank question': ({'text_html': '', 'options': _raw(1)['options']}, False),
            'too few options': (_raw(1, [('A', True)]), False),
            'no correct option': (_raw(1, [('A', False), ('B', False)]), False),
            'duplicate option': (_raw(1, [('A', True), ('A', False)]), True),
        }
        for i, (name, (raw, ok)) in enumerate(cases.items(), start=1):
            row = self.row(i, 'error', raw, errors=['x'])
            self.assertEqual(self.skip(row).status_code, 200 if ok else 400, name)

    def test_7_bulk_skip_applies_only_to_safe_rows_and_reports_the_rest(self):
        safe = self.dup_error(1)
        unsafe = self.blank_error(2)

        resp = self.client.post(
            f'/api/import-batches/{self.batch.id}/rows/bulk-skip-error/',
            {'row_ids': [safe.id, unsafe.id]}, format='json',
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['applied_count'], 1)
        self.assertEqual([r['row_id'] for r in resp.data['rejected']], [unsafe.id])
        self.assertIn("can't be skipped", resp.data['rejected'][0]['reason'])
        safe.refresh_from_db(); unsafe.refresh_from_db()
        self.assertTrue(safe.error_skipped)
        self.assertFalse(unsafe.error_skipped)

    def test_7_a_tampered_skip_flag_cannot_create_a_corrupt_question(self):
        """Backend safety: even if a buggy/malicious client (or a direct DB
        write) sets error_skipped on an unimportable row, Add to Exam refuses
        loudly and creates NOTHING — it neither drops the row silently nor
        persists a blank-stem question."""
        good = self.valid(1)
        unsafe = self.blank_error(2)
        ImportRow.objects.filter(pk=unsafe.pk).update(error_skipped=True)   # bypasses the API guard

        resp = self.add_to_exam()

        self.assertEqual(resp.status_code, 400)
        self.assertIn("can't be skipped", resp.data['detail'])
        self.assertEqual(resp.data['failed_row_number'], 2)
        self.assertEqual(Question.objects.count(), 0)                  # all-or-nothing rollback
        good.refresh_from_db(); unsafe.refresh_from_db()
        self.assertIsNone(good.created_question_id)
        self.assertEqual(unsafe.status, 'error')
        self.assertFalse(unsafe.error_skipped)                          # stale decision withdrawn
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, 'failed')                   # retryable after the admin fixes it


class AsyncQuestionBankPathTests(SkipSemanticsBase):
    """The question-bank import (run_import) must honor Skip exactly like Add to Exam."""

    def test_run_import_creates_skipped_error_and_leaves_unresolved_error_out(self):
        valid = self.valid(1)
        skipped = self.dup_error(2)
        unresolved = self.dup_error(3)
        self.skip(skipped)

        run_import(self.batch.id)

        for r in (valid, skipped, unresolved):
            r.refresh_from_db()
        self.assertEqual(valid.status, 'imported')
        self.assertEqual(skipped.status, 'imported')
        self.assertIsNotNone(skipped.created_question_id)
        self.assertEqual(unresolved.status, 'error')
        self.assertIsNone(unresolved.created_question_id)
        self.assertEqual(Question.objects.count(), 2)
        self.batch.refresh_from_db()
        self.assertEqual((self.batch.created_count, self.batch.failed_count), (2, 0))

    def test_run_import_refuses_a_tampered_flag_on_an_unsafe_row(self):
        unsafe = self.blank_error(1)
        ImportRow.objects.filter(pk=unsafe.pk).update(error_skipped=True)

        run_import(self.batch.id)

        unsafe.refresh_from_db()
        self.assertEqual(Question.objects.count(), 0)
        self.assertEqual(unsafe.status, 'error')
        self.assertFalse(unsafe.error_skipped)
        self.batch.refresh_from_db()
        self.assertEqual((self.batch.created_count, self.batch.failed_count), (0, 1))


class OptionsCreatedTests(SkipSemanticsBase):
    def test_options_of_a_skipped_row_are_created_in_source_order(self):
        bad = self.dup_error(1)
        self.skip(bad)
        result = self.add_to_exam()
        q = Question.objects.get(pk=result.data['question_ids'][0])
        self.assertEqual(Option.objects.filter(question=q).count(), 4)
        self.assertEqual([o.order for o in q.options.order_by('order')], [0, 1, 2, 3])
        self.assertEqual([o.is_correct for o in q.options.order_by('order')], [True, False, False, False])
