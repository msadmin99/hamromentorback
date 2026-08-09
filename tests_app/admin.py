from django.contrib import admin

from .models import Answer, Test, TestAttempt, TestQuestion


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
