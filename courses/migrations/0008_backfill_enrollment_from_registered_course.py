import random

from django.db import migrations


def backfill_enrollment_from_registered_course(apps, schema_editor):
    """Root-cause data fix for "free student sees 0 tests" — the exact same
    bug class as 0007_backfill_enrollment_from_subscription (same file's
    own docstring: "a student could pay... and still see zero content"),
    on the free side this time.

    courses.access.eligible_course_ids() — the single gate behind Test/
    Question/Video catalog visibility everywhere in the app — reads ONLY
    courses.Enrollment. A student picks a course at registration
    (accounts.User.course, a Course.prefix) but nothing ever turned that
    choice into an Enrollment row: not before this migration's companion
    code fix in accounts.serializers.RegisterSerializer.create(). Every
    self-registered student from before that fix is catalog-blind — 0
    Daily/Mock/Grand/PYQ/QBank content — unless an admin happened to
    enroll them (which in practice mostly only happened as a side effect
    of paying, via _ensure_enrollment/0007 above).

    Reports every affected (user, course) pair before creating anything.
    Purely additive: creates only Enrollment rows for students who
    currently have NONE — a student who already has any Enrollment
    (free, package, or admin-approved) is left untouched, so this can
    only ever grant the free tier's own catalog visibility, never
    override or downgrade an existing paid/admin-granted one. Never
    touches User, StudentProfile, Subscription, or Purchase.
    """
    User = apps.get_model('accounts', 'User')
    Course = apps.get_model('courses', 'Course')
    Enrollment = apps.get_model('courses', 'Enrollment')

    courses_by_prefix = {c.prefix: c for c in Course.objects.all()}

    # Staff/admin accounts don't go through student registration and don't
    # use course-scoped catalog visibility the same way — excluded so this
    # only ever touches genuine student rows, mirroring every other
    # student-only data-fix precedent in this codebase (e.g.
    # AdminUserViewSet.queryset = User.objects.filter(is_staff=False)).
    candidates = (
        User.objects.filter(is_staff=False)
        .exclude(course='')
        .values('id', 'email', 'course')
    )

    missing = []
    for row in candidates:
        course = courses_by_prefix.get(row['course'])
        if course is None:
            continue  # stale/free-text value with no matching Course — nothing to enroll into
        if Enrollment.objects.filter(user_id=row['id'], course_id=course.id).exists():
            continue  # already enrolled some way (free, package, or admin-approved) — leave as-is
        missing.append({'user_id': row['id'], 'email': row['email'], 'course': course})

    if not missing:
        print('\nAudit: every registered student already has a matching Enrollment for their chosen course. Nothing to backfill.\n')
        return

    print(f'\nAudit: {len(missing)} registered student(s) had no Enrollment for their chosen course '
          f'(the exact "free student sees 0 tests" bug) — creating them now:')
    for row in missing:
        print(f"  user={row['email']} course={row['course'].name}")
        # Historical migration models don't run Enrollment.save()'s custom
        # student_code auto-generation — replicate it here, exactly as
        # 0007 already does, or every row after the first collides on the
        # unique constraint by inserting student_code=''.
        prefix = row['course'].prefix
        Enrollment.objects.create(
            user_id=row['user_id'], course_id=row['course'].id, access_type='free', is_active=True,
            student_code=f"{prefix or 'HM'}{random.randint(10000, 99999)}",
        )
    print(f'Backfilled {len(missing)} Enrollment row(s). No User/Subscription/Purchase data was touched.\n')


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0007_backfill_enrollment_from_subscription'),
        ('accounts', '0008_alter_user_course_alter_user_program'),
    ]

    operations = [
        migrations.RunPython(backfill_enrollment_from_registered_course, migrations.RunPython.noop),
    ]
