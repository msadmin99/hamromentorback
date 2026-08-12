from django.core.management.base import BaseCommand

from courses.models import Course, CoursePackage

# Canonical course/program definitions — kept identical to the equivalent
# block in core.management.commands.seed_data (the project's full demo-data
# seeder), since that's this codebase's existing source of truth for what
# courses should exist. This command exists separately so a production
# database can get just this real, load-bearing data (the registration
# page's Program/Course dropdowns depend on it) without also pulling in
# seed_data's placeholder subjects/questions/demo accounts.
COURSE_DEFS = [
    ('CEE-UG MBBS', 'MBBS', 'CEE-UG', '🩺', '#f59e0b',
     'Undergraduate entrance exam for MBBS admission, with the full QBank and subject-wise tests.'),
    ('CEE-UG BDS', 'BDS', 'CEE-UG', '🦷', '#14b8a6',
     'Undergraduate entrance exam for BDS admission, sharing the same high-yield question bank.'),
    ('CEE-PG MD/MS', 'MD', 'CEE-PG', '⚕️', '#8b5cf6',
     'Postgraduate entrance prep for MD/MS seats, with Grand Tests that mirror the real pattern.'),
    ('NMCLE (MBBS)', 'NMBBS', 'NMCLE', '📜', '#ec4899',
     'Nepal Medical Council Licensing Exam preparation for MBBS graduates.'),
    ('NMCLE (BDS)', 'NBDS', 'NMCLE', '📜', '#06b6d4',
     'Nepal Medical Council Licensing Exam preparation for BDS graduates.'),
]


class Command(BaseCommand):
    help = (
        'Idempotently creates the Course/CoursePackage rows the registration page\'s '
        'Program/Course dropdowns depend on (courses.models.Course.program_group). '
        'Safe to re-run — uses get_or_create throughout, never duplicates or overwrites.'
    )

    def handle(self, *args, **options):
        for order, (name, prefix, group, icon, color, desc) in enumerate(COURSE_DEFS):
            course, created = Course.objects.get_or_create(
                prefix=prefix,
                defaults={
                    'name': name, 'program_group': group, 'order': order,
                    'icon': icon, 'color': color, 'description': desc,
                },
            )
            self.stdout.write(('Created ' if created else 'Already exists: ') + f'{course.name} ({course.program_group})')

            _, pkg_created_1 = CoursePackage.objects.get_or_create(
                course=course, name='Full Access Package (3 Months)',
                defaults={'price': 2999, 'duration_days': 90},
            )
            _, pkg_created_2 = CoursePackage.objects.get_or_create(
                course=course, name='Full Access Package (Lifetime)',
                defaults={'price': 7999, 'duration_days': None},
            )
            if pkg_created_1 or pkg_created_2:
                self.stdout.write(f'  + packages for {course.prefix}')

        self.stdout.write(self.style.SUCCESS(f'Done. {Course.objects.count()} course(s) in database.'))
