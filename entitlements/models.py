"""Phase 2 Entitlement Foundation — the one genuinely new access-source model.

Every OTHER access source (Enrollment, Subscription, Scholarship,
GrandTestAccess, Test.assigned_students/assigned_batches) already exists
and stays the system of record for itself — see
docs/ENTITLEMENT_CURRENT_STATE.md and docs/ENTITLEMENT_DATA_MODEL.md for the
full inventory and the explicit "why not a central Entitlement table"
decision. This app adds only what doesn't already exist: Free Starter
(free-tier) access, plus the decision-layer service in services.py that
composes this together with the existing sources.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

RESOURCE_TYPE_CHOICES = [
    ('qbank', 'Practice / QBank (questions)'),
    ('mock_test', 'Mock Test (attempts)'),
    ('daily_test', 'Daily Test (attempts)'),
    ('grand_test', 'Grand Test (attempts)'),
    ('pyq', 'Past Year Questions (questions)'),
]


class FreeStarterPolicy(models.Model):
    """Admin-configurable free-tier quota per resource type — the single
    source of truth for "how much can a new student use for free."

    No rows are seeded by the migration (table starts empty) — quantities
    are never hardcoded in application code or in a migration; nothing is
    granted until an admin explicitly activates at least one policy row.
    Configured today via the Django admin site (entitlements/admin.py) — a
    dedicated Admin-panel screen is a later-phase UI addition, not part of
    this foundation phase (Phase 2 spec: "do not implement the entire Free
    Tier UX yet").
    """

    resource_type = models.CharField(max_length=20, choices=RESOURCE_TYPE_CHOICES, unique=True)
    quantity = models.PositiveIntegerField(
        default=0, help_text='Free quota granted to every newly-provisioned student. Ignored if "unlimited" is set.',
    )
    unlimited = models.BooleanField(default=False)
    validity_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Days after provisioning this entitlement stays valid. Blank = does not expire on its own '
                   '(only exhausts by usage). Phase 3 — never hardcoded, e.g. 7/14/30 are examples only.',
    )
    is_active = models.BooleanField(
        default=True,
        help_text='Off = no Free Starter entitlement of this resource_type is granted at registration.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['resource_type']

    def __str__(self):
        return f'{self.get_resource_type_display()} — {"unlimited" if self.unlimited else self.quantity}'


class FreeStarterEntitlement(models.Model):
    """One row per (user, resource_type) — a student's free-tier grant and
    running usage. `quantity`/`unlimited` are snapshotted from
    FreeStarterPolicy at provisioning time (not read live) so a later
    policy change never silently rewrites an already-provisioned student's
    grant — matches this platform's existing "historical rights are not
    retroactively rewritten by a later config change" pattern
    (billing.PurchaseComboItem.price is the precedent).

    Provisioned idempotently — see entitlements.provisioning.
    provision_free_starter — via the (user, resource_type) unique
    constraint below, which is also the row select_for_update() locks for
    atomic quota consumption (entitlements.provisioning.consume_free_starter).
    """

    STATUS_CHOICES = [
        ('active', 'Active'),
        ('exhausted', 'Exhausted'),
        ('expired', 'Expired'),
        ('revoked', 'Revoked'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='free_starter_entitlements',
    )
    resource_type = models.CharField(max_length=20, choices=RESOURCE_TYPE_CHOICES)
    quantity = models.PositiveIntegerField(default=0)
    unlimited = models.BooleanField(default=False)
    used = models.PositiveIntegerField(default=0)
    valid_from = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(
        null=True, blank=True, help_text='Blank = does not expire on its own (only exhausts by usage).',
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='active')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'resource_type')
        ordering = ['-created_at']

    def __str__(self):
        cap = '∞' if self.unlimited else str(self.quantity)
        return f'{self.user} — {self.resource_type} ({self.used}/{cap})'

    @property
    def remaining(self):
        if self.unlimited:
            return None
        return max(self.quantity - self.used, 0)

    @property
    def effective_status(self):
        """The status considering live expiry/exhaustion, not just the
        stored `status` field — same effective-validity principle already
        used correctly by billing.Subscription.is_current and
        billing.access._active_subscriptions, applied here for
        consistency (Phase 2 spec Step 9: effective validity is mandatory,
        never "is_active/status alone")."""
        if self.status == 'revoked':
            return 'revoked'
        if self.expires_at and self.expires_at < timezone.now():
            return 'expired'
        if not self.unlimited and self.used >= self.quantity:
            return 'exhausted'
        return 'active'

    @property
    def is_currently_valid(self):
        return self.effective_status == 'active'


class EntitlementEventLog(models.Model):
    """Append-only audit trail for entitlement lifecycle events (Phase 2
    spec Step 23) — mirrors the existing DeletionAuditLog/PaymentAuditLog/
    AdminEditAuditLog pattern already used elsewhere in this codebase
    rather than inventing a new logging convention. No sensitive personal
    data beyond the user/actor reference is recorded."""

    EVENT_CHOICES = [
        ('created', 'Created'),
        ('consumed', 'Consumed'),
        ('exhausted', 'Exhausted'),
        ('expired', 'Expired'),
        ('revoked', 'Revoked'),
        ('restored', 'Restored'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Who/what caused this event — an admin for a manual action, blank for a system-triggered one.',
    )
    resource_type = models.CharField(max_length=30, blank=True)
    event = models.CharField(max_length=15, choices=EVENT_CHOICES)
    detail = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', 'resource_type'])]

    def __str__(self):
        return f'{self.user} — {self.event} ({self.resource_type})'
