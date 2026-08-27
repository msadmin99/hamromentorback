from django.db import migrations


def report_and_clear_stale_flag(apps, schema_editor):
    """Audit + correct the exact bug condition found in production:
    needs_course_review=True on a Test that already has real `courses`
    assigned. Once assigned, this flag serves no purpose (visible_test_
    queryset now only consults it when courses is empty) and its lingering
    True value is misleading to admins reading the field's own help_text
    ("an admin should assign real courses and clear this flag"). Reports
    every affected test before touching anything; only ever flips a
    boolean — never touches courses, questions, is_draft, or deletes
    anything.
    """
    Test = apps.get_model('tests_app', 'Test')

    stale = Test.objects.filter(needs_course_review=True, courses__isnull=False).distinct()
    stale_ids = list(stale.values_list('id', flat=True))
    if stale_ids:
        print(f'\nAudit: {len(stale_ids)} Test(s) had needs_course_review=True with courses already assigned '
              f'(the exact production leak — visible to every student regardless of enrollment before this fix):')
        for t in stale.order_by('id'):
            course_names = ', '.join(t.courses.values_list('name', flat=True))
            print(f'  Test {t.id} "{t.title}" (exam_type={t.exam_type}) — courses: {course_names}')
        Test.objects.filter(pk__in=stale_ids).update(needs_course_review=False)
        print(f'Cleared needs_course_review on {len(stale_ids)} Test(s) — courses/questions/is_draft untouched.\n')
    else:
        print('\nAudit: no Test has needs_course_review=True with courses already assigned. Nothing to clear.\n')

    # Also report (read-only, no changes) the legitimate remaining case —
    # needs_course_review=True with NO courses assigned — so it's visible
    # in deploy logs which tests are still relying on the legacy escape
    # hatch and could use an explicit admin course assignment.
    legacy = Test.objects.filter(needs_course_review=True, courses__isnull=True)
    legacy_ids = list(legacy.values_list('id', 'title'))
    if legacy_ids:
        print(f'Audit: {len(legacy_ids)} Test(s) remain on the legacy needs_course_review escape hatch '
              f'(no courses assigned — correctly still visible to everyone until an admin assigns courses):')
        for tid, title in legacy_ids:
            print(f'  Test {tid} "{title}"')
        print('')


class Migration(migrations.Migration):

    dependencies = [
        ('tests_app', '0012_answer_time_taken_seconds'),
    ]

    operations = [
        migrations.RunPython(report_and_clear_stale_flag, migrations.RunPython.noop),
    ]
