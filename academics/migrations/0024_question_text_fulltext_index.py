"""Scalability audit Phase B: QuestionViewSet's search filter used to run
Q(text__icontains=...) — a leading-wildcard LIKE — against the whole
academics_question table on every search request, forcing a full table
scan (confirmed via EXPLAIN: type=ALL, ~29,500 rows examined at 30K
questions, scaling linearly and getting worse toward 100K+). A MySQL
InnoDB FULLTEXT index on `text` lets QuestionViewSet.get_queryset() use a
real MATCH...AGAINST lookup instead for the common case, with the
original full-scan kept as an exact-behavior safety net for edge cases
FULLTEXT's word-tokenization can't reproduce (mid-word substrings,
below-minimum-length terms) — see academics/views.py.

Only `text` is indexed: `tags` looks like a text field in the old search
filter but is actually a JSONField (a list of keyword strings) — MySQL
FULLTEXT indexes cannot include JSON columns, and tags search is left
completely unchanged (still Q(tags__icontains=...) in both the fast path
and the fallback) since that was never the field driving the table-scan
cost anyway.

MySQL-only: SQLite (used for local dev/tests) has no FULLTEXT INDEX
syntax at all, and this migration would error on that backend — guarded
via schema_editor.connection.vendor so `python manage.py test` (SQLite)
is unaffected; the corresponding query-code branch in academics/views.py
carries the same guard, so the FULLTEXT path is only ever exercised
against a real MySQL database.
"""
from django.db import migrations


def add_fulltext_index(apps, schema_editor):
    if schema_editor.connection.vendor != 'mysql':
        return
    schema_editor.execute(
        'ALTER TABLE academics_question ADD FULLTEXT INDEX academics_question_text_fts (text)'
    )


def remove_fulltext_index(apps, schema_editor):
    if schema_editor.connection.vendor != 'mysql':
        return
    schema_editor.execute(
        'ALTER TABLE academics_question DROP INDEX academics_question_text_fts'
    )


class Migration(migrations.Migration):

    # ALTER TABLE ... ADD FULLTEXT INDEX is DDL that MySQL/InnoDB commits
    # implicitly and can't roll back — Django refuses to run it inside a
    # migration's default transaction wrapper (TransactionManagementError)
    # unless told the migration isn't atomic. SQLite (local/tests) doesn't
    # hit this at all since add_fulltext_index() no-ops there.
    atomic = False

    dependencies = [
        ('academics', '0023_importbatch_processing_claimed_at'),
    ]

    operations = [
        migrations.RunPython(add_fulltext_index, remove_fulltext_index),
    ]
