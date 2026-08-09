from django.db import migrations


def forwards(apps, schema_editor):
    RolePermission = apps.get_model('accounts', 'RolePermission')
    for rp in RolePermission.objects.all():
        if 'question_bank' in rp.features and 'question_entry' not in rp.features:
            rp.features = [*rp.features, 'question_entry']
            rp.save(update_fields=['features'])


def backwards(apps, schema_editor):
    RolePermission = apps.get_model('accounts', 'RolePermission')
    for rp in RolePermission.objects.all():
        if 'question_entry' in rp.features:
            rp.features = [f for f in rp.features if f != 'question_entry']
            rp.save(update_fields=['features'])


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_teacher_role_and_content_scope'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
