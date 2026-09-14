from django.contrib import admin

from .models import (
    Notification,
    NotificationDelivery,
    NotificationPreference,
    NotificationSettings,
    PushDeliveryAttempt,
    PushSubscription,
)


class NotificationDeliveryInline(admin.TabularInline):
    model = NotificationDelivery
    extra = 0
    readonly_fields = [f.name for f in NotificationDelivery._meta.fields if f.name != 'id']
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    """Read-only visibility for debugging "why didn't this student receive
    X" (architecture prompt §41) — not an editing surface. The real
    course-targeted send/campaign UI is Phase 7's own Admin module."""

    list_display = ('created_at', 'user', 'event_type', 'category', 'course', 'status', 'priority', 'read_at')
    list_filter = ('event_type', 'category', 'status', 'priority', 'course')
    search_fields = ('user__email', 'title', 'dedupe_key', 'event_id')
    raw_id_fields = ('user', 'course', 'test', 'attempt', 'purchase', 'subscription', 'video', 'question')
    readonly_fields = [f.name for f in Notification._meta.fields]
    inlines = [NotificationDeliveryInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ('user', 'category', 'channel', 'course', 'enabled')
    list_filter = ('category', 'channel', 'enabled')
    search_fields = ('user__email',)
    raw_id_fields = ('user', 'course')


@admin.register(NotificationSettings)
class NotificationSettingsAdmin(admin.ModelAdmin):
    list_display = ('user', 'timezone', 'quiet_hours_start', 'quiet_hours_end')
    search_fields = ('user__email',)
    raw_id_fields = ('user',)


class PushDeliveryAttemptInline(admin.TabularInline):
    model = PushDeliveryAttempt
    fk_name = 'subscription'
    extra = 0
    readonly_fields = [f.name for f in PushDeliveryAttempt._meta.fields if f.name != 'id']
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(PushSubscription)
class PushSubscriptionAdmin(admin.ModelAdmin):
    """Read-only, same debugging-visibility rationale as NotificationAdmin
    — never an editing surface (an admin has no legitimate reason to
    hand-edit someone's push credentials; revocation happens through the
    student's own device-management UI, or by an admin using the `status`
    filter to spot stale rows, never by rewriting `endpoint`/`p256dh`/
    `auth`)."""

    list_display = ('user', 'browser', 'os', 'device_label', 'status', 'last_seen_at', 'created_at')
    list_filter = ('status', 'browser', 'os')
    search_fields = ('user__email', 'device_label')
    raw_id_fields = ('user',)
    readonly_fields = [f.name for f in PushSubscription._meta.fields]
    inlines = [PushDeliveryAttemptInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
