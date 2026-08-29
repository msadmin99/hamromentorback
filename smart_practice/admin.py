from django.contrib import admin

from .models import SmartPracticeConfig, SmartPracticeSession, SmartPracticeSessionQuestion


@admin.register(SmartPracticeConfig)
class SmartPracticeConfigAdmin(admin.ModelAdmin):
    """Singleton — same one-row admin pattern as academics.QuestionBankConfig."""

    def has_add_permission(self, request):
        return not SmartPracticeConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


class SmartPracticeSessionQuestionInline(admin.TabularInline):
    model = SmartPracticeSessionQuestion
    extra = 0
    readonly_fields = ('question', 'order', 'origin', 'selected_option', 'is_correct', 'time_taken_seconds', 'answered_at')
    can_delete = False


@admin.register(SmartPracticeSession)
class SmartPracticeSessionAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'source_test', 'mode', 'status', 'question_count', 'accuracy', 'started_at')
    list_filter = ('mode', 'status')
    search_fields = ('user__email', 'source_test__title')
    readonly_fields = [f.name for f in SmartPracticeSession._meta.fields]
    inlines = [SmartPracticeSessionQuestionInline]

    def has_add_permission(self, request):
        return False
