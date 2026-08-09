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
    list_display = ('role', 'features')


@admin.register(StudentProfile)
class StudentProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'college', 'district', 'province', 'exam_target', 'batch')
    search_fields = ('user__email', 'college')


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ('user', 'device_id', 'device_label', 'last_seen')
