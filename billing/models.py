import random
import string
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


class SubscriptionPlan(models.Model):
    """Admin-defined purchasable plan for one product type under one course."""
    PRODUCT_CHOICES = [
        ('qbank', 'Practice Question Bank'),
        ('daily_test', 'Daily Test'),
        ('mock_test', 'Mock Test'),
        ('video', 'Video Lectures'),
        ('pyq', 'Past Year Questions'),
    ]
    DURATION_UNIT_CHOICES = [
        ('day', 'Day(s)'),
        ('week', 'Week(s)'),
        ('month', 'Month(s)'),
        ('year', 'Year(s)'),
    ]

    course = models.ForeignKey('courses.Course', on_delete=models.CASCADE, related_name='subscription_plans')
    product_type = models.CharField(max_length=20, choices=PRODUCT_CHOICES)
    name = models.CharField(max_length=150, help_text='e.g. "3 Month QBank Access", "30 Mock Tests / 3 Months"')
    duration_value = models.PositiveIntegerField(default=1)
    duration_unit = models.CharField(max_length=10, choices=DURATION_UNIT_CHOICES, default='month')
    mock_test_quota = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Only for Mock Test plans — how many mock tests this package includes. Blank = unlimited.',
    )
    price = models.DecimalField(max_digits=9, decimal_places=2)
    is_active = models.BooleanField(default=True)
    is_popular = models.BooleanField(default=False, help_text='Shows a "Popular" badge on the plan card.')
    is_best_value = models.BooleanField(default=False, help_text='Shows a "Best Value" badge on the plan card.')
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['course', 'product_type', 'order']

    def __str__(self):
        return f'{self.course.name} — {self.name}'

    def duration_timedelta(self):
        if self.duration_unit == 'day':
            return timedelta(days=self.duration_value)
        if self.duration_unit == 'week':
            return timedelta(weeks=self.duration_value)
        if self.duration_unit == 'month':
            return timedelta(days=30 * self.duration_value)
        if self.duration_unit == 'year':
            return timedelta(days=365 * self.duration_value)
        return timedelta(days=30)


class Subscription(models.Model):
    """A student's activated access window for one product under one course."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='subscriptions')
    plan = models.ForeignKey(SubscriptionPlan, on_delete=models.SET_NULL, null=True, blank=True, related_name='subscriptions')
    course = models.ForeignKey('courses.Course', on_delete=models.CASCADE, related_name='subscriptions')
    product_type = models.CharField(max_length=20, choices=SubscriptionPlan.PRODUCT_CHOICES)
    starts_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    mock_test_quota = models.PositiveIntegerField(null=True, blank=True)
    mock_test_used = models.PositiveIntegerField(default=0)
    auto_renew = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user} — {self.get_product_type_display()} ({self.course.prefix})'

    @property
    def is_current(self):
        if not self.is_active:
            return False
        if self.expires_at and self.expires_at < timezone.now():
            return False
        if self.mock_test_quota is not None and self.mock_test_used >= self.mock_test_quota:
            return False
        return True


class Coupon(models.Model):
    DISCOUNT_TYPE_CHOICES = [
        ('fixed', 'Fixed amount'),
        ('percentage', 'Percentage'),
        ('free_exam', 'Free Exam'),
        ('free_subscription', 'Free Subscription'),
        ('free_grand_test', 'Free Grand Test'),
    ]
    FREE_DISCOUNT_TYPES = ('free_exam', 'free_subscription', 'free_grand_test')
    APPLIES_TO_CHOICES = [
        ('all', 'Entire store'),
        ('qbank', 'Question Bank Subscription'),
        ('daily_test', 'Daily Test Subscription'),
        ('mock_test', 'Mock Test Package'),
        ('grand_test', 'Grand Test'),
        ('video', 'Video Lectures Subscription'),
    ]
    ELIGIBILITY_CHOICES = [
        ('everyone', 'Everyone'),
        ('registered', 'Registered Users Only'),
        ('first_purchase', 'First Purchase Only'),
        ('new_students', 'New Students'),
        ('specific_emails', 'Specific Email Addresses'),
    ]

    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=150, blank=True)
    course = models.ForeignKey(
        # SET_NULL (not CASCADE): the field is nullable specifically to mean
        # "applies across every course" (see help_text) — deleting the scoped
        # Course should fall back to that same unscoped state, not destroy
        # the coupon itself. CASCADE here was a copy-paste bug found during
        # the permanent-deletion-system audit — past purchases that used
        # this coupon are already SET_NULL-protected either way.
        'courses.Course', on_delete=models.SET_NULL, null=True, blank=True, related_name='coupons',
        help_text='Blank = applies across every course.',
    )
    discount_type = models.CharField(max_length=20, choices=DISCOUNT_TYPE_CHOICES, default='percentage')
    discount_value = models.DecimalField(max_digits=9, decimal_places=2, default=0)
    applies_to = models.CharField(max_length=20, choices=APPLIES_TO_CHOICES, default='all')
    start_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    max_uses = models.PositiveIntegerField(null=True, blank=True, help_text='Blank = unlimited total redemptions.')
    max_uses_per_user = models.PositiveIntegerField(default=1)
    min_purchase_amount = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    max_discount_amount = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    first_purchase_only = models.BooleanField(default=False)
    eligibility = models.CharField(max_length=20, choices=ELIGIBILITY_CHOICES, default='everyone')
    eligible_emails = models.TextField(
        blank=True, help_text='Comma-separated emails — only used when eligibility="Specific Email Addresses".',
    )
    new_student_days = models.PositiveIntegerField(
        default=30, help_text='Only used when eligibility="New Students" — days since registration to still count as new.',
    )
    auto_apply = models.BooleanField(
        default=False, help_text='Automatically applied (best-value one wins) when the student has not typed a code.',
    )
    is_active = models.BooleanField(default=True)
    usage_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.code

    def is_valid_now(self):
        today = timezone.localdate()
        if not self.is_active:
            return False
        if self.start_date and today < self.start_date:
            return False
        if self.expiry_date and today > self.expiry_date:
            return False
        if self.max_uses is not None and self.usage_count >= self.max_uses:
            return False
        return True

    def applies_to_product(self, product_type):
        return self.applies_to == 'all' or self.applies_to == product_type

    def applies_to_course(self, plan, grand_test):
        """Blank course = applies everywhere. Otherwise must match the subscription
        plan's course, or (for Grand Test) be among the test's mapped courses — an
        unscoped Grand Test (Test.courses blank = visible to everyone) matches any coupon."""
        if self.course_id is None:
            return True
        if plan is not None:
            return plan.course_id == self.course_id
        if grand_test is not None:
            course_ids = list(grand_test.courses.values_list('id', flat=True))
            return not course_ids or self.course_id in course_ids
        return True

    def is_eligible_for(self, user):
        if not user or not user.is_authenticated:
            return self.eligibility == 'everyone'
        if self.eligibility == 'first_purchase':
            return not Purchase.objects.filter(user=user, status__in=Purchase.OPEN_STATUSES).exists()
        if self.eligibility == 'new_students':
            cutoff = timezone.now() - timedelta(days=self.new_student_days)
            return user.date_joined >= cutoff
        if self.eligibility == 'specific_emails':
            allowed = [e.strip().lower() for e in self.eligible_emails.split(',') if e.strip()]
            return user.email.lower() in allowed
        return True  # everyone / registered — every purchaser is already authenticated in this app

    def compute_discount(self, amount):
        if self.discount_type in self.FREE_DISCOUNT_TYPES:
            return amount
        if self.discount_type == 'fixed':
            discount = self.discount_value
        else:
            discount = amount * self.discount_value / 100
        if self.max_discount_amount is not None:
            discount = min(discount, self.max_discount_amount)
        return min(discount, amount)


class PaymentMethod(models.Model):
    """Admin-configurable payment channel shown on the QR Payment Page —
    same list/slug shape as marketplace.CourseCategory / videos_app.VideoCategory."""
    PROVIDER_CHOICES = [
        ('fonepay', 'Fonepay'),
        ('khalti', 'Khalti'),
        ('esewa', 'eSewa'),
        ('connectips', 'connectIPS'),
        ('bank_qr', 'Bank QR'),
        ('other', 'Other'),
    ]

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True, blank=True)
    provider_type = models.CharField(
        max_length=15, choices=PROVIDER_CHOICES, default='other',
        help_text='Which QR/payment network this is — drives future automatic-verification routing (see billing.payment_providers). Purely informational today.',
    )
    merchant_name = models.CharField(max_length=150, blank=True, help_text="The merchant/account holder's name shown on the QR receipt.")
    merchant_id = models.CharField(max_length=100, blank=True, help_text='Merchant/account ID for this channel, if applicable.')
    account_info = models.TextField(blank=True, help_text='Bank/account number or other identifying info shown alongside the QR, if needed.')
    qr_code_image = models.ImageField(upload_to='payment_method_qr/', null=True, blank=True)
    instructions = models.TextField(blank=True, help_text='"How to Pay" steps shown on the QR Payment Page.')
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            suffix = 1
            while PaymentMethod.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                suffix += 1
                slug = f'{base_slug}-{suffix}'
            self.slug = slug
        super().save(*args, **kwargs)


class Purchase(models.Model):
    KIND_CHOICES = [
        ('subscription', 'Subscription'),
        ('grand_test', 'Grand Test'),
        ('teacher_course', 'Teacher Course'),
    ]
    STATUS_CHOICES = [
        ('unpaid', 'Unpaid'),  # = "Awaiting Payment": order created, no proof submitted yet
        ('pending', 'Pending Verification'),
        ('resubmission_requested', 'Resubmission Requested'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('expired', 'Expired'),
        ('cancelled', 'Cancelled'),
    ]
    # Statuses that still count as "in flight or won" for coupon/referral usage
    # counting — an abandoned unpaid cart or an in-review resubmission shouldn't
    # let a student re-trigger a first-purchase-only coupon or referral discount.
    # expired/cancelled deliberately excluded: a student whose QR window lapsed
    # (or who cancelled) should still be able to try again and get the same
    # first-purchase/referral treatment, not be penalized for an abandoned order.
    OPEN_STATUSES = ('unpaid', 'pending', 'resubmission_requested', 'approved')

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='purchases')
    kind = models.CharField(max_length=15, choices=KIND_CHOICES)
    plan = models.ForeignKey(SubscriptionPlan, on_delete=models.SET_NULL, null=True, blank=True, related_name='purchases')
    grand_test = models.ForeignKey(
        'tests_app.Test', on_delete=models.SET_NULL, null=True, blank=True, related_name='purchases',
    )
    teacher_course = models.ForeignKey(
        'marketplace.TeacherCourse', on_delete=models.SET_NULL, null=True, blank=True, related_name='purchases',
    )
    coupon = models.ForeignKey(Coupon, on_delete=models.SET_NULL, null=True, blank=True, related_name='purchases')
    currency = models.CharField(max_length=3, default='NRS', help_text='NRS only — this platform does not support other currencies.')
    original_amount = models.DecimalField(max_digits=9, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=9, decimal_places=2, default=0)
    final_amount = models.DecimalField(max_digits=9, decimal_places=2)
    payment_method = models.ForeignKey(
        PaymentMethod, on_delete=models.SET_NULL, null=True, blank=True, related_name='purchases',
        help_text='Which channel the student says they paid through — still manually verified, no live gateway.',
    )
    payment_reference = models.CharField(
        max_length=150, blank=True, db_index=True,
        help_text='Bank transfer / eSewa / Khalti reference number the student provides as proof of payment.',
    )
    # Deprecated in favor of payment_screenshot_key/bucket (GCS private-bucket
    # storage — see billing/gcs.py) — kept, nullable and unused for new
    # submissions, for one release cycle only. Not backfilled: any file this
    # field previously pointed at was on Cloud Run's ephemeral local disk and
    # is already gone by the time this migration runs.
    payment_screenshot = models.ImageField(upload_to='payment_screenshots/', null=True, blank=True)
    payment_screenshot_key = models.CharField(
        max_length=255, blank=True, help_text='GCS object key (private bucket) for the submitted screenshot.',
    )
    payment_screenshot_bucket = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default='unpaid')
    admin_note = models.CharField(
        max_length=255, blank=True,
        help_text='Rejection reason or resubmission-request reason — which one is determined by status.',
    )
    expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text='QR payment window deadline (created_at + 30 min) for orders with a nonzero amount. Null for free/100%-off orders, which never expire since nothing is owed.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    # How long a QR-payable order stays in 'unpaid' before the expiry cron
    # (see PurchaseViewSet / payment_service.expire) flips it to 'expired'.
    EXPIRY_MINUTES = 30

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user} — {self.kind} (Rs.{self.final_amount}) [{self.status}]'

    @property
    def order_id(self):
        """Display-only order code derived from the PK — not a separate
        stored identifier, so there's exactly one source of truth for
        "which purchase is this" across DB lookups, URLs, and the UI."""
        return f'HM-{self.id:06d}'

    @property
    def is_expired(self):
        return bool(self.expires_at and self.status == 'unpaid' and timezone.now() > self.expires_at)


class Scholarship(models.Model):
    """An admin-granted Subscription with zero revenue — tracked separately from
    Purchase so it's excluded from every revenue figure in the analytics dashboard."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='scholarships')
    course = models.ForeignKey('courses.Course', on_delete=models.CASCADE, related_name='scholarships')
    product_type = models.CharField(max_length=20, choices=SubscriptionPlan.PRODUCT_CHOICES)
    plan = models.ForeignKey(
        SubscriptionPlan, on_delete=models.SET_NULL, null=True, blank=True, related_name='scholarships',
        help_text='Optional — a scholarship can grant access without referencing a specific priced plan.',
    )
    subscription = models.OneToOneField(
        Subscription, on_delete=models.SET_NULL, null=True, blank=True, related_name='scholarship',
    )
    reason = models.CharField(max_length=255, blank=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    granted_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-granted_at']

    def __str__(self):
        return f'{self.user} — {self.get_product_type_display()} scholarship ({self.course.prefix})'


class NotificationLog(models.Model):
    """One row per (subscription, notification_type) actually attempted — the
    dedupe key the renewal-reminder cron job checks before sending, so it never
    double-sends the same reminder even if the cron endpoint is hit repeatedly."""
    CHANNEL_CHOICES = [('email', 'Email'), ('sms', 'SMS'), ('whatsapp', 'WhatsApp'), ('push', 'Push')]
    TYPE_CHOICES = [
        ('reminder_30', '30 days before expiry'),
        ('reminder_15', '15 days before expiry'),
        ('reminder_7', '7 days before expiry'),
        ('reminder_3', '3 days before expiry'),
        ('reminder_1', '1 day before expiry'),
        ('expiry', 'On expiry'),
        ('grace_period', 'Grace period'),
        ('renewal_confirmation', 'Renewal confirmation'),
        ('payment_submitted', 'Payment submitted'),
        ('payment_approved', 'Payment approved'),
        ('payment_rejected', 'Payment rejected'),
        ('payment_expired', 'Payment expired'),
    ]
    STATUS_CHOICES = [('sent', 'Sent'), ('failed', 'Failed'), ('skipped', 'Skipped')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notification_logs')
    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, null=True, blank=True, related_name='notification_logs',
    )
    purchase = models.ForeignKey(
        Purchase, on_delete=models.CASCADE, null=True, blank=True, related_name='notification_logs',
    )
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    notification_type = models.CharField(max_length=25, choices=TYPE_CHOICES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user} — {self.notification_type} via {self.channel} [{self.status}]'


class GrandTestAccess(models.Model):
    """One student's unique password + access grant for a paid Grand Test."""
    purchase = models.OneToOneField(Purchase, on_delete=models.CASCADE, related_name='grand_test_access')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='grand_test_accesses')
    test = models.ForeignKey('tests_app.Test', on_delete=models.CASCADE, related_name='student_accesses')
    password = models.CharField(max_length=20, unique=True, blank=True)
    granted_at = models.DateTimeField(null=True, blank=True)
    email_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('user', 'test')

    def __str__(self):
        return f'{self.user} — {self.test} ({self.password})'

    def save(self, *args, **kwargs):
        if not self.password:
            chars = string.ascii_uppercase + string.digits
            self.password = 'HM-' + ''.join(random.choices(chars, k=4)) + '-' + ''.join(random.choices(chars, k=4))
        super().save(*args, **kwargs)


class PaymentAuditLog(models.Model):
    """Immutable trail of every payment state transition — who did it, what
    changed, and why. Written only by billing.payment_audit.record_payment_event(),
    called from every branch of billing.payment_service (activate/reject/expire/
    cancel) and PurchaseViewSet.submit_payment. Never updated after creation.
    Mirrors core.models.DeletionAuditLog's shape/conventions."""
    ACTION_CHOICES = [
        ('submitted', 'Payment submitted'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('resubmission_requested', 'Resubmission requested'),
        ('expired', 'Expired'),
        ('cancelled', 'Cancelled'),
    ]

    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name='audit_log')
    action = models.CharField(max_length=25, choices=ACTION_CHOICES)
    previous_status = models.CharField(max_length=25, blank=True)
    new_status = models.CharField(max_length=25)

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    # Snapshot so the log stays meaningful even after the actor's own account
    # is later deleted (actor FK goes null, this doesn't) — matches
    # DeletionAuditLog's actor_email convention.
    actor_email = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    reason = models.CharField(max_length=500, blank=True)
    metadata = models.JSONField(default=dict, blank=True, help_text='Extra structured context, e.g. {"payment_reference": "..."}.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['purchase', 'created_at'])]

    def __str__(self):
        return f'Purchase {self.purchase_id}: {self.previous_status} -> {self.new_status} ({self.action})'
