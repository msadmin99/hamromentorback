import re

from django.conf import settings
from django.db import models
from django.utils.text import slugify


def _slug_source_text(html):
    """Question.text is stored as HTML — strip tags before slugifying so the
    URL is built from readable words, not tag fragments."""
    plain = re.sub(r'<[^>]+>', ' ', html or '')
    plain = re.sub(r'\s+', ' ', plain).strip()
    return plain[:80]


class Subject(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True, blank=True)
    prefix = models.CharField(
        max_length=20, blank=True,
        help_text='Short code (e.g. PHY, BOT) matched against the "Subject Prefix" column when importing from Excel.',
    )
    icon = models.CharField(max_length=10, default='book', help_text='Emoji or short icon key')
    order = models.PositiveIntegerField(default=0)
    is_free = models.BooleanField(
        default=True,
        help_text='On = anyone can practice this subject. Off = requires an active Question Bank subscription for one of its courses.',
    )
    courses = models.ManyToManyField(
        'courses.Course', blank=True, related_name='subjects',
        help_text='Which course(s) use this subject (a subject can be shared across courses, e.g. Physiology under both CEE-PG and NMCLE).',
    )

    class Meta:
        ordering = ['order', 'name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            suffix = 1
            while Subject.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                suffix += 1
                slug = f'{base_slug}-{suffix}'
            self.slug = slug
        if not self.prefix:
            self.prefix = slugify(self.name).upper().replace('-', '')[:4]
        super().save(*args, **kwargs)


class Chapter(models.Model):
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name='chapters')
    name = models.CharField(max_length=255)
    slug = models.SlugField(blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'name']
        unique_together = ('subject', 'slug')

    def __str__(self):
        return f'{self.subject.name} / {self.name}'

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            suffix = 1
            while Chapter.objects.filter(subject=self.subject, slug=slug).exclude(pk=self.pk).exists():
                suffix += 1
                slug = f'{base_slug}-{suffix}'
            self.slug = slug
        super().save(*args, **kwargs)


class Topic(models.Model):
    """'Schema' entries: important/frequently-asked topics within a chapter."""
    chapter = models.ForeignKey(Chapter, on_delete=models.CASCADE, related_name='topics')
    name = models.CharField(max_length=255)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'name']

    def __str__(self):
        return self.name


class Question(models.Model):
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name='questions')
    chapter = models.ForeignKey(Chapter, on_delete=models.SET_NULL, null=True, blank=True, related_name='questions')
    topic = models.ForeignKey(Topic, on_delete=models.SET_NULL, null=True, blank=True, related_name='questions')
    courses = models.ManyToManyField(
        'courses.Course', blank=True, related_name='questions',
        help_text='Which course(s) this question belongs to (a question can be shared across courses).',
    )

    text = models.TextField()
    image = models.ImageField(upload_to='questions/', null=True, blank=True)
    image_asset = models.ForeignKey(
        'media_library.MediaAsset', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Optimized/responsive replacement for `image`, via media_library. Falls back to `image` '
                   'when unset — existing questions are unaffected.',
    )
    latex = models.TextField(blank=True, help_text='Optional LaTeX, e.g. \\int_0^1 x^2\\,dx')

    explanation = models.TextField(blank=True)
    explanation_image = models.ImageField(upload_to='explanations/', null=True, blank=True)
    explanation_image_asset = models.ForeignKey(
        'media_library.MediaAsset', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Optimized/responsive replacement for `explanation_image`. See `image_asset`.',
    )
    explanation_latex = models.TextField(blank=True)
    explanation_video_url = models.URLField(blank=True)
    references = models.JSONField(
        default=list, blank=True,
        help_text='Book citations, paper links, or YouTube links backing the explanation — '
                   'each entry is {type: book|paper|video|link, label, url}.',
    )

    marks = models.DecimalField(max_digits=5, decimal_places=2, default=1)
    negative_marks = models.DecimalField(max_digits=5, decimal_places=2, default=0.25)

    remarks = models.CharField(max_length=500, blank=True)
    year = models.PositiveIntegerField(null=True, blank=True, help_text='For previous-year papers')
    past_exam_years = models.CharField(
        max_length=100, blank=True, help_text='Comma-separated BS years this question appeared in, e.g. "2078,2080"',
    )
    public_id = models.CharField(max_length=20, unique=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Which staff account created this question — used to scope Teacher-role visibility to their own content.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # --- Free SEO Layer (public /question/{slug}/ page) ---
    slug = models.SlugField(
        max_length=220, unique=True, blank=True,
        help_text='Auto-generated from subject + question text on first save. Stable once set — the URL for '
                   'this question\'s public page never changes on later edits.',
    )
    is_published = models.BooleanField(
        default=False,
        help_text='On = live at /question/{slug}/ and eligible for the sitemap. Off = no public page (practice, '
                   'tests, and import are unaffected either way).',
    )
    is_indexable = models.BooleanField(
        default=True,
        help_text='Only relevant when published. Off = the page stays live but is served noindex and left out '
                   'of the sitemap (e.g. a near-duplicate you don\'t want Google to pick up).',
    )
    short_explanation = models.TextField(
        blank=True,
        help_text='Free teaser shown on the public question page. Leave blank to auto-use a short excerpt of '
                   'the full explanation.',
    )
    seo_title = models.CharField(
        max_length=255, blank=True,
        help_text='Overrides the auto-generated page <title>. Leave blank to auto-generate from the question text.',
    )
    seo_description = models.CharField(
        max_length=255, blank=True,
        help_text='Overrides the auto-generated meta description. Leave blank to auto-generate.',
    )
    quick_revision = models.TextField(
        blank=True, help_text='Optional short revision recap for the public page — the section is omitted entirely if blank.',
    )

    def __str__(self):
        return self.text[:60]

    def save(self, *args, **kwargs):
        if not self.public_id:
            prefix = ''.join(w[0] for w in self.subject.name.split())[:2].upper() or 'MD'
            last = Question.objects.filter(public_id__startswith=prefix).count() + 1
            self.public_id = f'{prefix}{last:04d}'
        if not self.slug:
            self.slug = self._generate_slug()
        super().save(*args, **kwargs)

    def _generate_slug(self):
        subject_part = self.subject.prefix or self.subject.name
        source = _slug_source_text(self.text)
        base_slug = slugify(f'{subject_part} {source}')[:180] or 'question'
        slug = base_slug
        suffix = 1
        while Question.objects.filter(slug=slug).exclude(pk=self.pk).exists():
            suffix += 1
            slug = f'{base_slug}-{suffix}'
        return slug


class Option(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name='options')
    text = models.CharField(max_length=500, blank=True)
    image = models.ImageField(upload_to='options/', null=True, blank=True)
    image_asset = models.ForeignKey(
        'media_library.MediaAsset', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Optimized/responsive replacement for `image`. See Question.image_asset.',
    )
    latex = models.TextField(blank=True)
    is_correct = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)
    pick_percentage = models.PositiveIntegerField(default=0, help_text='% of students who picked this option')

    class Meta:
        ordering = ['order']

    def __str__(self):
        return self.text[:40]


class QuestionAttempt(models.Model):
    """A single MCQ solve event, used both for QBank practice and driving
    subject/chapter progress shown on the Home & QBank screens."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='question_attempts')
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name='attempts')
    selected_option = models.ForeignKey(Option, on_delete=models.SET_NULL, null=True, blank=True)
    is_correct = models.BooleanField(default=False)
    is_bookmarked = models.BooleanField(default=False)
    answered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-answered_at']


class ImportBatch(models.Model):
    """One bulk-question-import upload — tracks parsing/validation/import
    progress and enables rollback. Rows themselves live in ImportRow so the
    admin can preview/edit before anything touches the Question table."""
    FORMAT_CHOICES = [('docx', 'Word (.docx)'), ('xlsx', 'Excel (.xlsx)'), ('csv', 'CSV'), ('json', 'JSON')]
    STATUS_CHOICES = [
        ('uploaded', 'Uploaded'), ('validating', 'Validating'), ('ready', 'Ready to import'),
        ('importing', 'Importing'), ('completed', 'Completed'), ('failed', 'Failed'),
        ('rolled_back', 'Rolled back'),
    ]

    MODE_CHOICES = [('question_bank', 'Import to Question Bank'), ('create_test', 'Import & Create Test')]

    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='import_batches')
    file_name = models.CharField(max_length=255)
    file_format = models.CharField(max_length=10, choices=FORMAT_CHOICES)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='uploaded')
    import_mode = models.CharField(max_length=15, choices=MODE_CHOICES, default='question_bank')
    created_test = models.ForeignKey(
        'tests_app.Test', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Set once Import & Create Test successfully builds a Test from this batch.',
    )

    # Subject/Chapter/Topic are no longer parsed from the uploaded file — the
    # admin picks them once, on the Preview & Validate screen, via dependent
    # dropdowns sourced from the existing Subject Management taxonomy, and
    # every row in the batch is assigned this same triple on confirm. This
    # also eliminates the get-or-create-by-name path that previously created
    # duplicate Subject/Chapter/Topic rows when a file's free-text name didn't
    # exactly match an existing one.
    subject = models.ForeignKey('Subject', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    chapter = models.ForeignKey('Chapter', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    topic = models.ForeignKey('Topic', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    courses = models.ManyToManyField(
        'courses.Course', blank=True, related_name='+',
        help_text='Which course(s) this batch\'s questions belong to — a file can cover multiple courses at once.',
    )

    total_rows = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    duplicate_count = models.PositiveIntegerField(default=0)

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.file_name} ({self.status})'

    @property
    def processed_count(self):
        return self.created_count + self.failed_count + self.skipped_count + self.duplicate_count

    @property
    def progress_percent(self):
        # A completed/rolled-back batch is always 100%, full stop — rows that
        # failed validation before import even started (status='error') are
        # excluded from the import run entirely, so counting them against the
        # denominator would make a fully-finished batch appear stuck below 100%.
        if self.status in ('completed', 'rolled_back'):
            return 100
        eligible = self.total_rows - self.rows.filter(status='error').count()
        if eligible <= 0:
            return 100
        return min(100, round(self.processed_count / eligible * 100))


class ImportRow(models.Model):
    """One parsed question from an ImportBatch's source file, prior to (and
    then after) actually becoming a real Question row."""
    STATUS_CHOICES = [
        ('pending', 'Pending'), ('valid', 'Valid'), ('warning', 'Warning'), ('error', 'Error'),
        ('imported', 'Imported'), ('skipped', 'Skipped'), ('duplicate', 'Duplicate'),
    ]

    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name='rows')
    row_number = models.PositiveIntegerField()
    raw_data = models.JSONField(default=dict, help_text='The parsed ParsedQuestion shape — editable before confirm.')
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='pending')
    errors = models.JSONField(default=list, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    duplicate_of = models.ForeignKey(Question, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    dedup_action = models.CharField(
        max_length=10, blank=True,
        choices=[('skip', 'Skip'), ('replace', 'Replace'), ('keep_both', 'Keep both')],
        help_text='Admin decision for a row flagged as a duplicate — required before it can be confirmed.',
    )
    created_question = models.ForeignKey(Question, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['row_number']

    def __str__(self):
        return f'Batch {self.batch_id} row {self.row_number} [{self.status}]'
