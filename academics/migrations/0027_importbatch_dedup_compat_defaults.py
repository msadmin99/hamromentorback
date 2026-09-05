"""Compatibility migration (upload-500 schema-drift fix — NOT part of the
unfinished async-dedup feature): the currently-deployed ImportBatch model
has no dedup_generation/dedup_status fields at all, but the actual
database (confirmed via direct information_schema inspection, not
assumed from migration files — see the schema-drift audit report) already
has both as NOT NULL columns with no persisted database-level default.
Django's own AddField migration behavior only applies a field's
`default=` to backfill EXISTING rows at migration time, then drops the
SQL-level DEFAULT, expecting the ORM model to supply the value on every
future insert. Since today's deployed ImportBatch class doesn't declare
these fields, its generated INSERT omits them entirely, and MySQL rejects
it (1364: "Field '...' doesn't have a default value").

This migration does NOT add dedup_generation/dedup_status (or any other
field) to the ImportBatch model, does NOT touch any other dedup-related
column, and does NOT assume the columns exist on every database — it
inspects information_schema first and is a no-op wherever a column is
absent or already carries the intended default. It only ever changes a
column's DEFAULT clause, never its type, nullability, or the data in
existing rows.

Numbering: this depends only on 0020 (the last committed migration) and
is deliberately numbered 0027, not 0021, even though 0021 is the "next"
slot by dependency. Migrations 0021-0026 already exist in the working
tree as part of the not-yet-committed async-dedup feature (including the
very 0025 that created the two columns this migration is fixing defaults
for) — reusing 0021 for this unrelated, independently-releasable fix
would collide with those filenames the moment both are ever present
together (e.g. in a teammate's checkout, or once 0021-0026 are finally
committed), even though Django's actual migration graph is defined by
`dependencies` below, not by filename. Numbering this 0027 (one past the
highest number currently on disk) avoids that collision outright. Since
this file's *real* dependency is 0020, not 0026, academics will have two
independent graph leaves once 0021-0026 are committed — that's
intentional: it forces an explicit `makemigrations --merge` at that
point, which is the right moment for a human to confirm this fix's
assumptions (a column that already exists with no default) still hold
once the real dedup model fields land, rather than having that
interaction silently glossed over.
"""
from django.db import migrations

# Only these two columns are known to be BOTH (a) NOT NULL with no
# database-level default and (b) absent from the currently-deployed
# ImportBatch model — confirmed via direct information_schema inspection
# of the staging database. The three other dedup-related columns
# (dedup_claimed_at, dedup_completed_at, processing_claimed_at) are all
# nullable and therefore harmless even when the deployed model omits them
# from an INSERT — deliberately left untouched, not "fixed" alongside
# these two just because they share a migration.
TARGET_DEFAULTS = {
    'dedup_generation': '0',
    # '' matches the field's own intended meaning elsewhere in the
    # codebase ("blank until a Subject is first selected").
    'dedup_status': "''",
}

TABLE_NAME = 'academics_importbatch'


def _column_info(cursor, table, column):
    """Returns (is_nullable, column_default) or None if the column
    doesn't exist on this database at all."""
    cursor.execute(
        'SELECT IS_NULLABLE, COLUMN_DEFAULT FROM information_schema.columns '
        'WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s',
        [table, column],
    )
    return cursor.fetchone()


def restore_defaults(apps, schema_editor):
    # SQLite (local dev/tests) has no equivalent ALTER COLUMN ... SET
    # DEFAULT syntax and never received migrations 0021-0026 in the first
    # place — same MySQL-only guard pattern as 0024's FULLTEXT index.
    if schema_editor.connection.vendor != 'mysql':
        return
    with schema_editor.connection.cursor() as cursor:
        for column, default_sql in TARGET_DEFAULTS.items():
            info = _column_info(cursor, TABLE_NAME, column)
            if info is None:
                continue  # column doesn't exist on this database — nothing to fix (case C)
            _is_nullable, current_default = info
            if current_default is not None:
                continue  # already has a database-level default — nothing to do (case B)
            schema_editor.execute(
                f'ALTER TABLE {TABLE_NAME} ALTER COLUMN {column} SET DEFAULT {default_sql}'
            )


def drop_defaults(apps, schema_editor):
    """Reverse: restores the pre-migration state (no database-level
    default). Never touches existing row data — a row that already has a
    non-NULL value keeps it; only the column's own DEFAULT clause for
    future inserts changes."""
    if schema_editor.connection.vendor != 'mysql':
        return
    with schema_editor.connection.cursor() as cursor:
        for column in TARGET_DEFAULTS:
            info = _column_info(cursor, TABLE_NAME, column)
            if info is None:
                continue
            schema_editor.execute(f'ALTER TABLE {TABLE_NAME} ALTER COLUMN {column} DROP DEFAULT')


class Migration(migrations.Migration):

    # ALTER TABLE ... ALTER COLUMN ... SET DEFAULT is DDL that MySQL/InnoDB
    # commits implicitly and can't roll back — same restriction migration
    # 0024's FULLTEXT index already documents. Confirmed by an actual
    # staging run: without this, Django refuses with
    # TransactionManagementError before executing any SQL at all. SQLite
    # (local/tests) never reaches schema_editor.execute() at all (the
    # vendor guard returns first), so this has no effect there.
    atomic = False

    dependencies = [
        ('academics', '0020_alter_questionevent_source'),
    ]

    operations = [
        migrations.RunPython(restore_defaults, drop_defaults),
    ]
