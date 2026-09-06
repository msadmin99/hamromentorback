from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Device, RolePermission, StudentProfile, User


@admin.register(User)
class HamroUserAdmin(UserAdmin):
    list_display = ('email', 'username', 'phone', 'program', 'course', 'admin_role', 'is_active', 'is_staff')
    search_fields = ('email', 'username', 'phone')
    fieldsets = UserAdmin.fieldsets + (
        ('Dr. Gutka info', {'fields': ('phone', 'program', 'course', 'admin_role')}),
    )


@admin.register(RolePermission)
class RolePermissionAdmin(admin.ModelAdmin):
    """The Django-admin mutation path for RolePermission.

    Audited to the same AdminEditAuditLog as the API path (see
    accounts/role_audit.py). Django's own LogEntry already records *that*
    a change happened here; this records what the value was before and
    after, which LogEntry does not capture for a JSONField.

    No double-logging risk: this stack and the DRF viewset are entirely
    separate, so one mutation only ever traverses one of them.
    """
    list_display = ('role', 'features')

    def save_model(self, request, obj, form, change):
        from django.db import transaction

        from .role_audit import record_role_permission_change, snapshot

        # Re-read the stored row for the before-state: `obj` is already
        # carrying the form's new values by the time save_model is called,
        # so snapshotting it here would record new-vs-new and show no change.
        before = snapshot(RolePermission.objects.filter(pk=obj.pk).first()) if change else {}
        with transaction.atomic():
            super().save_model(request, obj, form, change)
            record_role_permission_change(request, before=before, after=snapshot(obj), instance=obj)

    def delete_model(self, request, obj):
        from django.db import transaction

        from .role_audit import record_role_permission_change, snapshot

        before = snapshot(obj)
        with transaction.atomic():
            super().delete_model(request, obj)
            record_role_permission_change(request, before=before, after={}, instance=obj)

    def delete_queryset(self, request, queryset):
        """The bulk-delete admin action. Deleting the row that grants a role
        its features is as consequential as editing it, and the bulk path
        must not be the unaudited way to do it."""
        from django.db import transaction

        from .role_audit import record_role_permission_change, snapshot

        befores = [snapshot(obj) for obj in queryset]
        with transaction.atomic():
            super().delete_queryset(request, queryset)
            for before in befores:
                record_role_permission_change(request, before=before, after={}, instance=None)


@admin.register(StudentProfile)
class StudentProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'college', 'district', 'province', 'exam_target', 'batch')
    search_fields = ('user__email', 'college')


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ('user', 'device_id', 'device_label', 'last_seen')
