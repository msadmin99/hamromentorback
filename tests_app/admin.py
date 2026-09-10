from django.contrib import admin

from .models import Answer, ExamTypePolicy, GrandTestMotivationBand, Test, TestAttempt, TestQuestion


class TestQuestionInline(admin.TabularInline):
    model = TestQuestion
    extra = 1


@admin.register(Test)
class TestAdmin(admin.ModelAdmin):
    list_display = ('title', 'exam_type', 'subject', 'duration_minutes', 'is_pro', 'scheduled_start')
    list_filter = ('exam_type', 'subject', 'is_pro')
    search_fields = ('title',)
    inlines = [TestQuestionInline]


@admin.register(TestAttempt)
class TestAttemptAdmin(admin.ModelAdmin):
    list_display = ('user', 'test', 'score', 'rank', 'status', 'start_time')
    list_filter = ('status', 'test')


admin.site.register(Answer)


@admin.register(ExamTypePolicy)
class ExamTypePolicyAdmin(admin.ModelAdmin):
    """Phase 5 — the actual, admin-editable per-exam-category default
    template. Edit a row here to change what a brand-new exam of that
    category starts out looking like in the Create Exam Wizard / Import &
    Create Test flows; existing exams are never affected (see
    tests_app/policy.py's module docstring)."""

    list_display = (
        'exam_type', 'default_is_draft', 'default_duration_minutes', 'default_max_attempts',
        'default_negative_marking', 'default_is_pro', 'updated_at',
    )
    list_editable = (
        'default_is_draft', 'default_duration_minutes', 'default_max_attempts',
        'default_negative_marking', 'default_is_pro',
    )


@admin.register(GrandTestMotivationBand)
class GrandTestMotivationBandAdmin(admin.ModelAdmin):
    """Grand Test 3.0 / GT3-6 — a plain, admin-editable score-band table
    (same pattern as ExamTypePolicyAdmin above), not a rules-engine UI per
    the spec's own 'minimum necessary configuration' instruction. Edit a
    row's title/message/hint or add/remove bands here; tests_app.
    grand_test_analytics.motivation_for_score() reads this table live."""

    list_display = ('title', 'min_percent', 'max_percent', 'order')
    list_editable = ('min_percent', 'max_percent', 'order')
    ordering = ('-min_percent',)
