"""Shared academic-eligibility helpers — the single source of truth for
"which courses is this user actually enrolled in", reused by tests_app and
academics so Test/Question visibility can never drift out of sync with each
other or be derived two different ways."""


def eligible_course_ids(user):
    """Active-enrollment course IDs for this user. Empty set for anonymous
    users or anyone with no active enrollment — deliberately fails closed.

    FIX (Phase 2 Entitlement Foundation): this used to filter only
    `is_active=True`, never `expires_at` — an expired package enrollment
    stayed fully eligible indefinitely, since nothing in the codebase ever
    flips `is_active` to False on expiry. Now filters `expires_at` exactly
    like billing.access._active_subscriptions/has_video_access's course
    branch already correctly do, for consistency. See
    docs/ENTITLEMENT_DATA_MODEL.md "Fix 1" for the full evidence that this
    can only remove access from already-expired rows, never grant new
    access."""
    if not user or not user.is_authenticated:
        return set()
    from django.db.models import Q
    from django.utils import timezone

    from .models import Enrollment

    return set(
        Enrollment.objects.filter(user=user, is_active=True)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gte=timezone.now()))
        .values_list('course_id', flat=True)
    )


def eligible_batch_ids(user):
    """Active-enrollment batch IDs for this user (their cohort within each
    enrolled course, where set) — used for batch-scoped exam assignment.
    Same expires_at fix as eligible_course_ids above, for the same reason."""
    if not user or not user.is_authenticated:
        return set()
    from django.db.models import Q
    from django.utils import timezone

    from .models import Enrollment

    return set(
        Enrollment.objects.filter(user=user, is_active=True, batch__isnull=False)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gte=timezone.now()))
        .values_list('batch_id', flat=True)
    )
