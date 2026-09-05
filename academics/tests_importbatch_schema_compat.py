"""Tests for the ImportBatch dedup-column compatibility migration
(0027_importbatch_dedup_compat_defaults) — the upload-500 schema-drift
fix. This is NOT a test of the (deliberately unfinished, out-of-scope)
async-dedup feature; it only proves the migration correctly restores
database-level defaults for the two columns confirmed, via direct
information_schema inspection of the actual staging database, to be
simultaneously (a) NOT NULL with no default and (b) unknown to the
currently-deployed ImportBatch model: dedup_generation and dedup_status.

Kept in its own file, separate from academics/tests.py, so this isolated
compatibility-fix work never has to be reconciled with that file's large
amount of unrelated in-progress changes.
"""
import importlib
import unittest

from django.db import connection
from django.test import TestCase

from academics.models import ImportBatch

_migration = importlib.import_module('academics.migrations.0027_importbatch_dedup_compat_defaults')

TABLE = 'academics_importbatch'


def _column_default(name):
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT COLUMN_DEFAULT FROM information_schema.columns '
            'WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s',
            [TABLE, name],
        )
        row = cursor.fetchone()
        return row[0] if row else None


def _column_exists(name):
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT COUNT(*) FROM information_schema.columns '
            'WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s',
            [TABLE, name],
        )
        return cursor.fetchone()[0] > 0


@unittest.skipUnless(
    connection.vendor == 'mysql',
    'the fix itself is MySQL-only (same guard pattern as migration 0024\'s FULLTEXT index) — '
    'this project\'s local/CI default is SQLite (see hamromentor/settings.py), so these run only '
    'when tests execute against a real MySQL database',
)
class CompatibilityMigrationMySQLTests(TestCase):
    """Each test starts by adding dedup_generation/dedup_status to this
    test database's own academics_importbatch table as NOT NULL columns
    with no persisted default — deliberately reproducing the real staging
    drift from scratch, rather than assuming any particular migration set
    already produced it, so this test is meaningful regardless of which
    other migrations happen to be present."""

    def setUp(self):
        with connection.cursor() as cursor:
            cursor.execute(f'ALTER TABLE {TABLE} ADD COLUMN dedup_generation INT UNSIGNED NOT NULL')
            cursor.execute(f'ALTER TABLE {TABLE} ADD COLUMN dedup_status VARCHAR(15) NOT NULL')

    def tearDown(self):
        with connection.cursor() as cursor:
            for column in ('dedup_generation', 'dedup_status', 'unrelated_not_null_column'):
                if _column_exists(column):
                    cursor.execute(f'ALTER TABLE {TABLE} DROP COLUMN {column}')

    def test_case_a_missing_default_is_restored_for_both_columns(self):
        self.assertIsNone(_column_default('dedup_generation'))
        self.assertIsNone(_column_default('dedup_status'))

        with connection.schema_editor() as schema_editor:
            _migration.restore_defaults(None, schema_editor)

        self.assertEqual(_column_default('dedup_generation'), '0')
        self.assertEqual(_column_default('dedup_status'), '')

    def test_case_b_reapplying_is_idempotent(self):
        with connection.schema_editor() as schema_editor:
            _migration.restore_defaults(None, schema_editor)  # first run: case A -> fixed
        with connection.schema_editor() as schema_editor:
            _migration.restore_defaults(None, schema_editor)  # second run: case B, must not error or change anything

        self.assertEqual(_column_default('dedup_generation'), '0')
        self.assertEqual(_column_default('dedup_status'), '')

    def test_case_c_a_missing_column_is_a_no_op_and_does_not_recreate_it(self):
        with connection.cursor() as cursor:
            cursor.execute(f'ALTER TABLE {TABLE} DROP COLUMN dedup_generation')

        with connection.schema_editor() as schema_editor:
            _migration.restore_defaults(None, schema_editor)  # must not error, must not recreate dedup_generation

        self.assertFalse(_column_exists('dedup_generation'))
        # the OTHER target column, which does exist, is still fixed correctly
        self.assertEqual(_column_default('dedup_status'), '')

    def test_fully_fresh_schema_with_neither_column_is_a_total_no_op(self):
        """The scenario that matters most before ever considering this for
        production: a database that never received the out-of-band dedup
        migration at all, so BOTH target columns are absent (not just
        one, as in the case-C test above). Must be a completely silent
        no-op — no error, no column created, no table touched — proving
        this fix stays independent of whether the unfinished async-dedup
        schema exists anywhere."""
        with connection.cursor() as cursor:
            cursor.execute(f'ALTER TABLE {TABLE} DROP COLUMN dedup_generation')
            cursor.execute(f'ALTER TABLE {TABLE} DROP COLUMN dedup_status')

        with connection.schema_editor() as schema_editor:
            _migration.restore_defaults(None, schema_editor)  # must not error or create either column

        self.assertFalse(_column_exists('dedup_generation'))
        self.assertFalse(_column_exists('dedup_status'))

        # and the ordinary, already-working path must still work exactly
        # as it does on a database that never had this drift at all
        batch = ImportBatch.objects.create(
            file_name='fresh-schema.csv', file_format='csv', status='validating', import_mode='question_bank',
        )
        self.assertIsNotNone(batch.pk)

    def test_untargeted_columns_are_never_touched(self):
        """Guards against a naive implementation that fixes anything
        matching a pattern rather than the exact, proven-necessary two
        columns (dedup_claimed_at/dedup_completed_at/processing_claimed_at
        are all nullable and correctly out of scope; this proves an
        unrelated NOT NULL column with no default is left alone too)."""
        with connection.cursor() as cursor:
            cursor.execute(f'ALTER TABLE {TABLE} ADD COLUMN unrelated_not_null_column INT NOT NULL')

        with connection.schema_editor() as schema_editor:
            _migration.restore_defaults(None, schema_editor)

        self.assertIsNone(_column_default('unrelated_not_null_column'))

    def test_reverse_restores_pre_migration_state(self):
        with connection.schema_editor() as schema_editor:
            _migration.restore_defaults(None, schema_editor)
        with connection.schema_editor() as schema_editor:
            _migration.drop_defaults(None, schema_editor)

        self.assertIsNone(_column_default('dedup_generation'))
        self.assertIsNone(_column_default('dedup_status'))

    def test_existing_row_data_is_never_altered(self):
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {TABLE} (file_name, file_format, status, import_mode, total_rows, "
                "created_count, failed_count, skipped_count, duplicate_count, created_at, "
                "dedup_generation, dedup_status) "
                "VALUES ('pre-existing.csv', 'csv', 'ready', 'question_bank', 3, 0, 0, 0, 0, NOW(), 7, 'completed')"
            )

        with connection.schema_editor() as schema_editor:
            _migration.restore_defaults(None, schema_editor)

        batch = ImportBatch.objects.get(file_name='pre-existing.csv')
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT dedup_generation, dedup_status FROM {TABLE} WHERE id = %s', [batch.pk])
            generation, dedup_status = cursor.fetchone()
        self.assertEqual(generation, 7)  # the migration only changes the column DEFAULT, never existing values
        self.assertEqual(dedup_status, 'completed')

    def test_import_batch_create_succeeds_after_fix(self):
        """The actual end-to-end proof: the CURRENTLY DEPLOYED ImportBatch
        model (no dedup_generation/dedup_status fields declared at all)
        can insert a row without error once the migration has run — this
        is the literal upload-500 bug being fixed."""
        with connection.schema_editor() as schema_editor:
            _migration.restore_defaults(None, schema_editor)

        batch = ImportBatch.objects.create(
            file_name='test.csv', file_format='csv', status='validating', import_mode='question_bank',
        )

        self.assertIsNotNone(batch.pk)
        self.assertEqual(batch.status, 'validating')
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT dedup_generation, dedup_status FROM {TABLE} WHERE id = %s', [batch.pk])
            generation, dedup_status = cursor.fetchone()
        self.assertEqual(generation, 0)
        self.assertEqual(dedup_status, '')

    def test_import_batch_create_fails_without_fix(self):
        """Negative control: proves setUp actually reproduces the real
        bug (and that this test suite would catch a regression in the fix
        itself) — without calling restore_defaults, the exact same
        create() call raises, matching the real MySQL 1364 error."""
        from django.db import DatabaseError
        with self.assertRaises(DatabaseError):
            ImportBatch.objects.create(
                file_name='should-fail.csv', file_format='csv', status='validating', import_mode='question_bank',
            )


class CompatibilityMigrationVendorGuardTests(TestCase):
    """Runs on every backend, including this project's local/CI default
    (SQLite) — proves the migration is a safe, silent no-op wherever the
    MySQL-only fix doesn't apply, rather than erroring or attempting
    unsupported SQL, and that it doesn't disturb the ordinary case (a
    database that never had the dedup columns at all)."""

    def test_no_op_on_non_mysql_backend(self):
        if connection.vendor == 'mysql':
            self.skipTest('this test is specifically about the non-MySQL guard')
        # A bare stand-in exposing only `.connection` — restore_defaults()/
        # drop_defaults() check schema_editor.connection.vendor and return
        # immediately on non-MySQL, before ever calling schema_editor.execute()
        # or opening a cursor, so nothing more elaborate is needed here.
        # (Django's real schema_editor() context manager itself refuses to
        # open on SQLite mid-transaction — irrelevant to what's actually
        # being tested: that the vendor check short-circuits correctly.)
        fake_schema_editor = type('FakeSchemaEditor', (), {'connection': connection})()
        _migration.restore_defaults(None, fake_schema_editor)  # must not raise
        _migration.drop_defaults(None, fake_schema_editor)  # must not raise

    def test_ordinary_import_batch_creation_still_works(self):
        """Regression guard: this migration must never break the already-
        working case — a database that never had the dedup columns."""
        batch = ImportBatch.objects.create(
            file_name='ordinary.csv', file_format='csv', status='validating', import_mode='question_bank',
        )
        self.assertIsNotNone(batch.pk)
