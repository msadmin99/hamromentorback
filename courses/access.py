"""Shared academic-eligibility helpers — the single source of truth for
"which courses is this user actually enrolled in", reused by tests_app and
academics so Test/Question visibility can never drift out of sync with each
other or be derived two different ways."""


def eligible_course_ids(user):
    """Active-enrollment course IDs for this user. Empty set for anonymous
    users or anyone with no active enrollment — deliberately fails closed."""
    if not user or not user.is_authenticated:
        return set()
    from .models import Enrollment

    return set(Enrollment.objects.filter(user=user, is_active=True).values_list('course_id', flat=True))


def eligible_batch_ids(user):
    """Active-enrollment batch IDs for this user (their cohort within each
    enrolled course, where set) — used for batch-scoped exam assignment."""
    if not user or not user.is_authenticated:
        return set()
    from .models import Enrollment

    return set(
        Enrollment.objects.filter(user=user, is_active=True, batch__isnull=False).values_list('batch_id', flat=True)
    )
