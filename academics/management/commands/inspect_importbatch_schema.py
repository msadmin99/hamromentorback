from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    """TEMPORARY, diagnostic-only (schema-drift investigation, upload-500
    root cause phase). Prints the ACTUAL database columns on
    academics_importbatch straight from information_schema — column name,
    type, nullability, database-level default — never via Django's ORM
    model state, since the whole point is to see what the database
    actually has regardless of what today's ImportBatch class knows about.
    Prints schema metadata only: no row data, no credentials, nothing
    user-entered. Meant to be run once via a one-off Cloud Run Job
    execution (never exposed over HTTP) and removed once the compatibility
    migration it informs has shipped and been verified — not a permanent
    management command."""

    help = 'Print the actual database schema for academics_importbatch (read-only, diagnostic).'

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            if connection.vendor == 'mysql':
                cursor.execute(
                    "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT "
                    "FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = 'academics_importbatch' "
                    "ORDER BY ORDINAL_POSITION"
                )
                self.stdout.write('vendor=mysql')
                for name, coltype, nullable, default in cursor.fetchall():
                    self.stdout.write(f'COLUMN name={name} type={coltype} nullable={nullable} default={default!r}')
            else:
                cursor.execute("PRAGMA table_info(academics_importbatch)")
                self.stdout.write(f'vendor={connection.vendor}')
                for row in cursor.fetchall():
                    self.stdout.write(f'COLUMN name={row[1]} type={row[2]} nullable={not row[3]} default={row[4]!r}')
