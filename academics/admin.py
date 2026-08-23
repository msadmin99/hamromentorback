from django.contrib import admin

from .models import Chapter, Option, Question, QuestionAttempt, QuestionBankConfig, QuestionEvent, Subject, Topic


class ChapterInline(admin.TabularInline):
    model = Chapter
    extra = 1


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'icon', 'order')
    prepopulated_fields = {'slug': ('name',)}
    inlines = [ChapterInline]


class TopicInline(admin.TabularInline):
    model = Topic
    extra = 1


@admin.register(Chapter)
class ChapterAdmin(admin.ModelAdmin):
    list_display = ('name', 'subject', 'order')
    list_filter = ('subject',)
    inlines = [TopicInline]


class OptionInline(admin.TabularInline):
    model = Option
    extra = 4


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ('public_id', 'text', 'subject', 'chapter', 'year', 'instructor_difficulty', 'actual_difficulty')
    list_filter = ('subject', 'chapter', 'year', 'instructor_difficulty', 'actual_difficulty', 'question_type')
    search_fields = ('text', 'public_id')
    readonly_fields = ('actual_difficulty', 'actual_difficulty_sample_size', 'actual_difficulty_updated_at')
    inlines = [OptionInline]


@admin.register(QuestionAttempt)
class QuestionAttemptAdmin(admin.ModelAdmin):
    list_display = ('user', 'question', 'is_correct', 'mastery_status', 'attempts_count', 'is_bookmarked', 'answered_at')
    list_filter = ('is_correct', 'mastery_status', 'is_bookmarked')


@admin.register(QuestionEvent)
class QuestionEventAdmin(admin.ModelAdmin):
    list_display = ('user', 'question', 'is_correct', 'source', 'created_at')
    list_filter = ('is_correct', 'source')


@admin.register(QuestionBankConfig)
class QuestionBankConfigAdmin(admin.ModelAdmin):
    """Singleton — same one-row admin pattern as core.SiteSettings."""

    def has_add_permission(self, request):
        return not QuestionBankConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
