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
    'question_reports', 'exam_schedule',
]

# Layered under 'test_series' (which gates seeing Exam Management at all) —
# a role can have the base feature without these, e.g. a Question Editor who
# builds/edits exams but can't schedule sessions, archive, or delete them.
# Chosen over a separate role hierarchy (Question Editor/Exam Manager as
# first-class admin_role values) so the existing feature-key + RolePermission
# system stays the single source of truth for capability differences.
EXAM_MANAGEMENT_FEATURES = ['exam_schedule', 'exam_archive', 'exam_delete', 'exam_release_solutions']

ALL_FEATURES = [
    'dashboard', 'courses', 'question_bank', 'question_entry', 'video_lectures', 'daily_practice', 'students',
    'test_series', 'self_mock_test', 'enrollment_requests', 'question_reports', 'daily_live_exam', 'mock_test',
    'website_settings', 'advanced', 'billing', 'teacher_applications', 'marketplace_courses',
    *EXAM_MANAGEMENT_FEATURES,
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


def user_feature_list(user):
    """The dashboard feature keys this account may use — the single source
    of truth for the whole RolePermission system.

    FIX (P0 security audit): before this helper existed, this exact
    computation was duplicated only in UserSerializer.get_permissions()
    (accounts/serializers.py) and consulted ONLY by the frontend
    (hasFeature()/RequireStaff) — no backend DRF permission class ever
    read RolePermission at all, so the feature-key vocabulary (billing,
    exam_schedule, exam_archive, exam_delete, question_entry, ...) was
    frontend-UX-only. `hamromentor.permissions.HasFeature` is the backend
    enforcement counterpart, and both it and the serializer now call this
    one function so they can never drift apart again.
    """
    if not user or not getattr(user, 'is_staff', False):
        return []
    if getattr(user, 'is_superuser', False) or getattr(user, 'admin_role', None) in (None, '', 'super_admin'):
        return ALL_FEATURES
    if user.admin_role == 'teacher':
        # Fixed ceiling — not admin-configurable via RolePermission (see model comment).
        return TEACHER_ALLOWED_FEATURES
    role_permission = RolePermission.objects.filter(role=user.admin_role).first()
    if role_permission:
        if user.admin_role == 'editor':
            # Server-side ceiling enforcement (P0 fix): EDITOR_ALLOWED_FEATURES
            # was previously documented as a hard ceiling but never actually
            # enforced — a RolePermission row could contain anything (e.g.
            # 'billing', 'advanced') and get_permissions()/HasFeature would
            # honor it verbatim. Intersecting here means a stored row can never
            # grant an Editor more than the documented ceiling, regardless of
            # how it was written (API, admin site, a future bug in the save UI).
            return [f for f in role_permission.features if f in EDITOR_ALLOWED_FEATURES]
        return role_permission.features
    return ALL_FEATURES if user.admin_role == 'admin' else EDITOR_ALLOWED_FEATURES


class StudentProfile(models.Model):
    PAYMENT_CHANNEL_CHOICES = [('bank', 'Bank Transfer'), ('esewa', 'eSewa'), ('khalti', 'Khalti')]
    # Identity verification is a profile-layer feature only — see the
    # help_text below. It must NEVER be imported into tests_app/access.py,
    # billing/access.py, courses/access.py, or any exam-listing/start/
    # submit code path. See accounts/tests_verification.py's regression
    # suite, which proves access is identical across all four states.
    VERIFICATION_STATUS_CHOICES = [
        ('unverified', 'Unverified'), ('pending', 'Pending'),
        ('verified', 'Verified'), ('rejected', 'Rejected'),
    ]

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
    verification_status = models.CharField(
        max_length=10, choices=VERIFICATION_STATUS_CHOICES, default='unverified',
        help_text='Profile identity verification only — never a gate on exam access, Free Starter, subscriptions, '
                   'or any entitlement. Set to "pending" automatically on first document/photo submission; '
                   '"verified"/"rejected" only ever set by an explicit admin action.',
    )
    verification_reviewed_at = models.DateTimeField(null=True, blank=True)
    verification_reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    verification_rejection_reason = models.CharField(max_length=500, blank=True)

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


class VerificationDocument(models.Model):
    """One uploaded identity/academic document, stored in the private GCS
    bucket (see accounts/verification_storage.py) — never on this row
    itself beyond metadata. A student may have several (one per
    document_type, or several attempts at the same type); nothing here
    ever deletes an old one, even on rejection — see
    verification_storage.py's own retention note."""
    DOCUMENT_TYPE_CHOICES = [
        ('citizenship', 'Citizenship'), ('passport', 'Passport'),
        ('academic_certificate', 'Academic Certificate'),
        ('identity_document', 'Identity Document'), ('other', 'Other'),
    ]
    STATUS_CHOICES = [('pending', 'Pending'), ('approved', 'Approved'), ('rejected', 'Rejected')]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='verification_documents')
    document_type = models.CharField(max_length=25, choices=DOCUMENT_TYPE_CHOICES)
    storage_bucket = models.CharField(max_length=100, blank=True)
    storage_key = models.CharField(max_length=255, blank=True)
    original_filename = models.CharField(max_length=255, blank=True)
    mime_type = models.CharField(max_length=100, blank=True)
    file_size = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    rejection_reason = models.CharField(max_length=500, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f'{self.get_document_type_display()} — {self.user.email} ({self.status})'
