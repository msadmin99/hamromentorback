"""Notification core models (Phase 1).

Two separate tables for "a notification" vs. "a delivery attempt on one
channel" — never a single row/boolean — per the governing architecture
prompt §10: "A notification is not the same thing as a delivery... Do not
use a single boolean such as notification.sent = true, because one channel
may succeed while another fails."

Deliberately NOT built on top of, or renamed from, `billing.NotificationLog`
or `core.Announcement` — Phase 0's audit (docs/
NOTIFICATION_SYSTEM_ARCHITECTURE_AUDIT.md, sections C/D/O) found both are
real, working, narrowly-scoped models this app must coexist with, not
replace: NotificationLog is billing's own send-attempt/dedupe log for its
existing renewal-reminder cron, and Announcement is a single global
marketing banner with no user/course targeting at all. Neither is touched
by this app.
"""
from django.conf import settings
from django.db import models

from .events import CATEGORY_CHOICES, CHANNEL_CHOICES, EVENT_TYPE_CHOICES, PRIORITY_CHOICES, PRIORITY_NORMAL


class Notification(models.Model):
    """The canonical, course-aware notification record. Minimum conceptual
    fields per architecture prompt §9 — every optional relation below is
    populated per event_type, never all at once (§9: "Do not add every
    possible foreign key blindly")."""

    STATUS_SCHEDULED = 'scheduled'
    STATUS_DISPATCHED = 'dispatched'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_SCHEDULED, 'Scheduled'),
        (STATUS_DISPATCHED, 'Dispatched'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    # NULL = a global/platform-wide notification (architecture prompt §1:
    # "global notifications" must be supported alongside course-scoped
    # ones) — never a fake/sentinel course. SET_NULL, not CASCADE: a course
    # being deleted must never delete a student's notification history.
    course = models.ForeignKey(
        'courses.Course', on_delete=models.SET_NULL, null=True, blank=True, related_name='notifications',
    )

    event_type = models.CharField(max_length=40, choices=EVENT_TYPE_CHOICES)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default=PRIORITY_NORMAL)

    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    action_url = models.CharField(
        max_length=500, blank=True,
        help_text='Structured deep-link path the frontend navigates to on click — never '
                   'reconstructed from title/body text (architecture prompt §16).',
    )

    # Optional context relations — SET_NULL so deleting the underlying
    # object (e.g. a Test being removed) never cascades into deleting a
    # student's notification history; the notification just loses its
    # deep-link target, which the frontend already has to handle gracefully
    # for any stale action_url.
    test = models.ForeignKey('tests_app.Test', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    attempt = models.ForeignKey('tests_app.TestAttempt', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    purchase = models.ForeignKey('billing.Purchase', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    subscription = models.ForeignKey('billing.Subscription', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    video = models.ForeignKey('videos_app.Video', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    question = models.ForeignKey('academics.Question', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    metadata = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default=STATUS_DISPATCHED)
    # NULL = no idempotency guard requested (e.g. a one-off admin message).
    # MySQL/Django treat multiple NULLs as distinct under a unique
    # constraint, so this is safe; any non-null value MUST be unique
    # platform-wide (architecture prompt §11: "a duplicate scheduler
    # execution must not create duplicate notifications" — enforced here at
    # the database level, not just in application code).
    dedupe_key = models.CharField(max_length=255, unique=True, null=True, blank=True)
    event_id = models.CharField(
        max_length=64, blank=True,
        help_text='Correlates this row back to the domain event that raised it, for '
                   'observability (architecture prompt §41) — not a uniqueness constraint itself.',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    scheduled_for = models.DateTimeField(
        null=True, blank=True,
        help_text='NULL = deliver immediately. A future time holds this in STATUS_SCHEDULED '
                   'until a dispatch sweep (services.dispatch_due_notifications) picks it up.',
    )
    expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Past this time the notification is stale (e.g. a Grand Test reminder for '
                   'a test that already started) — a future UI/eligibility layer may choose to '
                   'stop surfacing it as new; this field alone does not delete or hide anything.',
    )
    read_at = models.DateTimeField(null=True, blank=True)
    clicked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'read_at', '-created_at']),
            models.Index(fields=['user', 'course', '-created_at']),
            models.Index(fields=['status', 'scheduled_for']),
        ]

    def __str__(self):
        return f'{self.event_type} -> user {self.user_id} [{self.status}]'


class NotificationDelivery(models.Model):
    """Per-channel delivery abstraction (architecture prompt §10). One row
    per (notification, channel) — never a single notification-level status,
    so one channel failing never masks another channel succeeding."""

    STATUS_QUEUED = 'queued'
    STATUS_PROCESSING = 'processing'
    STATUS_SENT = 'sent'
    STATUS_DELIVERED = 'delivered'
    STATUS_FAILED = 'failed'
    STATUS_OPENED = 'opened'
    STATUS_CLICKED = 'clicked'
    STATUS_SKIPPED = 'skipped'
    STATUS_CHOICES = [
        (STATUS_QUEUED, 'Queued'),
        (STATUS_PROCESSING, 'Processing'),
        (STATUS_SENT, 'Sent'),
        (STATUS_DELIVERED, 'Delivered'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_OPENED, 'Opened'),
        (STATUS_CLICKED, 'Clicked'),
        (STATUS_SKIPPED, 'Skipped'),
    ]

    notification = models.ForeignKey(Notification, on_delete=models.CASCADE, related_name='deliveries')
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_QUEUED)

    provider = models.CharField(max_length=50, blank=True)
    provider_message_id = models.CharField(max_length=255, blank=True)
    # Retry architecture (§12): attempt 1 -> retry -> attempt 2 -> retry ->
    # attempt 3 -> FAILED. MAX_ATTEMPTS in services.py is the single source
    # of truth for "3" — not duplicated here as a magic number.
    attempt_count = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    clicked_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        # One delivery row per channel per notification — a second attempt
        # on the same channel updates this row (via services.py's retry
        # helper), it never creates a sibling row.
        unique_together = ('notification', 'channel')

    def __str__(self):
        return f'notification {self.notification_id} via {self.channel} [{self.status}]'


class NotificationPreference(models.Model):
    """Per-(category, channel[, course]) explicit opt-out. Absence of a row
    means enabled — a user who has never touched their preferences still
    receives notifications; a row only ever exists to record a deliberate
    choice. `course=None` applies platform-wide for that category+channel;
    a specific course row overrides it for that course only (architecture
    prompt §27's own "course: CEE-MBBS / BDS / ..." example).

    Mandatory categories (security, billing — see events.MANDATORY_CATEGORIES)
    are enforced as always-enabled in services.is_channel_enabled(), never
    here at the model level — a row can still be *stored* for a mandatory
    category (e.g. a legacy import, or a future admin override use case)
    without that storage alone being able to actually suppress delivery."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notification_preferences',
    )
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    course = models.ForeignKey('courses.Course', on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    enabled = models.BooleanField(default=True)

    class Meta:
        unique_together = ('user', 'category', 'channel', 'course')

    def __str__(self):
        scope = self.course.name if self.course_id else 'all courses'
        return f'{self.user_id}: {self.category}/{self.channel} ({scope}) = {self.enabled}'


class PushSubscription(models.Model):
    """One real browser/device's Web Push registration (Phase 3). A user may
    have several active rows at once (MacBook Chrome, Android Chrome, ...)
    — this is a plain FK, never unique/one-to-one on `user` (docs/
    PHASE_3_WEB_PUSH_TRACEABILITY.md, P0-03).

    Deliberately NOT `accounts.Device` (that model tracks LOGIN
    sessions for a max-3-concurrent-device policy — a different concept
    with no endpoint/key fields; conflating the two would tie a push
    subscription's lifetime to an unrelated login-slot eviction).

    `endpoint` is the real uniqueness key (a W3C push-service endpoint URL
    is unique per browser-subscription by construction — the platform
    never issues the same endpoint to two different subscriptions). This
    is also the mechanism that closes the account-switching gap (P0-05):
    push_service.register_subscription() does an update_or_create keyed on
    `endpoint`, so the same browser re-registering under a different
    authenticated user REASSIGNS `user` on that one row rather than ever
    creating a second, stale one."""

    STATUS_ACTIVE = 'active'
    STATUS_STALE = 'stale'
    STATUS_REVOKED = 'revoked'
    STATUS_INVALID = 'invalid'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'Active'),
        (STATUS_STALE, 'Stale'),
        (STATUS_REVOKED, 'Revoked'),
        (STATUS_INVALID, 'Invalid'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='push_subscriptions')

    # Push subscription credentials (W3C Push API / RFC 8291). Never
    # exposed to any serializer response — see PushSubscriptionSerializer.
    endpoint = models.CharField(max_length=500, unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)

    # Cosmetic/display-only — parsed server-side from the User-Agent header
    # at registration time (services.push_service), never trusted from an
    # arbitrary client-supplied field for anything security-relevant.
    browser = models.CharField(max_length=50, blank=True)
    browser_version = models.CharField(max_length=20, blank=True)
    os = models.CharField(max_length=50, blank=True)
    device_label = models.CharField(max_length=100, blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    last_seen_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Set on registration/re-registration and on every successful delivery — '
                   'NOT auto_now, so a plain status-only save never silently bumps it.',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_seen_at']
        indexes = [
            # The exact query push_tasks.process_push_delivery runs on every
            # delivery: "this user's currently active subscriptions."
            models.Index(fields=['user', 'status']),
        ]

    def __str__(self):
        return f'{self.user_id}: {self.browser or "?"}/{self.os or "?"} [{self.status}]'


class PushDeliveryAttempt(models.Model):
    """Per-DEVICE delivery detail for ONE NotificationDelivery(channel=push)
    row (Phase 3). NotificationDelivery itself stays exactly what it always
    was — one row per (notification, channel), enforced at the DB level —
    so it cannot represent "delivered to device A, failed on device B."
    This child table is the deliberate, additive resolution documented in
    docs/PHASE_3_WEB_PUSH_TRACEABILITY.md's "Architectural decisions"
    section #2: a nullable FK + widened unique_together on
    NotificationDelivery was considered and rejected because MySQL (this
    project's real production database) treats every NULL in a unique
    index as distinct, which would have silently stopped enforcing the
    existing, relied-upon "one row per (notification, channel)" guarantee
    for every OTHER channel. Both FKs here are non-nullable, so no such
    problem exists for this table's own unique_together.

    unique_together = (delivery, subscription) is also this table's
    idempotency guarantee: a Cloud Tasks retry of the same delivery only
    ever updates the existing attempt row for a given device, it never
    creates a duplicate — see push_tasks.process_push_delivery."""

    delivery = models.ForeignKey(NotificationDelivery, on_delete=models.CASCADE, related_name='push_attempts')
    subscription = models.ForeignKey(PushSubscription, on_delete=models.CASCADE, related_name='delivery_attempts')

    # Reuses NotificationDelivery's own status vocabulary (queued/sent/
    # failed/skipped are the only ones meaningful at the per-device grain)
    # rather than inventing a second, parallel status enum.
    STATUS_QUEUED = NotificationDelivery.STATUS_QUEUED
    STATUS_SENT = NotificationDelivery.STATUS_SENT
    STATUS_FAILED = NotificationDelivery.STATUS_FAILED
    STATUS_SKIPPED = NotificationDelivery.STATUS_SKIPPED
    STATUS_CHOICES = [
        (STATUS_QUEUED, 'Queued'),
        (STATUS_SENT, 'Sent'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_SKIPPED, 'Skipped'),
    ]

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    attempt_count = models.PositiveIntegerField(default=0)
    provider_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('delivery', 'subscription')

    def __str__(self):
        return f'delivery {self.delivery_id} -> subscription {self.subscription_id} [{self.status}]'


class NotificationSettings(models.Model):
    """One row per user — quiet hours (architecture prompt §28). Timezone
    is per-user (defaulting to Asia/Kathmandu, matching the same default
    already used by tests_app.ExamSession.timezone — Dr. Gutka's primary
    audience — but never hard-coded into any notification-sending code
    path, exactly as §28 requires)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notification_settings',
    )
    quiet_hours_start = models.TimeField(null=True, blank=True)
    quiet_hours_end = models.TimeField(null=True, blank=True)
    timezone = models.CharField(max_length=50, default='Asia/Kathmandu')

    def __str__(self):
        return f'{self.user_id} notification settings'
