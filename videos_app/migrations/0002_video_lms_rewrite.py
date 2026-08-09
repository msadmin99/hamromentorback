import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


DEFAULT_CATEGORIES = [
    'Subject Lecture', 'Chapter Lecture', 'Topic Lecture',
    'Quick Revision', 'Crash Course', 'High Yield Topics',
    'Question Discussion', 'Previous Year Solutions', 'Mock Test Discussion',
    'Clinical Case', 'Practical Demonstration', 'OSCE', 'Laboratory Demonstration',
    'Exam Strategy', 'Study Plan', 'Motivation', 'Orientation',
]


def _slugify(value):
    import re
    value = re.sub(r'[^\w\s-]', '', value or '').strip().lower()
    return re.sub(r'[-\s]+', '-', value) or 'video'


def backfill_videos(apps, schema_editor):
    Video = apps.get_model('videos_app', 'Video')
    used_slugs = set()
    for video in Video.objects.all():
        base_slug = _slugify(video.title)
        slug = base_slug
        suffix = 1
        while slug in used_slugs or Video.objects.filter(slug=slug).exclude(pk=video.pk).exists():
            suffix += 1
            slug = f'{base_slug}-{suffix}'
        used_slugs.add(slug)

        video.slug = slug
        video.video_url = video.url
        video.duration_seconds = (video.duration_minutes or 0) * 60
        video.source_type = 'youtube' if video.url else 'external_url'
        video.access_level = 'registered' if video.is_free else 'premium'
        video.save(update_fields=['slug', 'video_url', 'duration_seconds', 'source_type', 'access_level'])


def seed_categories(apps, schema_editor):
    VideoCategory = apps.get_model('videos_app', 'VideoCategory')
    for order, name in enumerate(DEFAULT_CATEGORIES):
        VideoCategory.objects.get_or_create(name=name, defaults={'slug': _slugify(name), 'order': order})


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('videos_app', '0001_initial'),
        ('academics', '0009_add_created_by'),
        ('courses', '0005_remove_vestigial_promo_code'),
        ('tests_app', '0006_add_created_by'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='VideoCategory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, unique=True)),
                ('slug', models.SlugField(blank=True, unique=True)),
                ('order', models.PositiveIntegerField(default=0)),
            ],
            options={'ordering': ['order', 'name'], 'verbose_name_plural': 'Video categories'},
        ),

        # --- Add every new field first (nullable / defaulted, no uniqueness yet) ---
        migrations.AddField(model_name='video', name='slug', field=models.SlugField(blank=True, default='', max_length=280), preserve_default=False),
        migrations.AddField(model_name='video', name='description', field=models.TextField(blank=True, default=''), preserve_default=False),
        migrations.AddField(model_name='video', name='category', field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='videos', to='videos_app.videocategory')),
        migrations.AddField(model_name='video', name='courses', field=models.ManyToManyField(blank=True, help_text='Which subcourse(s) this video is assigned to. Blank = visible to every course.', related_name='videos', to='courses.course')),
        migrations.AddField(model_name='video', name='chapter', field=models.ForeignKey(blank=True, help_text='"Unit" in the admin UI.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='videos', to='academics.chapter')),
        migrations.AddField(model_name='video', name='topic', field=models.ForeignKey(blank=True, help_text='"Chapter" in the admin UI.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='videos', to='academics.topic')),
        migrations.AddField(model_name='video', name='source_type', field=models.CharField(choices=[('upload', 'Direct Upload'), ('youtube', 'YouTube'), ('vimeo', 'Vimeo'), ('external_url', 'External URL')], default='youtube', max_length=20)),
        migrations.AddField(model_name='video', name='video_file', field=models.FileField(blank=True, upload_to='video_lectures/')),
        migrations.AddField(model_name='video', name='video_url', field=models.URLField(blank=True, default=''), preserve_default=False),
        migrations.AddField(model_name='video', name='duration_seconds', field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name='video', name='access_level', field=models.CharField(choices=[('public', 'Public — anyone, even signed out'), ('registered', 'Registered — any logged-in student'), ('premium', 'Premium — requires an active Video Lectures subscription'), ('course', 'Course-Based — requires enrollment in one of the assigned courses'), ('teacher_only', 'Teacher Only')], default='registered', max_length=20)),
        migrations.AddField(model_name='video', name='allow_notes_download', field=models.BooleanField(default=True)),
        migrations.AddField(model_name='video', name='allow_slides_download', field=models.BooleanField(default=True)),
        migrations.AddField(model_name='video', name='linked_tests', field=models.ManyToManyField(blank=True, help_text='Quizzes a student can jump to from the player ("Open Linked Quiz").', related_name='linked_videos', to='tests_app.test')),
        migrations.AddField(model_name='video', name='is_active', field=models.BooleanField(default=True, help_text='Off = hidden from students (draft).')),
        migrations.AddField(model_name='video', name='is_archived', field=models.BooleanField(default=False, help_text='Retired — hidden from students and default admin lists.')),
        migrations.AddField(model_name='video', name='views_count', field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name='video', name='created_by', field=models.ForeignKey(blank=True, help_text='Which staff account uploaded this — used to scope Teacher-role visibility to their own content.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
        migrations.AddField(model_name='video', name='created_at', field=models.DateTimeField(auto_now_add=True, default=timezone.now), preserve_default=False),
        migrations.AddField(model_name='video', name='updated_at', field=models.DateTimeField(auto_now=True, default=timezone.now), preserve_default=False),

        migrations.AlterField(model_name='video', name='subject', field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='videos', to='academics.subject')),
        migrations.AlterField(model_name='video', name='instructor_name', field=models.CharField(blank=True, help_text='Display override — falls back to the uploader’s name if blank.', max_length=100)),

        migrations.CreateModel(
            name='VideoResource',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('resource_type', models.CharField(choices=[('notes', 'Lecture Notes (PDF)'), ('slides', 'Slides'), ('practice', 'Practice Questions'), ('reference', 'Reference / External Reading')], default='notes', max_length=20)),
                ('title', models.CharField(max_length=255)),
                ('file', models.FileField(blank=True, upload_to='video_resources/')),
                ('external_url', models.URLField(blank=True)),
                ('order', models.PositiveIntegerField(default=0)),
                ('video', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='resources', to='videos_app.video')),
            ],
            options={'ordering': ['order', 'id']},
        ),
        migrations.CreateModel(
            name='VideoProgress',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('last_position_seconds', models.PositiveIntegerField(default=0)),
                ('max_position_seconds', models.PositiveIntegerField(default=0, help_text='Furthest point reached — used for watch %.')),
                ('is_completed', models.BooleanField(default=False)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('is_bookmarked', models.BooleanField(default=False)),
                ('last_watched_at', models.DateTimeField(auto_now=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='video_progress', to=settings.AUTH_USER_MODEL)),
                ('video', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='progress', to='videos_app.video')),
            ],
            options={'ordering': ['-last_watched_at']},
        ),
        migrations.AlterUniqueTogether(name='videoprogress', unique_together={('user', 'video')}),
        migrations.CreateModel(
            name='VideoNote',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('timestamp_seconds', models.PositiveIntegerField(blank=True, null=True)),
                ('text', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='video_notes', to=settings.AUTH_USER_MODEL)),
                ('video', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='notes', to='videos_app.video')),
            ],
            options={'ordering': ['timestamp_seconds', 'created_at']},
        ),

        # --- Backfill existing rows from the old fields, then seed default categories ---
        migrations.RunPython(backfill_videos, noop),
        migrations.RunPython(seed_categories, noop),

        # --- Finalize: uniqueness + drop the now-superseded old fields ---
        migrations.AlterField(model_name='video', name='slug', field=models.SlugField(blank=True, max_length=280, unique=True)),
        migrations.RemoveField(model_name='video', name='url'),
        migrations.RemoveField(model_name='video', name='duration_minutes'),
        migrations.RemoveField(model_name='video', name='rating'),
        migrations.RemoveField(model_name='video', name='instructor_photo'),
        migrations.RemoveField(model_name='video', name='is_free'),

        migrations.AlterModelOptions(name='video', options={'ordering': ['order', '-created_at']}),
    ]
