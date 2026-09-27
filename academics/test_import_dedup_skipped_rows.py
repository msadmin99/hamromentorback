"""Bulk Import: skipped ("import anyway") errors take part in duplicate detection,
and every row is re-validated at import time.

Skip means "acknowledge THIS validation error and still import the question". It
does not mean "bypass every other check". A skipped error row is going to become a
question, so — unlike an unresolved error — it must be checked against the question
bank and the rest of the file, and if it duplicates one it follows the normal
duplicate workflow (an explicit Skip / Replace / Keep Both decision). Separately, an
import-time safety net re-validates every row about to be created, whatever its
status, so nothing structurally broken can be persisted.

Stage 3 (rows edited AFTER a duplicate decision) is deliberately not covered here.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from academics import import_views
from academics.import_engine import run_import
from academics.import_views import _run_dedup
from academics.models import Chapter, ImportBatch, ImportRow, Option, Question, Subject, Topic
from tests_app.models import Test, TestQuestion

User = get_user_model()

STEM = r'<p>One can easily calculate the mass of the Earth by using the formula (in usual notation):</p>'
OPTS = [r'$\frac{g}{G} R_E^2$', r'$\frac{G}{g} R_E^2$', r'$\frac{G}{g} R_E$', r'$\frac{G}{g} R_E^3$']
# Same four distinct options plus one repeated option: the repeat is a validation error
# that may be skipped, while the option SET still equals the bank question's, so the row
# is also a duplicate of it.
DUP_OPTS = [OPTS[0], OPTS[1], OPTS[1], OPTS[2], OPTS[3]]


# Genuinely different stems, so filler rows are never duplicates of each other.
_TOPICS = [
    'lens focal length and image formation', 'photoelectric effect threshold frequency', 'carnot engine efficiency limits',
    'kirchhoff loop rules in circuits', 'projectile range at forty five degrees', 'doppler shift of a moving source',
    'moment of inertia of a solid sphere', 'bernoulli principle in a narrow pipe', 'radioactive decay half life problem',
    'capacitor charging time constant', 'simple pendulum small angle period', 'refraction through a glass prism',
    'ideal gas isothermal expansion work', 'magnetic force on a moving charge', 'standing waves on a stretched string',
    'escape velocity from a planet surface',
]


def distinct_stem(i):
    return f'<p>{_TOPICS[i % len(_TOPICS)]}</p>'


def raw(options, stem=STEM):
    return {
        'text_html': stem,
        'options': [{'text_html': f'<p>{t}</p>', 'is_correct': i == 0, 'image_path': ''} for i, t in enumerate(options)],
        'explanation_html': '<p>Because.</p>', 'question_image_path': '', 'explanation_image_path': '',
    }


class DedupSkippedBase(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='staff1', email='staff1@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.client.force_authenticate(user=self.staff)
        self.subject = Subject.objects.create(name='Physics')
        self.chapter = Chapter.objects.create(subject=self.subject, name='Gravitation')
        self.topic = Topic.objects.create(chapter=self.chapter, name='Earth')
        self.existing = Question.objects.create(subject=self.subject, chapter=self.chapter, topic=self.topic, text=STEM)
        for i, t in enumerate(OPTS):
            Option.objects.create(question=self.existing, text=f'<p>{t}</p>', order=i, is_correct=i == 0)
        self.batch = ImportBatch.objects.create(
            uploaded_by=self.staff, file_name='q.docx', file_format='docx', status='ready', total_rows=0,
            import_mode='question_bank', subject=self.subject, chapter=self.chapter, topic=self.topic,
            dedup_status='completed',
        )

    # ---- factories / shortcuts ------------------------------------------------------
    def row(self, n, status, data, **kw):
        r = ImportRow.objects.create(batch=self.batch, row_number=n, status=status, raw_data=data, **kw)
        self.batch.total_rows = self.batch.rows.count()
        self.batch.save(update_fields=['total_rows'])
        return r

    def unique_valid(self, n):
        return self.row(n, 'valid', raw(['Alpha', 'Beta'], stem=distinct_stem(n)))

    def error_row(self, n, data=None):
        return self.row(n, 'error', data or raw(DUP_OPTS), errors=['Option 3 duplicates option 2.'])

    def url(self, r):
        return f'/api/import-batches/{self.batch.id}/rows/{r.id}/'

    def skip(self, r):
        return self.client.patch(self.url(r), {'error_skipped': True}, format='json')

    def decide(self, r, action):
        return self.client.patch(self.url(r), {'dedup_action': action}, format='json')

    def add_to_exam(self):
        return self.client.post(f'/api/import-batches/{self.batch.id}/create-questions/', {}, format='json')

    def created(self):
        return Question.objects.exclude(pk=self.existing.pk)


class SkippedDuplicateWorkflowTests(DedupSkippedBase):
    def test_1_a_skipped_error_that_duplicates_the_bank_enters_the_duplicate_workflow(self):
        bad = self.error_row(1)

        resp = self.skip(bad)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'duplicate')            # normal duplicate workflow
        self.assertEqual(resp.data['duplicate_of_id'], self.existing.id)
        self.assertTrue(resp.data['error_skipped'])                    # its Skip decision is kept
        self.assertTrue(resp.data['errors'])                           # ...and so is the ignored error
        self.assertEqual(resp.data['dedup_action'], '')                # an explicit decision is now required
        result = self.add_to_exam()
        self.assertEqual(result.status_code, 400)
        self.assertIn('Skip/Replace/Keep Both', result.data['detail'])
        self.assertEqual(self.created().count(), 0)                    # nothing was silently created

    def test_2_skip_decision_reuses_the_existing_question_and_creates_no_duplicate(self):
        bad = self.error_row(1)
        self.skip(bad)
        self.decide(bad, 'skip')

        result = self.add_to_exam()

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data['question_ids'], [self.existing.id])   # the existing one is attached
        self.assertEqual(Question.objects.count(), 1)                        # no new Question Bank record
        bad.refresh_from_db()
        self.assertEqual(bad.status, 'skipped')
        self.assertEqual(bad.created_question_id, self.existing.id)

    def test_3_keep_both_creates_exactly_one_new_question_with_the_source_unchanged(self):
        bad = self.error_row(1)
        self.skip(bad)
        self.decide(bad, 'keep_both')

        result = self.add_to_exam()

        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.created().count(), 1)
        q = self.created().get()
        self.assertEqual(result.data['question_ids'], [q.id])
        self.assertEqual(q.text, STEM)
        got = [(o.text, o.is_correct, o.order) for o in q.options.order_by('order')]
        want = [(f'<p>{t}</p>', i == 0, i) for i, t in enumerate(DUP_OPTS)]
        self.assertEqual(got, want)                                          # order, text, LaTeX, answer key
        self.assertEqual(self.existing.options.count(), 4)                   # the existing question is untouched
        bad.refresh_from_db()
        self.assertEqual(bad.status, 'imported')
        self.assertEqual(bad.raw_data, raw(DUP_OPTS))

    def test_4_replace_deletes_the_old_question_and_creates_the_row_content(self):
        bad = self.error_row(1)
        self.skip(bad)
        self.decide(bad, 'replace')
        old_id = self.existing.id

        result = self.add_to_exam()

        self.assertEqual(result.status_code, 200)
        self.assertFalse(Question.objects.filter(pk=old_id).exists())
        self.assertEqual(Question.objects.count(), 1)
        self.assertEqual(Question.objects.get().options.count(), 5)

    def test_4_replace_keeps_an_already_referenced_question(self):
        test = Test.objects.create(title='T', created_by=self.staff)
        TestQuestion.objects.create(test=test, question=self.existing, order=0)
        bad = self.error_row(1)
        self.skip(bad)
        self.decide(bad, 'replace')

        result = self.add_to_exam()

        self.assertEqual(result.status_code, 200)
        self.assertTrue(Question.objects.filter(pk=self.existing.pk).exists())   # in use -> never deleted
        self.assertEqual(self.created().count(), 1)


class DedupGateTests(DedupSkippedBase):
    def test_add_to_exam_is_refused_until_dedup_has_completed(self):
        self.unique_valid(1)
        for status in ('', 'pending', 'processing'):
            ImportBatch.objects.filter(pk=self.batch.pk).update(dedup_status=status)
            resp = self.add_to_exam()
            self.assertEqual(resp.status_code, 400, status)
            self.assertIn('Duplicate check is still in progress', resp.data['detail'])
        self.assertEqual(Question.objects.count(), 1)

    def test_add_to_exam_runs_once_dedup_is_completed(self):
        self.unique_valid(1)
        self.assertEqual(self.add_to_exam().status_code, 200)

    def test_5_a_bulk_skip_hands_off_to_the_background_run_and_import_stays_blocked(self):
        rows = [self.error_row(i, raw(['Alpha', 'Alpha', 'Beta'], stem=distinct_stem(i)))
                for i in range(1, import_views.TARGETED_DEDUP_SYNC_LIMIT + 2)]
        ids = [r.id for r in rows]

        with patch.object(import_views, 'enqueue_dedup_task') as enqueue:
            resp = self.client.post(
                f'/api/import-batches/{self.batch.id}/rows/bulk-skip-error/', {'row_ids': ids}, format='json',
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data['applied_count'], len(ids))
            self.assertEqual(resp.data['dedup_status'], 'pending')
            enqueue.assert_called_once()
            self.assertEqual(enqueue.call_args.args[0], self.batch.id)
            self.assertEqual(enqueue.call_args.args[1], self.batch.dedup_generation + 1)   # a fresh generation
            blocked = self.add_to_exam()

        self.assertEqual(blocked.status_code, 400)
        self.assertIn('Duplicate check is still in progress', blocked.data['detail'])
        self.assertEqual(Question.objects.count(), 1)

    def test_5_when_the_background_run_completes_the_skipped_rows_are_checked_and_import_unblocks(self):
        dup = self.error_row(1)
        rows = [dup] + [self.error_row(i, raw(['Alpha', 'Alpha'], stem=distinct_stem(i)))
                        for i in range(2, import_views.TARGETED_DEDUP_SYNC_LIMIT + 2)]

        resp = self.client.post(       # DEDUP_PROCESSING_ASYNC is off in tests -> the run completes inline
            f'/api/import-batches/{self.batch.id}/rows/bulk-skip-error/', {'row_ids': [r.id for r in rows]}, format='json',
        )

        self.assertEqual(resp.data['dedup_status'], 'completed')
        dup.refresh_from_db()
        self.assertEqual(dup.status, 'duplicate')                    # the full run includes skipped rows
        self.assertEqual(self.add_to_exam().status_code, 400)        # ...and still demands a decision for it
        self.decide(dup, 'keep_both')
        self.assertEqual(self.add_to_exam().status_code, 200)


class DuplicateClearedTests(DedupSkippedBase):
    def test_6_when_the_duplicate_condition_clears_a_skipped_error_returns_to_a_skipped_error_not_valid(self):
        bad = self.error_row(1)
        self.skip(bad)
        bad.refresh_from_db()
        self.assertEqual(bad.status, 'duplicate')
        self.existing.delete()                                        # the question it duplicated is gone

        _run_dedup(self.batch)

        bad.refresh_from_db()
        self.assertEqual(bad.status, 'error')                        # recomputed from the remaining validation state
        self.assertTrue(bad.error_skipped)                           # SKIPPED is restored
        self.assertIsNone(bad.duplicate_of_id)
        self.assertEqual(bad.dedup_action, '')
        self.assertEqual(len(bad.errors), 1)                         # the ignored error is still recorded
        self.assertNotEqual(bad.status, 'valid')
        summary = self.client.get(f'/api/import-batches/{self.batch.id}/status/').data
        self.assertEqual((summary['skipped_error_count'], summary['unskipped_error_count'], summary['importable_count']), (1, 0, 1))

    def test_6_a_restored_skipped_error_is_then_importable_as_a_normal_skipped_error(self):
        bad = self.error_row(1)
        self.skip(bad)
        self.existing.delete()
        _run_dedup(self.batch)

        result = self.add_to_exam()

        self.assertEqual(result.status_code, 200)
        self.assertEqual(Question.objects.count(), 1)

    def test_6_a_clean_row_still_returns_to_valid_when_its_duplicate_flag_clears(self):
        """The long-standing behaviour for ordinary rows is unchanged."""
        row = self.row(1, 'valid', raw(OPTS))
        _run_dedup(self.batch)
        row.refresh_from_db()
        self.assertEqual(row.status, 'duplicate')
        self.existing.delete()

        _run_dedup(self.batch)

        row.refresh_from_db()
        self.assertIn(row.status, ('valid', 'warning'))

    def test_6_a_cleared_duplicate_with_a_structural_error_is_an_unskipped_error(self):
        row = self.row(1, 'duplicate', raw([OPTS[0]]), duplicate_of=self.existing, error_skipped=True,
                       errors=['x'], warnings=['100% similar to an existing question.'])
        self.existing.delete()

        _run_dedup(self.batch)

        row.refresh_from_db()
        self.assertEqual(row.status, 'error')
        self.assertFalse(row.error_skipped)                          # a skip can't cover a structural error
        self.assertTrue(any('option' in e.lower() for e in row.errors))


class IntraBatchTests(DedupSkippedBase):
    def test_7_a_skipped_row_that_duplicates_another_row_in_the_file_is_flagged(self):
        Question.objects.filter(pk=self.existing.pk).delete()        # isolate the in-file case
        keep = self.row(1, 'valid', raw(OPTS))
        skipped = self.error_row(2)

        self.skip(skipped)

        skipped.refresh_from_db()
        self.assertEqual(skipped.status, 'duplicate')                # matched against row 1 (in-file)
        self.assertIsNone(skipped.duplicate_of_id)                   # no bank question involved
        keep.refresh_from_db()
        self.assertEqual(keep.status, 'valid')                       # targeted run: only the skipped row was evaluated

    def test_7_a_full_run_flags_both_rows_of_an_in_file_pair(self):
        Question.objects.filter(pk=self.existing.pk).delete()
        keep = self.row(1, 'valid', raw(OPTS))
        skipped = self.row(2, 'error', raw(DUP_OPTS), errors=['x'], error_skipped=True)

        _run_dedup(self.batch)

        keep.refresh_from_db(); skipped.refresh_from_db()
        self.assertEqual((keep.status, skipped.status), ('duplicate', 'duplicate'))

    def test_9_an_unresolved_error_is_neither_a_candidate_nor_a_comparison_target(self):
        Question.objects.filter(pk=self.existing.pk).delete()
        unresolved = self.row(1, 'error', raw(DUP_OPTS), errors=['x'])          # not skipped
        twin = self.row(2, 'valid', raw(OPTS))

        _run_dedup(self.batch)

        unresolved.refresh_from_db(); twin.refresh_from_db()
        self.assertEqual(unresolved.status, 'error')                 # excluded from dedup
        self.assertIsNone(unresolved.duplicate_of_id)
        self.assertEqual(twin.status, 'valid')                       # and not matched against it
        result = self.add_to_exam()                                  # the unresolved error is simply left out
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data['question_ids'], [Question.objects.get().id])
        unresolved.refresh_from_db()
        self.assertIsNone(unresolved.created_question_id)


class SafetyNetTests(DedupSkippedBase):
    def broken_duplicate(self, n, action='keep_both'):
        """A duplicate row (decision made) that was then edited into an unimportable state."""
        return self.row(n, 'duplicate', {'text_html': '', 'options': [{'text_html': '<p>only</p>', 'is_correct': True}]},
                        duplicate_of=self.existing, dedup_action=action, errors=['Question text is blank.'])

    def test_8_add_to_exam_refuses_a_broken_duplicate_row_and_creates_nothing(self):
        ok = self.unique_valid(1)
        bad = self.broken_duplicate(2)

        resp = self.add_to_exam()

        self.assertEqual(resp.status_code, 400)
        self.assertIn("can't be imported", resp.data['detail'])
        self.assertEqual(resp.data['failed_row_number'], 2)
        self.assertEqual(self.created().count(), 0)                  # all-or-nothing, no broken question persisted
        ok.refresh_from_db(); bad.refresh_from_db()
        self.assertIsNone(ok.created_question_id)
        self.assertEqual(bad.status, 'error')

    def test_8_create_test_path_also_refuses_a_broken_duplicate_row(self):
        self.broken_duplicate(1)
        with self.assertRaises(import_views.RowImportError) as ctx:
            import_views._create_questions_for_test(self.batch)
        self.assertEqual(ctx.exception.row_number, 1)

    def test_8_run_import_refuses_a_broken_duplicate_row_but_imports_the_rest(self):
        ok = self.unique_valid(1)
        bad = self.broken_duplicate(2)

        run_import(self.batch.id)

        ok.refresh_from_db(); bad.refresh_from_db()
        self.assertEqual(ok.status, 'imported')
        self.assertEqual(bad.status, 'error')
        self.assertIsNone(bad.created_question_id)
        self.assertTrue(any("can't be imported" in e for e in bad.errors))
        self.assertEqual(self.created().count(), 1)
        self.batch.refresh_from_db()
        self.assertEqual((self.batch.created_count, self.batch.failed_count), (1, 1))

    def test_8_a_refused_replace_never_deletes_the_old_question(self):
        self.broken_duplicate(1, action='replace')
        run_import(self.batch.id)
        self.assertTrue(Question.objects.filter(pk=self.existing.pk).exists())
        self.assertEqual(self.created().count(), 0)

    def test_8_a_duplicate_row_with_an_unresolved_bypassable_error_is_refused(self):
        row = self.row(1, 'duplicate', raw(DUP_OPTS), duplicate_of=self.existing, dedup_action='keep_both',
                       errors=['Option 3 duplicates option 2.'])          # error_skipped=False

        resp = self.add_to_exam()

        self.assertEqual(resp.status_code, 400)
        self.assertIn('unresolved validation error', resp.data['detail'])
        row.refresh_from_db()
        self.assertEqual(self.created().count(), 0)

    def test_8_a_duplicate_row_decided_skip_is_not_validated_because_nothing_new_is_created(self):
        self.broken_duplicate(1, action='skip')
        resp = self.add_to_exam()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['question_ids'], [self.existing.id])
        self.assertEqual(Question.objects.count(), 1)

    def test_8_valid_and_warning_rows_are_unaffected_by_the_net(self):
        self.unique_valid(1)
        self.row(2, 'warning', raw(['Gamma', 'Delta'], stem='<p>Another unrelated stem about magnets</p>'),
                 warnings=['No explanation provided.'])
        resp = self.add_to_exam()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['question_ids']), 2)


class TargetedDedupPerformanceTests(DedupSkippedBase):
    def test_10_only_the_newly_skipped_rows_are_compared_not_the_whole_batch(self):
        skip_me = [self.error_row(i, raw(['Alpha', 'Alpha'], stem=distinct_stem(i))) for i in (1, 2)]
        unresolved = [self.error_row(i, raw(['Beta', 'Beta'], stem=distinct_stem(i + 8))) for i in (3, 4, 5)]
        valid = [self.unique_valid(i) for i in (6, 7, 8, 9)]

        with patch.object(import_views, 'find_duplicate', wraps=import_views.find_duplicate) as spy, \
             patch.object(import_views, 'enqueue_dedup_task') as enqueue, \
             patch.object(import_views, '_run_dedup', wraps=import_views._run_dedup) as run:
            for r in skip_me:
                self.assertEqual(self.skip(r).status_code, 200)

        self.assertEqual(spy.call_count, 2)                          # 2 skips -> 2 comparisons, not 9 rows' worth
        enqueue.assert_not_called()                                  # no background/full re-run
        self.assertTrue(all(c.kwargs.get('only_row_ids') for c in run.call_args_list))
        self.assertEqual(sorted(c.kwargs['only_row_ids'].pop() for c in run.call_args_list), sorted(r.id for r in skip_me))
        for r in unresolved + valid:
            r.refresh_from_db()
        self.assertTrue(all(r.status == 'error' for r in unresolved))     # untouched
        self.assertTrue(all(r.status == 'valid' for r in valid))

    def test_10_a_skip_that_is_not_new_does_not_trigger_any_dedup(self):
        row = self.error_row(1, raw(['Alpha', 'Alpha'], stem=distinct_stem(3)))
        self.skip(row)
        with patch.object(import_views, 'find_duplicate') as spy, patch.object(import_views, 'enqueue_dedup_task') as enqueue:
            self.skip(row)                                            # already skipped
            self.client.patch(self.url(row), {'error_skipped': False}, format='json')   # undo is not a new skip
        spy.assert_not_called()
        enqueue.assert_not_called()

    def test_10_no_subject_means_nothing_to_compare_and_no_dedup_is_triggered(self):
        ImportBatch.objects.filter(pk=self.batch.pk).update(subject=None)
        row = self.error_row(1, raw(['Alpha', 'Alpha'], stem=distinct_stem(3)))
        with patch.object(import_views, 'find_duplicate') as spy, patch.object(import_views, 'enqueue_dedup_task') as enqueue:
            self.assertEqual(self.skip(row).status_code, 200)
        spy.assert_not_called()
        enqueue.assert_not_called()

    def test_targeted_run_leaves_rows_outside_the_target_untouched(self):
        other = self.row(1, 'duplicate', raw(OPTS), duplicate_of=self.existing, dedup_action='keep_both',
                         warnings=['100% similar to an existing question.'])
        target = self.error_row(2)
        self.skip(target)
        other.refresh_from_db()
        self.assertEqual((other.status, other.dedup_action), ('duplicate', 'keep_both'))     # decision preserved
