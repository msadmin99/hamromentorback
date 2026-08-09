import random
import string

from django.contrib.auth.models import AbstractUser
from django.db import models


def _generate_referral_code(base):
    base = ''.join(ch for ch in (base or '').upper() if ch.isalnum())[:6] or 'HM'
    while True:
        code = base + ''.join(random.choices(string.digits, k=2))
        if not User.objects.filter(referral_code=code).exists():
            return code


class User(AbstractUser):
    ADMIN_ROLE_CHOICES = [
        ('super_admin', 'Super Admin'),
        ('admin', 'Admin'),
        ('editor', 'Editor'),
        ('teacher', 'Teacher'),
    ]

    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, unique=True, null=True, blank=True)
    # Free text, matching courses.Course.program_group's convention — chosen at
    # registration from whatever program groups/courses admin currently has set
    # up under Course Management, not a fixed list baked into this model.
    program = models.CharField(max_length=50, blank=True, help_text='e.g. CEE-PG, CEE-UG, NHPC — matches a Course.program_group value.')
    course = models.CharField(max_length=50, blank=True, help_text="Matches a courses.Course.prefix value the student registered under.")
    admin_role = models.CharField(max_length=20, choices=ADMIN_ROLE_CHOICES, blank=True, null=True)
    can_manage_all_content = models.BooleanField(
        default=False,
        help_text="Teacher-role only: if set, this account can see/manage every Test and Question, not just their own.",
    )
    active_course = models.ForeignKey(
        'courses.Course', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text="The student's currently-selected subcourse — drives which QBank/Mock/Daily/Grand/PYQ content their dashboard shows.",
    )

    referral_code = models.CharField(
        max_length=20, unique=True, blank=True,
        help_text='Auto-generated — this student shares it so friends get a discount and they earn wallet credit.',
    )
    referred_by = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='referrals',
        help_text='Which student referred this account, if any — set at registration from a referral code.',
    )
    wallet_balance = models.DecimalField(
        max_digits=9, decimal_places=2, default=0,
        help_text='Reward credit earned from referrals. Accumulates only — not yet redeemable at checkout.',
    )

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    def __str__(self):
        return self.email

    def save(self, *args, **kwargs):
        if not self.referral_code:
            self.referral_code = _generate_referral_code(self.first_name or self.email.split('@')[0])
        super().save(*args, **kwargs)


# Feature keys an Editor account may be granted; anything outside this set is
# Admin/Super-Admin only regardless of what's saved in RolePermission.
EDITOR_ALLOWED_FEATURES = [
    'question_bank', 'question_entry', 'video_lectures', 'test_series', 'daily_live_exam', 'self_mock_test', 'mock_test',
]

ALL_FEATURES = [
    'dashboard', 'courses', 'question_bank', 'question_entry', 'video_lectures', 'daily_practice', 'students',
    'test_series', 'self_mock_test', 'enrollment_requests', 'daily_live_exam', 'mock_test',
    'website_settings', 'advanced', 'billing', 'teacher_applications', 'marketplace_courses',
]

# Teacher permissions are a fixed ceiling, not admin-configurable via RolePermission
# (that model's `role` choices deliberately exclude 'teacher' — see RolePermission below).
TEACHER_ALLOWED_FEATURES = ['question_entry', 'test_series', 'video_lectures']


class RolePermission(models.Model):
    """Which dashboard feature keys an 'admin' or 'editor' role account can access.
    Super Admin implicitly has every feature and isn't stored here."""
    role = models.CharField(max_length=20, unique=True, choices=[('admin', 'Admin'), ('editor', 'Editor')])
    features = models.JSONField(default=list)

    def __str__(self):
        return self.role


class StudentProfile(models.Model):
    PAYMENT_CHANNEL_CHOICES = [('bank', 'Bank Transfer'), ('esewa', 'eSewa'), ('khalti', 'Khalti')]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    college = models.CharField(max_length=255, blank=True)
    district = models.CharField(max_length=100, blank=True)
    province = models.CharField(max_length=100, blank=True)
    exam_target = models.CharField(max_length=100, blank=True)
    batch = models.CharField(max_length=50, blank=True)
    photo = models.ImageField(upload_to='profile_photos/', null=True, blank=True)
    plan_expires_at = models.DateField(null=True, blank=True)
    preferred_payment_channel = models.CharField(
        max_length=10, choices=PAYMENT_CHANNEL_CHOICES, blank=True,
        help_text='Pre-selected at checkout — informational only, this platform has no stored payment instrument.',
    )

    def __str__(self):
        return f'Profile<{self.user.email}>'


class Device(models.Model):
    """Tracks devices a student has logged in from (max 3 active devices)."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='devices')
    device_id = models.CharField(max_length=255)
    device_label = models.CharField(max_length=255, blank=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'device_id')
