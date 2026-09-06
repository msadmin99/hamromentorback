"""Phase 5 — seed one ExamTypePolicy row per exam category.

Every field is left at its model column default (see 0018_examtypepolicy.py)
— a genuine zero-behavior-change launch. See
Backend/docs/PHASE5_AUDIT_AND_ARCHITECTURE.md §3 for why no differentiated
per-category values were invented: the pre-Phase-5 audit found no existing
differentiated behavior to preserve, and inventing new business numbers
without a source of truth would be guessing policy. Admins can differentiate
each category's defaults afterward via Django admin — that's the actual
capability this migration unlocks, not a set of pre-baked numbers.

Reversible: the reverse operation deletes exactly these 5 rows (and nothing
else, since exam_type is their primary key and no other code ever creates a
row here).
"""
from django.db import migrations

EXAM_TYPES = ['qbank', 'daily', 'mock', 'grand', 'pyq']


def seed_policies(apps, schema_editor):
    ExamTypePolicy = apps.get_model('tests_app', 'ExamTypePolicy')
    for exam_type in EXAM_TYPES:
        ExamTypePolicy.objects.get_or_create(exam_type=exam_type)


def remove_policies(apps, schema_editor):
    ExamTypePolicy = apps.get_model('tests_app', 'ExamTypePolicy')
    ExamTypePolicy.objects.filter(exam_type__in=EXAM_TYPES).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tests_app', '0018_examtypepolicy'),
    ]

    operations = [
        migrations.RunPython(seed_policies, remove_policies),
    ]
