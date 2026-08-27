import random

from django.db import migrations


def backfill_enrollment_from_subscription(apps, schema_editor):
    """Root-cause data fix for the "student pays, sees nothing" bug:
    buying a subscription never created a courses.Enrollment row — only a
    billing.Subscription — and every catalog visibility check in the app
    (subjects, chapters, questions, tests, videos) reads Enrollment, not
    Subscription. Any student who purchased before the code fix
    (billing.payment_service._ensure_enrollment) is stuck with a paid
    Subscription and zero visible content until this backfills the missing
    Enrollment. Reports every affected (user, course) pair before creating
    anything; purely additive — never touches Subscription, Purchase, or
    any existing Enrollment.
    """
    Subscription = apps.get_model('billing', 'Subscription')
    Enrollment = apps.get_model('courses', 'Enrollment')

    missing = []
    seen = set()
    for sub in Subscription.objects.all().values('user_id', 'course_id', 'user__email', 'course__name', 'course__prefix'):
        key = (sub['user_id'], sub['course_id'])
        if key in seen:
            continue
        seen.add(key)
        if not Enrollment.objects.filter(user_id=sub['user_id'], course_id=sub['course_id']).exists():
            missing.append(sub)

    if not missing:
        print('\nAudit: every Subscription already has a matching Enrollment. Nothing to backfill.\n')
        return

    print(f'\nAudit: {len(missing)} paid Subscription(s) had no matching Enrollment '
          f'(the exact "student pays, sees nothing" bug) — creating them now:')
    for row in missing:
        print(f"  user={row['user__email']} course={row['course__name']}")
        # Historical migration models don't run Enrollment.save()'s custom
        # student_code auto-generation — replicate it here, or every row
        # after the first would try to insert student_code='' and collide
        # on the unique constraint.
        prefix = row['course__prefix']
        Enrollment.objects.create(
            user_id=row['user_id'], course_id=row['course_id'], access_type='package', is_active=True,
            student_code=f"{prefix or 'HM'}{random.randint(10000, 99999)}",
        )
    print(f'Backfilled {len(missing)} Enrollment row(s). No Subscription/Purchase data was touched.\n')


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0006_batch_enrollment_batch'),
        ('billing', '0012_alter_purchase_kind_purchasecomboitem_comboplan_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_enrollment_from_subscription, migrations.RunPython.noop),
    ]
