from django.contrib import admin

from .models import Announcement, Banner, HomeFeature, MCQOfTheDay, SiteLink, SiteSettings


@admin.register(Banner)
class BannerAdmin(admin.ModelAdmin):
    list_display = ('title', 'tag', 'order', 'is_active')


@admin.register(MCQOfTheDay)
class MCQOfTheDayAdmin(admin.ModelAdmin):
    list_display = ('date', 'question', 'label', 'question_courses')

    def question_courses(self, obj):
        courses = ', '.join(c.name for c in obj.question.courses.all())
        return courses or 'All courses'
    question_courses.short_description = 'Scoped to'


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ('message', 'coupon_code', 'is_active', 'created_at')


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not SiteSettings.objects.exists()


@admin.register(HomeFeature)
class HomeFeatureAdmin(admin.ModelAdmin):
    list_display = ('number', 'title', 'order')


@admin.register(SiteLink)
class SiteLinkAdmin(admin.ModelAdmin):
    list_display = ('section', 'label', 'url', 'order')
    list_filter = ('section',)
