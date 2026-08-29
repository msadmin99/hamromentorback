from django.conf import settings
from django.db import models


class SmartPracticeConfig(models.Model):
    """Singleton (pk always 1) of admin-editable Smart Practice tuning
    knobs — same load()/save() pattern as academics.QuestionBankConfig /
    core.SiteSettings."""
    enabled = models.BooleanField(default=True, help_text='Platform-wide kill switch. Off = every endpoint returns feature-disabled.')
    min_questions_per_session = models.PositiveIntegerField(default=5)
    max_questions_per_session = models.PositiveIntegerField(default=20)
    default_questions_per_session = models.PositiveIntegerField(default=10)
    weak_topic_accuracy_max_pct = models.PositiveIntegerField(
        default=50, help_text="A topic's accuracy within the source test at/below this = weak, for Source Weak Areas mode. "
                               'Deliberately separate from QuestionBankConfig/tests_app.performance thresholds — this one is source-scoped, those are platform-wide.',
    )
    min_mistakes_to_recommend = models.PositiveIntegerField(
        default=2, help_text='Fewer wrong/skipped answers than this on the source test and no Smart Practice CTA is shown at all.',
    )
    ai_coach_enabled = models.BooleanField(default=False, help_text='v2 — must stay False until a real explanation layer ships. No AI call exists yet.')

    class Meta:
        verbose_name = 'Smart Practice settings'
        verbose_name_plural = 'Smart Practice settings'

    def __str__(self):
        return 'Smart Practice settings'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class SmartPracticeSession(models.Model):
    """A generated practice session scoped to exactly one source Test —
    never Grand Test (enforced in smart_practice.access.resolve_source_scope
    and re-asserted in smart_practice.services.create_session). `course` is
    a snapshot taken at creation time from the source Test's own scope, not
    re-derived from whatever the student's active course happens to be
    later — a session must stay explainable even if the student switches
    active course mid-session."""
    MODE_CHOICES = [
        ('retry_mistakes', 'Master Mistakes'),
        ('source_weak_areas', 'Fix Weak Areas'),
        ('concept_reinforcement', 'Strengthen Concepts'),
        ('due_review', 'Due for Review'),
        ('new_questions', 'New Questions'),
        ('bookmarked', 'Bookmarked'),
        ('ai_mixed', 'AI Mixed Practice'),
    ]
    STATUS_CHOICES = [
        ('in_progress', 'In progress'),
        ('completed', 'Completed'),
        ('abandoned', 'Abandoned'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='smart_practice_sessions')
    source_test = models.ForeignKey('tests_app.Test', on_delete=models.CASCADE, related_name='smart_practice_sessions')
    source_attempt = models.ForeignKey('tests_app.TestAttempt', on_delete=models.SET_NULL, null=True, blank=True, related_name='smart_practice_sessions')
    course = models.ForeignKey('courses.Course', on_delete=models.SET_NULL, null=True, blank=True)

    mode = models.CharField(max_length=25, choices=MODE_CHOICES)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='in_progress')
    question_count = models.PositiveIntegerField()
    selection_reason = models.TextField(blank=True, help_text='Template-generated in v1, e.g. "5 questions you missed on Cardiac Physiology in this test."')

    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    score = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    accuracy = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['user', 'source_test']),
            models.Index(fields=['user', 'status']),
        ]


class SmartPracticeSessionQuestion(models.Model):
    ORIGIN_CHOICES = [
        ('source_mistake', 'Missed in source test'),
        ('source_weak_topic', 'Same weak topic, new question'),
        ('expansion_pool', 'Related concept'),
        ('due_review', 'Due for spaced review'),
        ('new_question', 'Never attempted before'),
        ('bookmarked', 'Bookmarked'),
    ]

    session = models.ForeignKey(SmartPracticeSession, on_delete=models.CASCADE, related_name='questions')
    question = models.ForeignKey('academics.Question', on_delete=models.CASCADE)
    order = models.PositiveIntegerField(default=0)
    origin = models.CharField(max_length=20, choices=ORIGIN_CHOICES)

    selected_option = models.ForeignKey('academics.Option', on_delete=models.SET_NULL, null=True, blank=True)
    is_correct = models.BooleanField(null=True, blank=True, help_text='Null = not yet answered.')
    time_taken_seconds = models.PositiveIntegerField(null=True, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['order']
        unique_together = ('session', 'question')
