"""Backfill Free Starter entitlements for existing students.

Whether/when existing (pre-Phase-2) students should receive Free Starter
access — vs. only students registering after this ships — is a product
decision not resolved by this phase (see docs/ENTITLEMENT_DATA_MODEL.md's
Migration strategy section). This command is the ready-to-run tool for
whichever decision gets made; it is NOT executed automatically by this
phase's deployment.

Idempotent and safe to re-run any number of times: provision_free_starter's
own (user, resource_type) uniqueness means a second run creates zero new
rows for anyone already provisioned. --dry-run by default, matching the
existing audit_exam_course_assignment command's convention exactly.
"""
from django.core.management.base import BaseCommand

from accounts.models import User
from entitlements.provisioning import provision_free_starter


class Command(BaseCommand):
    help = 'Provision Free Starter entitlements for every existing student (idempotent; dry-run by default).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually write. Without this, only reports what would happen.')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        students = User.objects.filter(is_staff=False)
        total = students.count()
        self.stdout.write(f'{total} student account(s) found.')

        if not apply_changes:
            self.stdout.write(self.style.WARNING('Dry run — no changes made. Re-run with --apply to provision.'))
            return

        provisioned_students = 0
        rows_created = 0
        for user in students.iterator():
            created = provision_free_starter(user)
            if created:
                provisioned_students += 1
                rows_created += len(created)

        self.stdout.write(self.style.SUCCESS(
            f'Done. {provisioned_students} student(s) received at least one new Free Starter row '
            f'({rows_created} row(s) total). Students already fully provisioned were left untouched.'
        ))
