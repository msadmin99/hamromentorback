"""Notification eligibility (architecture prompt §31, §2, §4).

Phase 0's audit (docs/NOTIFICATION_SYSTEM_ARCHITECTURE_AUDIT.md, section H)
confirmed access control on this platform is genuinely fragmented — there is
no single "is this user entitled" function. This module is the scaffolding
Phase 2 populates one real event at a time as it wires actual domain
triggers; it deliberately does NOT invent an eligibility rule for an event
type that has no real caller yet.

The one function below is real and working today — a template for every
future one, not a placeholder. It calls the EXISTING, already-hardened
functions (never re-derives their logic):

  - billing.access.get_grand_test_access(user, test)   — commercial access
  - entitlements.services.can_view_test(user, test)    — object-level view
    capability (course/batch/draft-state scoping, per that module's own
    CanView semantics)

Per §31, this check is meant to be called BOTH at schedule time and again
immediately before delivery for any access-sensitive event — the second
check matters because a subscription can expire, a course enrollment can
change, or a test can be unpublished in the gap between the two. Phase 1
provides the function; Phase 2's real Grand Test reminder scheduler is what
actually calls it twice.
"""


def is_eligible_for_grand_test_notification(user, test):
    """True only if `user` currently both (a) holds commercial access to
    this Grand Test and (b) is allowed to view it at all (course/batch/
    draft-state scoping). Never re-implements either check — see module
    docstring for exactly which existing functions this defers to."""
    from billing.access import get_grand_test_access
    from entitlements.services import can_view_test

    if not get_grand_test_access(user, test):
        return False
    return can_view_test(user, test).allowed


def is_eligible_for_daily_test_notification(user, test):
    """Phase 2 — same pattern as the Grand Test check above, for Daily
    Test's own authoritative commercial + view-eligibility functions."""
    from billing.access import has_daily_test_access
    from entitlements.services import can_view_test

    if not has_daily_test_access(user, test):
        return False
    return can_view_test(user, test).allowed


def recheck_notification_eligibility(notification):
    """Delivery-time re-check (architecture prompt §31 — eligibility can
    change between scheduling and delivery: subscription may expire,
    enrollment may change, access may be revoked). Only meaningful for a
    notification carrying a `test` reference; anything else (billing,
    announcements, etc.) has no re-checkable eligibility here and is
    always considered still eligible — this function only ever narrows,
    never invents a new denial for an event type it doesn't understand."""
    if notification.test_id is None:
        return True
    test = notification.test
    if test.exam_type == 'grand':
        return is_eligible_for_grand_test_notification(notification.user, test)
    if test.exam_type == 'daily':
        return is_eligible_for_daily_test_notification(notification.user, test)
    return True
