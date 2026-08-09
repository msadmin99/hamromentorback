from django.contrib import admin

from .models import Video, VideoCategory, VideoNote, VideoProgress, VideoResource


@admin.register(VideoCategory)
class VideoCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'order')
    search_fields = ('name',)


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    list_display = ('title', 'subject', 'category', 'access_level', 'source_type', 'is_active', 'is_archived', 'views_count')
    list_filter = ('subject', 'category', 'access_level', 'source_type', 'is_active', 'is_archived')
    search_fields = ('title', 'instructor_name')


@admin.register(VideoResource)
class VideoResourceAdmin(admin.ModelAdmin):
    list_display = ('title', 'video', 'resource_type')
    list_filter = ('resource_type',)


@admin.register(VideoProgress)
class VideoProgressAdmin(admin.ModelAdmin):
    list_display = ('user', 'video', 'is_completed', 'is_bookmarked', 'last_watched_at')
    list_filter = ('is_completed', 'is_bookmarked')


@admin.register(VideoNote)
class VideoNoteAdmin(admin.ModelAdmin):
    list_display = ('user', 'video', 'timestamp_seconds', 'created_at')
