"""Bulk-import Preview & Validate audit — Features 1, 2, 4, 6.

Covers:
  1. Bulk duplicate actions (select-all-style, applied server-side):
     Skip / Replace / Keep Both / Remove, hard-scoped to status='duplicate'
     regardless of what row_ids the client sends.
  2. Bulk (and individual) Error skip/undo-skip, hard-scoped to
     status='error', never mutating `status`/`errors`/raw data.
  4. Revalidation after edit — already-existing coverage in
     ImportRowDeleteTests.test_patch_can_fix_an_error_row_to_valid is
     extended here for the error_skipped-clearing behavior specifically.
  6. Import-summary counts (skipped_projected_count, unskipped_error_count)
     stay accurate and never double-count a row.

Reuses this app's existing test conventions (APITestCase, admin_role=
'admin' staff user, ImportBatch/ImportRow factory helpers) rather than
inventing a new pattern.
"""
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from academics.models import Chapter, ImportBatch, ImportRow, Subject, Topic

User = get_user_model()


def _mk_row(batch, row_number, status, **overrides):
    defaults = dict(
        batch=batch, row_number=row_number, status=status,
        raw_data={
            'text_html': f'<p>Q{row_number}</p>',
            'options': [{'text_html': 'A', 'is_correct': True}, {'text_html': 'B', 'is_correct': False}],
            'explanation_html': '<p>Because.</p>',
        },
    )
    defaults.update(overrides)
    return ImportRow.objects.create(**defaults)


class BulkImportActionsTestCase(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='staff1', email='staff1@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.client.force_authenticate(user=self.staff)
        self.subject = Subject.objects.create(name='Physics')
        self.chapter = Chapter.objects.create(subject=self.subject, name='Mechanics')
        self.topic = Topic.objects.create(chapter=self.chapter, name='Kinematics')
        self.batch = ImportBatch.objects.create(
            uploaded_by=self.staff, file_name='q.xlsx', file_format='xlsx', status='ready', total_rows=0,
            subject=self.subject, chapter=self.chapter, topic=self.topic,
        )

    def _dedup_url(self):
        return f'/api/import-batches/{self.batch.id}/rows/bulk-dedup-action/'

    def _skip_error_url(self):
        return f'/api/import-batches/{self.batch.id}/rows/bulk-skip-error/'


# --- Feature 1: bulk duplicate actions --------------------------------

class BulkDuplicateSkipTests(BulkImportActionsTestCase):
    def test_bulk_skip_applies_to_every_selected_duplicate(self):
        d1 = _mk_row(self.batch, 1, 'duplicate')
        d2 = _mk_row(self.batch, 2, 'duplicate')
        d3 = _mk_row(self.batch, 3, 'duplicate')

        resp = self.client.post(self._dedup_url(), {'row_ids': [d1.id, d2.id, d3.id], 'action': 'skip'}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['applied_count'], 3)
        for row in (d1, d2, d3):
            row.refresh_from_db()
            self.assertEqual(row.dedup_action, 'skip')
            self.assertEqual(row.status, 'duplicate')  # dedup_action only — status flips at import time


class BulkDuplicateDeselectTests(BulkImportActionsTestCase):
    def test_deselecting_one_row_before_apply_leaves_it_untouched(self):
        """Covers: select all duplicates, then deselect one before
        applying — the frontend simply omits it from row_ids; the
        untouched row must keep its prior (blank) dedup_action."""
        d1 = _mk_row(self.batch, 1, 'duplicate')
        d2 = _mk_row(self.batch, 2, 'duplicate')

        resp = self.client.post(self._dedup_url(), {'row_ids': [d1.id], 'action': 'keep_both'}, format='json')

        self.assertEqual(resp.status_code, 200)
        d1.refresh_from_db()
        d2.refresh_from_db()
        self.assertEqual(d1.dedup_action, 'keep_both')
        self.assertEqual(d2.dedup_action, '')


class BulkDuplicateReplaceTests(BulkImportActionsTestCase):
    def test_bulk_replace_sets_dedup_action_for_every_selected_row(self):
        d1 = _mk_row(self.batch, 1, 'duplicate')
        d2 = _mk_row(self.batch, 2, 'duplicate')

        resp = self.client.post(self._dedup_url(), {'row_ids': [d1.id, d2.id], 'action': 'replace'}, format='json')

        self.assertEqual(resp.status_code, 200)
        d1.refresh_from_db()
        d2.refresh_from_db()
        self.assertEqual(d1.dedup_action, 'replace')
        self.assertEqual(d2.dedup_action, 'replace')


class BulkDuplicateKeepBothTests(BulkImportActionsTestCase):
    def test_bulk_keep_both_sets_dedup_action_for_every_selected_row(self):
        d1 = _mk_row(self.batch, 1, 'duplicate')
        d2 = _mk_row(self.batch, 2, 'duplicate')

        resp = self.client.post(self._dedup_url(), {'row_ids': [d1.id, d2.id], 'action': 'keep_both'}, format='json')

        self.assertEqual(resp.status_code, 200)
        d1.refresh_from_db()
        d2.refresh_from_db()
        self.assertEqual(d1.dedup_action, 'keep_both')
        self.assertEqual(d2.dedup_action, 'keep_both')


class BulkDuplicateRemoveTests(BulkImportActionsTestCase):
    def test_bulk_remove_deletes_rows_and_decrements_total(self):
        self.batch.total_rows = 3
        self.batch.save(update_fields=['total_rows'])
        d1 = _mk_row(self.batch, 1, 'duplicate')
        d2 = _mk_row(self.batch, 2, 'duplicate')
        v1 = _mk_row(self.batch, 3, 'valid')

        resp = self.client.post(self._dedup_url(), {'row_ids': [d1.id, d2.id], 'action': 'remove'}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['applied_count'], 2)
        self.assertFalse(ImportRow.objects.filter(id__in=[d1.id, d2.id]).exists())
        self.assertTrue(ImportRow.objects.filter(id=v1.id).exists())
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.total_rows, 1)


class BulkDuplicateNeverTouchesNonDuplicateRowsTests(BulkImportActionsTestCase):
    """Feature 1, requirement 3: selection must NEVER affect Valid/Warning/
    Error rows — enforced server-side, not just by the frontend checkbox
    logic, so a stale or malicious row_ids list can't cross the boundary."""

    def test_valid_warning_and_error_rows_are_silently_excluded_from_a_bulk_dedup_action(self):
        dup = _mk_row(self.batch, 1, 'duplicate')
        valid = _mk_row(self.batch, 2, 'valid')
        warning = _mk_row(self.batch, 3, 'warning')
        error = _mk_row(self.batch, 4, 'error', errors=['Question text is blank.'])

        resp = self.client.post(
            self._dedup_url(),
            {'row_ids': [dup.id, valid.id, warning.id, error.id], 'action': 'skip'},
            format='json',
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['applied_count'], 1)  # only the duplicate row
        for row in (valid, warning, error):
            row.refresh_from_db()
            self.assertEqual(row.dedup_action, '')
        dup.refresh_from_db()
        self.assertEqual(dup.dedup_action, 'skip')

    def test_error_rows_are_unaffected_by_a_bulk_duplicate_remove(self):
        dup = _mk_row(self.batch, 1, 'duplicate')
        error = _mk_row(self.batch, 2, 'error', errors=['No option is marked correct.'])

        resp = self.client.post(self._dedup_url(), {'row_ids': [dup.id, error.id], 'action': 'remove'}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ImportRow.objects.filter(id=dup.id).exists())
        self.assertTrue(ImportRow.objects.filter(id=error.id).exists())


class BulkDedupActionValidationTests(BulkImportActionsTestCase):
    def test_unknown_action_is_rejected(self):
        dup = _mk_row(self.batch, 1, 'duplicate')
        resp = self.client.post(self._dedup_url(), {'row_ids': [dup.id], 'action': 'delete_forever'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_empty_row_ids_is_rejected(self):
        resp = self.client.post(self._dedup_url(), {'row_ids': [], 'action': 'skip'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_blocked_once_import_has_started(self):
        dup = _mk_row(self.batch, 1, 'duplicate')
        self.batch.status = 'importing'
        self.batch.save(update_fields=['status'])

        resp = self.client.post(self._dedup_url(), {'row_ids': [dup.id], 'action': 'skip'}, format='json')

        self.assertEqual(resp.status_code, 400)
        dup.refresh_from_db()
        self.assertEqual(dup.dedup_action, '')

    def test_requires_staff(self):
        student = User.objects.create_user(username='student1', email='student1@example.com', password='pw12345')
        self.client.force_authenticate(user=student)
        dup = _mk_row(self.batch, 1, 'duplicate')

        resp = self.client.post(self._dedup_url(), {'row_ids': [dup.id], 'action': 'skip'}, format='json')

        self.assertIn(resp.status_code, (401, 403))


# --- Feature 2: bulk (and individual) error skip ------------------------

class BulkErrorSkipTests(BulkImportActionsTestCase):
    def test_select_all_errors_then_bulk_skip_marks_every_error_row(self):
        e1 = _mk_row(self.batch, 1, 'error', errors=['Question text is blank.'])
        e2 = _mk_row(self.batch, 2, 'error', errors=['No option is marked correct.'])
        valid = _mk_row(self.batch, 3, 'valid')

        resp = self.client.post(self._skip_error_url(), {'row_ids': [e1.id, e2.id]}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['applied_count'], 2)
        for row in (e1, e2):
            row.refresh_from_db()
            self.assertTrue(row.error_skipped)
            self.assertEqual(row.status, 'error')  # status untouched, per its own field docstring
            self.assertTrue(row.errors)  # errors list untouched — never deletes the question
        valid.refresh_from_db()
        self.assertFalse(valid.error_skipped)

    def test_undo_skip_restores_the_row(self):
        e1 = _mk_row(self.batch, 1, 'error', errors=['Question text is blank.'], error_skipped=True)

        resp = self.client.post(self._skip_error_url(), {'row_ids': [e1.id], 'skipped': False}, format='json')

        self.assertEqual(resp.status_code, 200)
        e1.refresh_from_db()
        self.assertFalse(e1.error_skipped)

    def test_valid_and_duplicate_rows_are_excluded_from_bulk_skip_error(self):
        error = _mk_row(self.batch, 1, 'error', errors=['x'])
        dup = _mk_row(self.batch, 2, 'duplicate')

        resp = self.client.post(self._skip_error_url(), {'row_ids': [error.id, dup.id]}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['applied_count'], 1)
        dup.refresh_from_db()
        self.assertFalse(dup.error_skipped)

    def test_individual_skip_via_row_patch(self):
        error = _mk_row(self.batch, 1, 'error', errors=['x'])

        resp = self.client.patch(
            f'/api/import-batches/{self.batch.id}/rows/{error.id}/', {'error_skipped': True}, format='json',
        )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['error_skipped'])
        error.refresh_from_db()
        self.assertTrue(error.error_skipped)

    def test_individual_undo_skip_via_row_patch(self):
        error = _mk_row(self.batch, 1, 'error', errors=['x'], error_skipped=True)

        resp = self.client.patch(
            f'/api/import-batches/{self.batch.id}/rows/{error.id}/', {'error_skipped': False}, format='json',
        )

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['error_skipped'])

    def test_cannot_skip_a_non_error_row_individually(self):
        valid = _mk_row(self.batch, 1, 'valid')

        resp = self.client.patch(
            f'/api/import-batches/{self.batch.id}/rows/{valid.id}/', {'error_skipped': True}, format='json',
        )

        self.assertEqual(resp.status_code, 400)


class ErrorSkipRevalidationTests(BulkImportActionsTestCase):
    """Feature 4: fixing a skipped error row's underlying data clears the
    stale Skipped flag once it's no longer an error at all (see the
    ImportRowDetailView.patch comment) — a subtly different scenario from
    ImportRowDeleteTests.test_patch_can_fix_an_error_row_to_valid, which
    doesn't involve error_skipped."""

    def test_fixing_a_skipped_error_row_clears_the_skip_flag(self):
        row = _mk_row(
            self.batch, 1, 'error',
            raw_data={'text_html': '<p>Q1</p>', 'options': [{'text_html': 'A', 'is_correct': True}]},
            errors=['Only 1 option(s) found — at least 2 are required.'],
            error_skipped=True,
        )
        fixed_data = {
            'text_html': '<p>Q1</p>',
            'options': [{'text_html': 'A', 'is_correct': True}, {'text_html': 'B', 'is_correct': False}],
            'explanation_html': '<p>Because A is right.</p>',
        }

        resp = self.client.patch(f'/api/import-batches/{self.batch.id}/rows/{row.id}/', {'data': fixed_data}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'valid')
        self.assertFalse(resp.data['error_skipped'])

    def test_a_still_broken_edit_keeps_the_existing_skip_flag(self):
        """Editing one field but leaving the row still an Error must not
        silently un-skip it — only becoming non-error clears the flag."""
        row = _mk_row(
            self.batch, 1, 'error',
            raw_data={'text_html': '', 'options': [{'text_html': 'A', 'is_correct': True}]},
            errors=['Question text is blank.', 'Only 1 option(s) found — at least 2 are required.'],
            error_skipped=True,
        )
        still_broken_data = {'text_html': '<p>Now has text</p>', 'options': [{'text_html': 'A', 'is_correct': True}]}

        resp = self.client.patch(f'/api/import-batches/{self.batch.id}/rows/{row.id}/', {'data': still_broken_data}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'error')
        self.assertTrue(resp.data['error_skipped'])


# --- ids_only selection support -----------------------------------------

class SelectAllIdsOnlyTests(BulkImportActionsTestCase):
    def test_ids_only_returns_every_matching_row_across_pages_unpaginated(self):
        dup_ids = [_mk_row(self.batch, i, 'duplicate').id for i in range(1, 8)]
        _mk_row(self.batch, 8, 'valid')

        resp = self.client.get(f'/api/import-batches/{self.batch.id}/rows/?status=duplicate&ids_only=1')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sorted(resp.data['ids']), sorted(dup_ids))

    def test_error_skipped_is_included_in_the_normal_paginated_row_payload(self):
        row = _mk_row(self.batch, 1, 'error', errors=['x'], error_skipped=True)

        resp = self.client.get(f'/api/import-batches/{self.batch.id}/rows/')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['results'][0]['id'], row.id)
        self.assertTrue(resp.data['results'][0]['error_skipped'])


# --- Feature 6: import summary counts ------------------------------------

class ImportSummaryCountsTests(BulkImportActionsTestCase):
    def test_counts_match_the_documented_79_question_scenario(self):
        self.batch.total_rows = 79
        self.batch.save(update_fields=['total_rows'])
        for i in range(29):
            _mk_row(self.batch, i, 'valid')
        for i in range(29, 79):
            _mk_row(self.batch, i, 'duplicate')

        resp = self.client.get(f'/api/import-batches/{self.batch.id}/status/')

        self.assertEqual(resp.data['total_rows'], 79)
        self.assertEqual(resp.data['row_counts']['valid'], 29)
        self.assertEqual(resp.data['row_counts']['warning'], 0)
        self.assertEqual(resp.data['row_counts']['error'], 0)
        self.assertEqual(resp.data['row_counts']['duplicate'], 50)

    def test_mixed_state_scenario_with_skip_replace_keep_both_and_error_skip(self):
        """79 total: 25 valid, 10 warning, 30 duplicate, 14 error. Then:
        skip 10 duplicates, replace 5, keep_both 15 (accounting for all 30),
        skip 10 of the 14 errors. Verifies the final counts are correct
        and nothing is double-counted."""
        self.batch.total_rows = 79
        self.batch.save(update_fields=['total_rows'])
        for i in range(25):
            _mk_row(self.batch, i, 'valid')
        for i in range(25, 35):
            _mk_row(self.batch, i, 'warning', warnings=['No explanation provided.'])
        dup_ids = [_mk_row(self.batch, i, 'duplicate').id for i in range(35, 65)]
        error_ids = [_mk_row(self.batch, i, 'error', errors=['x']).id for i in range(65, 79)]

        self.client.post(self._dedup_url(), {'row_ids': dup_ids[:10], 'action': 'skip'}, format='json')
        self.client.post(self._dedup_url(), {'row_ids': dup_ids[10:15], 'action': 'replace'}, format='json')
        self.client.post(self._dedup_url(), {'row_ids': dup_ids[15:30], 'action': 'keep_both'}, format='json')
        self.client.post(self._skip_error_url(), {'row_ids': error_ids[:10]}, format='json')

        resp = self.client.get(f'/api/import-batches/{self.batch.id}/status/')
        data = resp.data

        self.assertEqual(data['total_rows'], 79)
        self.assertEqual(data['row_counts']['valid'], 25)
        self.assertEqual(data['row_counts']['warning'], 10)
        self.assertEqual(data['row_counts']['duplicate'], 30)  # classification unchanged by dedup_action choice
        self.assertEqual(data['row_counts']['error'], 14)  # classification unchanged by error_skipped
        self.assertEqual(data['duplicate_skip_count'], 10)
        self.assertEqual(data['skipped_error_count'], 10)
        self.assertEqual(data['unskipped_error_count'], 4)
        self.assertEqual(data['skipped_projected_count'], 20)  # 10 duplicate-skip + 10 error-skip, no double count

    def test_skipped_projected_count_is_zero_on_a_fresh_batch(self):
        _mk_row(self.batch, 1, 'valid')
        _mk_row(self.batch, 2, 'duplicate')
        _mk_row(self.batch, 3, 'error', errors=['x'])

        resp = self.client.get(f'/api/import-batches/{self.batch.id}/status/')

        self.assertEqual(resp.data['skipped_projected_count'], 0)
        self.assertEqual(resp.data['unskipped_error_count'], 1)
