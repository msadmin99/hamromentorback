from django.contrib import admin

from .models import Chapter, Option, Question, QuestionAttempt, Subject, Topic


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
    list_display = ('public_id', 'text', 'subject', 'chapter', 'year')
    list_filter = ('subject', 'chapter', 'year')
    search_fields = ('text', 'public_id')
    inlines = [OptionInline]


@admin.register(QuestionAttempt)
class QuestionAttemptAdmin(admin.ModelAdmin):
    list_display = ('user', 'question', 'is_correct', 'answered_at')
    list_filter = ('is_correct',)
