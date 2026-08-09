from django.db import migrations, models

# subject -> qbank (per-subject practice tests map onto Question Bank)
# mini    -> mock  (full-pattern mock tests)
# daily, live -> daily (already treated as one everywhere downstream)
# grand   -> grand (unchanged)
# pyq     -> pyq   (unchanged)
GROUP_TO_EXAM_TYPE = {
    'subject': 'qbank',
    'mini': 'mock',
    'live': 'daily',
    'daily': 'daily',
    'grand': 'grand',
    'pyq': 'pyq',
}


def forwards(apps, schema_editor):
    Test = apps.get_model('tests_app', 'Test')
    for old_value, new_value in GROUP_TO_EXAM_TYPE.items():
        Test.objects.filter(exam_type=old_value).update(exam_type=new_value)


def backwards(apps, schema_editor):
    Test = apps.get_model('tests_app', 'Test')
    EXAM_TYPE_TO_GROUP = {
        'qbank': 'subject',
        'mock': 'mini',
        'daily': 'daily',
        'grand': 'grand',
        'pyq': 'pyq',
    }
    for new_value, old_value in EXAM_TYPE_TO_GROUP.items():
        Test.objects.filter(exam_type=new_value).update(exam_type=old_value)


class Migration(migrations.Migration):

    dependencies = [
        ('tests_app', '0004_test_courses_test_description_test_difficulty_and_more'),
    ]

    operations = [
        migrations.RenameField(
            model_name='test',
            old_name='group',
            new_name='exam_type',
        ),
        migrations.RunPython(forwards, backwards),
        migrations.AlterField(
            model_name='test',
            name='exam_type',
            field=models.CharField(
                choices=[
                    ('qbank', 'Question Bank'),
                    ('daily', 'Daily Test'),
                    ('mock', 'Mock Test'),
                    ('grand', 'Grand Test'),
                    ('pyq', 'Past Year Questions'),
                ],
                default='mock',
                max_length=20,
            ),
        ),
    ]
