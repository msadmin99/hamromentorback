from django.conf import settings
from django.db import models

from academics.models import Question, Subject


class ExamTemplate(models.Model):
    """The stable identity of a recurring exam (e.g. 'CEE Mock Test #01' /
    EXAM-000125) that persists across reschedules. Each Test row linked via
    Test.exam_template is one "version" of this template (question set +
    settings); each ExamSession is one scheduled occurrence of a version."""
    exam_code = models.CharField(max_length=20, unique=True, blank=True)
    title = models.CharField(max_length=255)
    exam_type = models.CharField(max_length=20, choices=[
        ('qbank', 'Question Bank'), ('daily', 'Daily Test'), ('mock', 'Mock Test'),
        ('grand', 'Grand Test'), ('pyq', 'Past Year Questions'),
    ])
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.title} ({self.exam_code})'

    def save(self, *args, **kwargs):
        if not self.exam_code:
            last = ExamTemplate.objects.count() + 1
            code = f'EXAM-{last:06d}'
            while ExamTemplate.objects.filter(exam_code=code).exists():
                last += 1
                code = f'EXAM-{last:06d}'
            self.exam_code = code
        super().save(*args, **kwargs)


class Test(models.Model):
    EXAM_TYPE_CHOICES = [
        ('qbank', 'Question Bank'),
        ('daily', 'Daily Test'),
        ('mock', 'Mock Test'),
        ('grand', 'Grand Test'),
        ('pyq', 'Past Year Questions'),
    ]
    DIFFICULTY_CHOICES = [('easy', 'Easy'), ('medium', 'Medium'), ('hard', 'Hard')]
    SOLUTIONS_VISIBILITY_CHOICES = [
        ('auto', 'Automatically, once the exam window ends'),
        ('manual', 'Only when I click "Release solutions"'),
    ]

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, help_text='Short blurb shown on the exam card, e.g. "Sharpen your skills with exam-focused practice."')
    difficulty = models.CharField(max_length=10, choices=DIFFICULTY_CHOICES, blank=True)
    exam_type = models.CharField(max_length=20, choices=EXAM_TYPE_CHOICES, default='mock')
    subject = models.ForeignKey(Subject, on_delete=models.SET_NULL, null=True, blank=True, related_name='tests')
    courses = models.ManyToManyField(
        'courses.Course', blank=True, related_name='mapped_tests',
        help_text='Which subcourse(s) this exam is assigned to. Blank = visible to every enrolled student (unscoped).',
    )

    questions = models.ManyToManyField(Question, through='TestQuestion', related_name='tests')

    duration_minutes = models.PositiveIntegerField(default=60)
    questions_per_page = models.PositiveIntegerField(
        default=1,
        help_text='How many questions show together on one exam page. 1 = one question per screen (classic).',
    )
    negative_marking = models.BooleanField(default=True)
    shuffle_questions = models.BooleanField(default=True)
    shuffle_options = models.BooleanField(default=True)
    max_attempts = models.PositiveIntegerField(default=1)
    solutions_visibility = models.CharField(max_length=10, choices=SOLUTIONS_VISIBILITY_CHOICES, default='auto')

    is_pro = models.BooleanField(default=False)
    is_new = models.BooleanField(default=False)
    price = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    access_password = models.CharField(max_length=50, blank=True)
    free_preview_questions = models.PositiveIntegerField(
        default=0,
        help_text="For PRO Daily/Live tests only: how many questions a student without a subscription can preview. "
                   "They can view but not submit those questions until they subscribe.",
    )

    academic_year = models.CharField(max_length=20, blank=True, help_text='e.g. 2025-26')
    university = models.CharField(
        max_length=100, blank=True,
        help_text='Conducting institution for Past Year Questions, e.g. IOM, MOE, BPKIHS, KU — the top-level '
                   'grouping on the student Past Year Questions page (Year is the level below it).',
    )
    scheduled_start = models.DateTimeField(null=True, blank=True)
    scheduled_end = models.DateTimeField(null=True, blank=True)

    is_draft = models.BooleanField(
        default=False,
        help_text='On = only visible to staff (e.g. still being assembled via Import & Create Test, or now managed '
                   'through Exam Sessions). Off = visible to every eligible student, same as before this field existed.',
    )

    # --- Reschedule / Exam Versioning ---
    # A Test row IS an "Exam Version" (it already holds the question set, duration,
    # marks, negative marking). exam_template groups multiple versions of the same
    # recurring exam together; null on every Test that predates this feature or
    # has never been rescheduled — fully backward compatible.
    exam_template = models.ForeignKey(
        'ExamTemplate', on_delete=models.SET_NULL, null=True, blank=True, related_name='versions',
        help_text='Set the first time this exam is rescheduled — groups this Test (and any later versions) under '
                   'one stable exam identity so it can have multiple scheduled Exam Sessions.',
    )
    version_number = models.PositiveIntegerField(default=1)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Which staff account created this exam — used to scope Teacher-role visibility to their own content.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-scheduled_start', '-created_at']

    def __str__(self):
        return self.title

    @property
    def total_marks(self):
        return sum(float(q.marks) for q in self.questions.all())

    @property
    def question_count(self):
        return self.questions.count()


class TestQuestion(models.Model):
    test = models.ForeignKey(Test, on_delete=models.CASCADE)
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order']
        unique_together = ('test', 'question')


class ExamSession(models.Model):
    """One scheduled occurrence of an Exam Template's version — this is what
    'Reschedule / Schedule Again' creates. Never mutates exam_version once it
    has any attempt; changing questions/settings instead creates a new Test
    version (see tests_app.exam_versioning.clone_test_as_new_version)."""
    STATUS_CHOICES = [
        ('draft', 'Draft'), ('scheduled', 'Scheduled'), ('registration_open', 'Registration Open'),
        ('live', 'Live'), ('completed', 'Completed'), ('cancelled', 'Cancelled'),
    ]
    ACCESS_CHOICES = [
        ('all', 'All eligible students'), ('course', 'Specific course subscribers'),
        ('membership', 'Specific membership'), ('batch', 'Specific batch/group'),
        ('private', 'Private/password protected'),
    ]
    RECURRENCE_CHOICES = [
        ('none', 'One time'), ('weekly', 'Weekly'), ('monthly', 'Monthly'), ('custom', 'Custom'),
    ]

    exam_template = models.ForeignKey(ExamTemplate, on_delete=models.CASCADE, related_name='sessions')
    exam_version = models.ForeignKey(
        Test, on_delete=models.PROTECT, related_name='sessions',
        help_text='The Test row (question set + settings) this session uses. Never changes after creation.',
    )
    session_name = models.CharField(max_length=255)
    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()
    registration_deadline = models.DateTimeField(null=True, blank=True)
    timezone = models.CharField(max_length=50, default='Asia/Kathmandu')
    access_type = models.CharField(max_length=15, choices=ACCESS_CHOICES, default='all')
    access_courses = models.ManyToManyField(
        'courses.Course', blank=True, related_name='+',
        help_text="Used when access_type='course' — which course(s)' subscribers may take this session.",
    )
    password = models.CharField(max_length=50, blank=True, help_text="Used when access_type='private'.")
    max_attempts = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')

    # Future-ready for recurring scheduling (see spec #15) — schema only,
    # no recurrence-generation logic implemented yet.
    recurrence = models.CharField(max_length=10, choices=RECURRENCE_CHOICES, default='none')
    recurrence_parent = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='occurrences',
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-start_datetime']

    def __str__(self):
        return f'{self.exam_template.title} — {self.session_name}'

    @property
    def participant_count(self):
        return self.attempts.values('user').distinct().count()

    def refresh_status(self):
        """Auto-transitions based on current time. Never moves a session out
        of 'cancelled' or 'draft' — those are explicit admin actions only."""
        from django.utils import timezone as tz
        if self.status in ('cancelled', 'draft', 'completed'):
            return self.status
        now = tz.now()
        if now > self.end_datetime:
            new_status = 'completed'
        elif now >= self.start_datetime:
            new_status = 'live'
        elif self.registration_deadline and now >= self.registration_deadline:
            new_status = 'scheduled'
        else:
            new_status = 'registration_open' if self.registration_deadline else 'scheduled'
        if new_status != self.status:
            self.status = new_status
            self.save(update_fields=['status'])
        return self.status


class TestAttempt(models.Model):
    STATUS_CHOICES = [
        ('in_progress', 'In progress'),
        ('submitted', 'Submitted'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='test_attempts')
    test = models.ForeignKey(Test, on_delete=models.CASCADE, related_name='attempts')
    session = models.ForeignKey(
        ExamSession, on_delete=models.SET_NULL, null=True, blank=True, related_name='attempts',
        help_text='Null for every attempt made before this feature existed, or made directly against a Test that '
                   'has never been scheduled through a session — those keep ranking against test-wide attempts '
                   'exactly as before.',
    )
    attempt_number = models.PositiveIntegerField(default=1)
    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(null=True, blank=True)
    score = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    rank = models.PositiveIntegerField(null=True, blank=True)
    percentile = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    accuracy = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='in_progress')

    class Meta:
        ordering = ['-start_time']

    def __str__(self):
        return f'{self.user} - {self.test} (#{self.attempt_number})'


class Answer(models.Model):
    attempt = models.ForeignKey(TestAttempt, on_delete=models.CASCADE, related_name='answers')
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    selected_option = models.ForeignKey('academics.Option', on_delete=models.SET_NULL, null=True, blank=True)
    is_correct = models.BooleanField(default=False)
    is_marked_for_review = models.BooleanField(default=False)

    class Meta:
        unique_together = ('attempt', 'question')
