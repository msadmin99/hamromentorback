import random

from django.conf import settings
from django.db import models


class Course(models.Model):
    """A sellable program track, e.g. 'CEE-MD Ayurveda' under the 'CEE-PG' group."""
    name = models.CharField(max_length=150, unique=True)
    prefix = models.CharField(
        max_length=20, unique=True,
        help_text='Matches the "Course Prefix" column when importing questions from Excel.',
    )
    program_group = models.CharField(max_length=50, blank=True, help_text='e.g. CEE-PG, CEE-UG, NHPC')
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True, help_text='Off = hidden from the homepage "Choose your course" section.')

    icon = models.CharField(max_length=10, default='📘', help_text='Emoji shown on the homepage course card.')
    color = models.CharField(max_length=20, default='#0b5fd9', help_text='Accent color (hex) for the homepage course card.')
    description = models.TextField(blank=True, help_text='Short description shown on the homepage course card.')

    class Meta:
        ordering = ['program_group', 'order', 'name']

    def __str__(self):
        return self.name


class CoursePackage(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='packages')
    name = models.CharField(max_length=150)
    price = models.DecimalField(max_digits=9, decimal_places=2, default=0)
    duration_days = models.PositiveIntegerField(null=True, blank=True, help_text='Blank = lifetime access')
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f'{self.course.name} - {self.name}'


class Enrollment(models.Model):
    ACCESS_CHOICES = [('free', 'Free'), ('package', 'Package')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='enrollments')
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='enrollments')
    package = models.ForeignKey(CoursePackage, on_delete=models.SET_NULL, null=True, blank=True)
    access_type = models.CharField(max_length=10, choices=ACCESS_CHOICES, default='free')
    student_code = models.CharField(max_length=20, unique=True, blank=True)
    is_active = models.BooleanField(default=True)
    enrolled_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('user', 'course')
        ordering = ['-enrolled_at']

    def __str__(self):
        return f'{self.user} -> {self.course}'

    def save(self, *args, **kwargs):
        if not self.student_code:
            self.student_code = f'{self.course.prefix}{random.randint(10000, 99999)}'
        super().save(*args, **kwargs)


class EnrollmentRequest(models.Model):
    STATUS_CHOICES = [('pending', 'Pending'), ('approved', 'Approved'), ('declined', 'Declined')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='enrollment_requests')
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='enrollment_requests')
    package = models.ForeignKey(CoursePackage, on_delete=models.SET_NULL, null=True, blank=True)
    student_code = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    submitted_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-submitted_at']

    def __str__(self):
        return f'{self.user} -> {self.course} ({self.status})'
