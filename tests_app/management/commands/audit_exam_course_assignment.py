from django.core.management.base import BaseCommand

from tests_app.models import Test


class Command(BaseCommand):
    """One-time migration helper for the switch from "blank Test.courses =
    visible to everyone" to default-deny, course-scoped exam access.

    Every Test that is currently published (is_draft=False) with no course/
    student/batch assignment was, until now, silently visible to every
    enrolled student. Flipping enforcement to default-deny without this step
    would make those exams invisible to everyone overnight — exactly what
    the migration must not do. This command instead:

    - Leaves any already-scoped exam (courses/assigned_students/
      assigned_batches non-empty) untouched.
    - Auto-assigns a course when the exam's Subject maps to exactly one
      Course (an unambiguous, safe inference).
    - Otherwise flags Test.needs_course_review=True, which the access-control
      layer (tests_app/access.py) treats as "keep behaving exactly like
      before" until an admin reviews and assigns real courses.

    Run with --dry-run first (the default) and inspect the report before
    running with --apply. Safe to re-run — already-scoped/already-flagged
    exams are always skipped."""

    help = 'Audit and (optionally) backfill course assignment for legacy unscoped exams before default-deny access control goes live.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Actually write changes. Without this flag, only prints the report (read-only).',
        )

    def handle(self, *args, **options):
        apply_changes = options['apply']

        candidates = Test.objects.filter(
            is_draft=False, courses__isnull=True, assigned_students__isnull=True, assigned_batches__isnull=True,
        ).distinct().select_related('subject').prefetch_related('subject__courses')

        total = Test.objects.count()
        already_scoped = Test.objects.exclude(pk__in=candidates.values_list('pk', flat=True)).count()

        mapped = []
        needs_review = []
        for test in candidates:
            course_ids = list(test.subject.courses.values_list('id', flat=True)) if test.subject_id else []
            if len(course_ids) == 1:
                mapped.append((test, course_ids[0]))
            else:
                needs_review.append(test)

        if apply_changes:
            for test, course_id in mapped:
                test.courses.set([course_id])
            Test.objects.filter(pk__in=[t.pk for t in needs_review]).update(needs_course_review=True)

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Exam course-assignment audit') + (' (DRY RUN — no changes written)' if not apply_changes else ' (APPLIED)'))
        self.stdout.write(f'Total exams:              {total}')
        self.stdout.write(f'Already scoped:           {already_scoped}')
        self.stdout.write(f'Successfully mapped:      {len(mapped)}')
        self.stdout.write(f'Needs Admin Review:       {len(needs_review)}')
        self.stdout.write('')

        if mapped:
            self.stdout.write('Mapped (subject -> single course):')
            for test, course_id in mapped:
                self.stdout.write(f'  #{test.id} "{test.title}" -> course {course_id}')
            self.stdout.write('')

        if needs_review:
            self.stdout.write(self.style.WARNING('Needs Admin Review (kept visible under the legacy exception until reviewed):'))
            for test in needs_review:
                subject_label = test.subject.name if test.subject_id else '(no subject)'
                self.stdout.write(f'  #{test.id} "{test.title}" — subject: {subject_label}')
            self.stdout.write('')

        if not apply_changes:
            self.stdout.write(self.style.WARNING('This was a dry run. Re-run with --apply to write these changes.'))
