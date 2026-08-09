from django.conf import settings
from django.db import models
from django.utils.text import slugify


class VideoCategory(models.Model):
    """Admin-manageable content type tags (Subject Lecture, Quick Revision, Crash
    Course, Clinical Case, ...) — a free-form list rather than a hardcoded choices
    field so admins can add more without a code change."""
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'name']
        verbose_name_plural = 'Video categories'

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            suffix = 1
            while VideoCategory.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                suffix += 1
                slug = f'{base_slug}-{suffix}'
            self.slug = slug
        super().save(*args, **kwargs)


class Video(models.Model):
    SOURCE_CHOICES = [
        ('upload', 'Direct Upload'),
        ('youtube', 'YouTube'),
        ('vimeo', 'Vimeo'),
        ('external_url', 'External URL'),
    ]
    ACCESS_CHOICES = [
        ('public', 'Public — anyone, even signed out'),
        ('registered', 'Registered — any logged-in student'),
        ('premium', 'Premium — requires an active Video Lectures subscription'),
        ('course', 'Course-Based — requires enrollment in one of the assigned courses'),
        ('teacher_only', 'Teacher Only'),
    ]

    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=280, unique=True, blank=True)
    description = models.TextField(blank=True)
    category = models.ForeignKey(
        VideoCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name='videos',
    )

    courses = models.ManyToManyField(
        'courses.Course', blank=True, related_name='videos',
        help_text='Which subcourse(s) this video is assigned to. Blank = visible to every course.',
    )
    subject = models.ForeignKey('academics.Subject', on_delete=models.SET_NULL, null=True, blank=True, related_name='videos')
    chapter = models.ForeignKey(
        'academics.Chapter', on_delete=models.SET_NULL, null=True, blank=True, related_name='videos',
        help_text='"Unit" in the admin UI.',
    )
    topic = models.ForeignKey(
        'academics.Topic', on_delete=models.SET_NULL, null=True, blank=True, related_name='videos',
        help_text='"Chapter" in the admin UI.',
    )

    source_type = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='youtube')
    video_file = models.FileField(upload_to='video_lectures/', blank=True)
    video_url = models.URLField(blank=True)
    thumbnail = models.ImageField(upload_to='video_thumbs/', null=True, blank=True)

    instructor_name = models.CharField(
        max_length=100, blank=True, help_text='Display override — falls back to the uploader’s name if blank.',
    )
    duration_seconds = models.PositiveIntegerField(default=0)

    access_level = models.CharField(max_length=20, choices=ACCESS_CHOICES, default='registered')
    allow_notes_download = models.BooleanField(default=True)
    allow_slides_download = models.BooleanField(default=True)

    linked_tests = models.ManyToManyField(
        'tests_app.Test', blank=True, related_name='linked_videos',
        help_text='Quizzes a student can jump to from the player ("Open Linked Quiz").',
    )

    is_active = models.BooleanField(default=True, help_text='Off = hidden from students (draft).')
    is_archived = models.BooleanField(default=False, help_text='Retired — hidden from students and default admin lists.')
    order = models.PositiveIntegerField(default=0)
    views_count = models.PositiveIntegerField(default=0)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Which staff account uploaded this — used to scope Teacher-role visibility to their own content.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order', '-created_at']

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.title) or 'video'
            slug = base_slug
            suffix = 1
            while Video.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                suffix += 1
                slug = f'{base_slug}-{suffix}'
            self.slug = slug
        super().save(*args, **kwargs)


class VideoResource(models.Model):
    TYPE_CHOICES = [
        ('notes', 'Lecture Notes (PDF)'),
        ('slides', 'Slides'),
        ('practice', 'Practice Questions'),
        ('reference', 'Reference / External Reading'),
    ]

    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='resources')
    resource_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='notes')
    title = models.CharField(max_length=255)
    file = models.FileField(upload_to='video_resources/', blank=True)
    external_url = models.URLField(blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f'{self.video.title} — {self.title}'


class VideoProgress(models.Model):
    """Per-student watch state for one video — resume position, completion, and
    bookmark all live here (mirrors QuestionAttempt.is_bookmarked's shape: one
    row per (user, target), toggled via update_or_create)."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='video_progress')
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='progress')

    last_position_seconds = models.PositiveIntegerField(default=0)
    max_position_seconds = models.PositiveIntegerField(default=0, help_text='Furthest point reached — used for watch %.')
    is_completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    is_bookmarked = models.BooleanField(default=False)

    last_watched_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'video')
        ordering = ['-last_watched_at']

    def __str__(self):
        return f'{self.user} — {self.video.title}'


class VideoNote(models.Model):
    """A student's personal note while watching, optionally pinned to a timestamp."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='video_notes')
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='notes')
    timestamp_seconds = models.PositiveIntegerField(null=True, blank=True)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['timestamp_seconds', 'created_at']

    def __str__(self):
        return f'{self.user} note on {self.video.title}'
