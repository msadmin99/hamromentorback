"""Notification template rendering (Phase 1).

Mirrors the existing pattern in billing/notifications.py's MESSAGES dict —
plain Python `.format()` templates, not hard-coded strings scattered across
call sites (§22: "Do not hard-code email HTML into business logic" — the
same principle applies here even though this phase only renders in-app
title/body text, not HTML). Kept as a Python dict, not a database model:
Phase 1 has exactly one channel (in-app, plain text), so a DB-backed
template editor would be premature — Phase 7's Admin "Templates" screen is
where that becomes worth building, once real content-editing need exists.

Seeded with the "highest-value events" the governing prompt's §18 names as
what Phase 2 should connect first — real, tested starter templates, not a
claim that every event in events.py has one yet (per §8: "Do not implement
every future event immediately"). A caller may always pass title/body
directly to services.create_notification() instead of using a template;
nothing requires going through this module.
"""
# event_type -> (title_template, body_template). Placeholders are filled by
# `render()`'s **context — a missing key raises KeyError immediately
# (loudly) rather than silently emitting a half-formatted string with a
# literal "{course}" left in it.
TEMPLATES = {
    'GRAND_TEST_SCHEDULED': (
        '{course} Grand Test scheduled',
        '{test_title} is scheduled for {start_time}.',
    ),
    'GRAND_TEST_REMINDER': (
        '{course} Grand Test reminder',
        '{test_title} starts in {time_remaining} ({start_time}).',
    ),
    'GRAND_TEST_STARTING': (
        '{course} Grand Test starting now',
        '{test_title} is starting now.',
    ),
    'GRAND_TEST_RESULT_AVAILABLE': (
        '{course} Grand Test result available',
        'Your result for {test_title} is now available.',
    ),
    'DAILY_TEST_AVAILABLE': (
        "Today's Daily Test is ready",
        "{course}'s Daily Test for today is now available.",
    ),
    'DAILY_TEST_ENDING': (
        'Daily Test ending soon',
        "{course}'s Daily Test closes soon — attempt it before it ends.",
    ),
    'DAILY_TEST_RESULT_AVAILABLE': (
        'Daily Test result available',
        'Your Daily Test result for {course} is now available.',
    ),
    'PAYMENT_APPROVED': (
        'Payment verified — {item} is now active',
        'Your payment for {item} has been verified and is now active.',
    ),
    'SUBSCRIPTION_ACTIVATED': (
        '{course} subscription activated',
        'Your {course} subscription is now active.',
    ),
    'SUBSCRIPTION_EXPIRING': (
        '{course} access expires soon',
        'Your {course} access expires on {expiry}. Renew to keep uninterrupted access.',
    ),
    'ANNOUNCEMENT': (
        'Dr. Gutka',
        '{message}',
    ),
}

def has_template(event_type):
    return event_type in TEMPLATES


def render(event_type, context=None):
    """Returns (title, body) for `event_type`, filling `context` into the
    template. Raises KeyError if no template is registered for this
    event_type (use has_template() first if a caller needs to fall back to
    directly-supplied title/body instead), or if `context` is missing a
    placeholder the template needs — both deliberately loud, never a
    silently half-formatted message reaching a real user."""
    title_tpl, body_tpl = TEMPLATES[event_type]
    context = context or {}
    return title_tpl.format(**context), body_tpl.format(**context)
