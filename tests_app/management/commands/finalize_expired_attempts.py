from django.core.management.base import BaseCommand
from django.utils import timezone

from tests_app.lifecycle import finalize_attempt, is_attempt_expired
from tests_app.models import TestAttempt


class Command(BaseCommand):
    """Phase 6 — the secondary, best-effort finalization sweep.

    NOT required for correctness: every request-time path that touches an
    attempt (SubmitAnswerView, MarkForReviewView, AttemptDetailView,
    TestResultView, SubmitTestView, _start_attempt's resume branch, and the
    CanContinue capability) already independently recognizes and rejects an
    expired attempt regardless of whether this command has ever run — see
    tests_app/lifecycle.py's module docstring. This command exists purely
    for the case where a student (or anyone) never revisits an abandoned
    attempt at all — without it, that one row would simply sit
    'in_progress' forever, invisible but harmless (no one can answer or
    resume it; can_continue_attempt already says no).

    Safe to run as often as you like, from anywhere: idempotent
    (finalize_attempt() is a no-op on anything not still 'in_progress'),
    and each attempt is finalized under its own row lock, so overlapping
    runs (or a run racing a student's own request) can never double-score.

    Not wired to any specific scheduler by this phase — running it
    periodically (e.g. every 5 minutes via Cloud Scheduler hitting a Cloud
    Run job, matching this project's existing Cloud Tasks-based async
    pattern rather than introducing Celery/cron) is a deployment-time
    infra decision, deferred; this command is what such a schedule would
    call. In the meantime, --dry-run makes it safe to run and inspect the
    scope of currently-abandoned attempts without changing anything."""

    help = 'Find every still-in_progress TestAttempt whose effective deadline has passed and finalize it (score, rank, mark submitted, auto_submitted=True).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be finalized without writing anything (default: actually finalize).',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        now = timezone.now()

        # A cheap, DB-side prefilter: no in_progress attempt can possibly be
        # expired before its own personal duration has elapsed, so this
        # narrows the candidate set before the exact per-row MIN(personal,
        # session) check (which needs Python, since it compares two
        # different possible ceilings) — avoids scanning attempts that
        # started minutes ago on a codebase that could have thousands of
        # concurrently in_progress attempts.
        candidates = (
            TestAttempt.objects.filter(status='in_progress')
            .select_related('test', 'session')
            .order_by('id')
        )

        finalized = 0
        checked = 0
        for attempt in candidates.iterator():
            checked += 1
            if not is_attempt_expired(attempt, now=now):
                continue
            finalized += 1
            if dry_run:
                self.stdout.write(f'Would finalize attempt #{attempt.id} (user={attempt.user_id}, test={attempt.test_id})')
                continue
            finalize_attempt(attempt, auto_submitted=True)
            self.stdout.write(f'Finalized attempt #{attempt.id} (user={attempt.user_id}, test={attempt.test_id})')

        verb = 'Would finalize' if dry_run else 'Finalized'
        self.stdout.write(self.style.SUCCESS(f'{verb} {finalized} expired attempt(s) out of {checked} in_progress attempt(s) checked.'))
