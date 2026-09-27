"""Notification eligibility (architecture prompt §31, §2, §4).

Phase 0's audit (docs/NOTIFICATION_SYSTEM_ARCHITECTURE_AUDIT.md, section H)
confirmed access control on this platform is genuinely fragmented — there is
no single "is this user entitled" function. This module is the scaffolding
Phase 2 populates one real event at a time as it wires actual domain
triggers; it deliberately does NOT invent an eligibility rule for an event
type that has no real caller yet.

BUSINESS-RULE FIX (course-wide exam reminder audience): the two functions
below originally also required commercial access — `billing.access.
get_grand_test_access` / `has_daily_test_access` — before a course-enrolled
student's reminder would be scheduled. That was wrong for this event type.
The product rule for an upcoming-exam REMINDER is:

    Notification eligibility = active registration in the exam's course.
    NOT: exam purchase/subscription/entitlement.

A student enrolled in a course must be told "CEE-MBBS Grand Test-I starts in
1 hour" even if they haven't bought that specific exam — the reminder is an
announcement, not a grant of access. Commercial entitlement continues to
gate EXAM ACCESS exactly as before (billing.access.get_grand_test_access /
has_daily_test_access, tests_app._start_attempt, entitlements.services.
can_start_test) — nothing in that separate system changed. Only the
notification-audience question below changed, from "is this student both
enrolled AND paying" to "is this student enrolled (and is the exam even
visible — not a draft, not individually unassigned)".

`can_view_test(user, test).allowed` (wrapping tests_app.access.
can_access_test, called by _eligible_students_for_test's caller, which only
ever offers a candidate who is already actively enrolled in one of the
exam's courses) is kept: it is documented as "entirely independent of
commercial entitlement" by its own module and exists precisely to answer
"can this student see this exam at all" (course/batch/individual assignment,
draft-state) — the one thing an upcoming-exam reminder legitimately still
needs to check (no reminder for a draft/unpublished exam), without
reintroducing a purchase/subscription gate.
"""


def is_eligible_for_grand_test_notification(user, test):
    """True if `user` is allowed to view this Grand Test at all (course/
    batch/individual assignment, draft-state — see module docstring).
    Deliberately does NOT check commercial access (billing.access.
    get_grand_test_access): a course-enrolled student receives the "exam
    starts soon" reminder whether or not they have purchased it. Commercial
    access still gates whether they can actually START the exam — that
    check lives entirely in billing.access / tests_app._start_attempt and is
    untouched here."""
    from entitlements.services import can_view_test

    return can_view_test(user, test).allowed


def is_eligible_for_daily_test_notification(user, test):
    """Same rule as is_eligible_for_grand_test_notification, for Daily Test
    reminders — see that function's and the module's docstring for why the
    commercial access check (billing.access.has_daily_test_access) was
    removed here."""
    from entitlements.services import can_view_test

    return can_view_test(user, test).allowed


def recheck_notification_eligibility(notification):
    """Delivery-time re-check (architecture prompt §31 — eligibility can
    change between scheduling and delivery: enrollment may lapse/be
    revoked, a course/batch assignment may be removed, or the exam may be
    unpublished). Only meaningful for a notification carrying a `test`
    reference; anything else (billing, announcements, etc.) has no
    re-checkable eligibility here and is always considered still eligible —
    this function only ever narrows, never invents a new denial for an
    event type it doesn't understand.

    Re-checks the SAME course-registration/view rule the schedule-time
    filter used (see is_eligible_for_grand_test_notification /
    is_eligible_for_daily_test_notification above) — never commercial
    access, for the identical business-rule reason."""
    if notification.test_id is None:
        return True
    test = notification.test
    if test.exam_type == 'grand':
        return is_eligible_for_grand_test_notification(notification.user, test)
    if test.exam_type == 'daily':
        return is_eligible_for_daily_test_notification(notification.user, test)
    return True
