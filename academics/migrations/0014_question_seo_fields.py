from django.db import migrations, models
from django.utils.text import slugify

import re


def _slug_source_text(html):
    plain = re.sub(r'<[^>]+>', ' ', html or '')
    plain = re.sub(r'\s+', ' ', plain).strip()
    return plain[:80]


def backfill_slugs(apps, schema_editor):
    Question = apps.get_model('academics', 'Question')
    existing = set(Question.objects.exclude(slug='').values_list('slug', flat=True))
    for question in Question.objects.filter(slug='').select_related('subject').iterator():
        subject_part = question.subject.prefix or question.subject.name
        source = _slug_source_text(question.text)
        base_slug = slugify(f'{subject_part} {source}')[:180] or 'question'
        slug = base_slug
        suffix = 1
        while slug in existing:
            suffix += 1
            slug = f'{base_slug}-{suffix}'
        existing.add(slug)
        Question.objects.filter(pk=question.pk).update(slug=slug)


class Migration(migrations.Migration):

    dependencies = [
        ('academics', '0013_importbatch_created_test_importbatch_import_mode'),
    ]

    operations = [
        migrations.AddField(
            model_name='question',
            name='is_indexable',
            field=models.BooleanField(default=True, help_text="Only relevant when published. Off = the page stays live but is served noindex and left out of the sitemap (e.g. a near-duplicate you don't want Google to pick up)."),
        ),
        migrations.AddField(
            model_name='question',
            name='is_published',
            field=models.BooleanField(default=False, help_text='On = live at /question/{slug}/ and eligible for the sitemap. Off = no public page (practice, tests, and import are unaffected either way).'),
        ),
        migrations.AddField(
            model_name='question',
            name='quick_revision',
            field=models.TextField(blank=True, help_text='Optional short revision recap for the public page — the section is omitted entirely if blank.'),
        ),
        migrations.AddField(
            model_name='question',
            name='seo_description',
            field=models.CharField(blank=True, help_text='Overrides the auto-generated meta description. Leave blank to auto-generate.', max_length=255),
        ),
        migrations.AddField(
            model_name='question',
            name='seo_title',
            field=models.CharField(blank=True, help_text='Overrides the auto-generated page <title>. Leave blank to auto-generate from the question text.', max_length=255),
        ),
        migrations.AddField(
            model_name='question',
            name='short_explanation',
            field=models.TextField(blank=True, help_text='Free teaser shown on the public question page. Leave blank to auto-use a short excerpt of the full explanation.'),
        ),
        # slug goes in 3 steps — non-unique first, backfilled, then made unique —
        # otherwise adding a unique column to a table with existing rows fails
        # immediately (every row would default to the same empty string).
        migrations.AddField(
            model_name='question',
            name='slug',
            field=models.SlugField(blank=True, default='', max_length=220),
            preserve_default=False,
        ),
        migrations.RunPython(backfill_slugs, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='question',
            name='slug',
            field=models.SlugField(blank=True, help_text="Auto-generated from subject + question text on first save. Stable once set — the URL for this question's public page never changes on later edits.", max_length=220, unique=True),
        ),
    ]
