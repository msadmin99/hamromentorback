from django.contrib import admin

from .models import (
    Course,
    CoursePackage,
    Enrollment,
    EnrollmentRequest,
)


class CoursePackageInline(admin.TabularInline):
    model = CoursePackage
    extra = 1


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ('name', 'prefix', 'program_group', 'order', 'is_active')
    list_filter = ('program_group', 'is_active')
    search_fields = ('name', 'prefix')
    inlines = [CoursePackageInline]


@admin.register(Enrollment)
class EnrollmentAdmin(admin.ModelAdmin):
    list_display = ('user', 'course', 'access_type', 'student_code', 'is_active', 'enrolled_at')
    list_filter = ('course', 'access_type', 'is_active')
    search_fields = ('user__email', 'student_code')


@admin.register(EnrollmentRequest)
class EnrollmentRequestAdmin(admin.ModelAdmin):
    list_display = ('user', 'course', 'status', 'submitted_at')
    list_filter = ('status', 'course')
