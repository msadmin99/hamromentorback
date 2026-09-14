"""Email content rendering (Phase 4).

Deliberately separate from `email_adapter.py` (which only knows HOW to
send) — this module only knows WHAT to send, and never imports
`django.core.mail`/`smtplib` at all.

Subject design (docs/PHASE_4_EMAIL_TRACEABILITY_AND_DESIGN.md §10/§15):
`Notification.title` is NOT reused as the email subject. By the time a
delivery is being sent, the event-specific placeholders that originally
built `title`/`body` (via `notifications/templates.py`'s own
`.format(**context)`) are already resolved into plain strings — the raw
context isn't preserved on `Notification` (deliberately: Phase 1 never
added a raw-context field, and this phase doesn't add one either, per the
"no migration" finding, docs/PHASE_4_EMAIL_TRACEABILITY_AND_DESIGN.md
§26). So EMAIL_SUBJECTS below is a plain, deterministic, testable
`event_type -> subject` mapping — no placeholders to fill, no lost-context
problem, and a course name is prefixed when the notification has one,
exactly matching how title text already reads today.
"""
from urllib.parse import urljoin

from django.conf import settings
from django.template.loader import render_to_string

# One deterministic subject per event type this project's real event
# producers actually raise (docs/PHASE_4_EMAIL_TRACEABILITY_AND_DESIGN.md
# §3's own traceability matrix) — never generated from unescaped/dynamic
# text. An event type with no entry here falls back to
# `notification.title` (see resolve_subject) rather than crashing —
# vocabulary in `events.py` is allowed to grow ahead of every channel
# having a bespoke subject for it (the same "don't force every event to
# exist everywhere immediately" principle Phase 1 already established
# for notifications/templates.py itself).
EMAIL_SUBJECTS = {
    'GRAND_TEST_SCHEDULED': 'Grand Test Scheduled',
    'GRAND_TEST_REMINDER': 'Grand Test Reminder',
    'GRAND_TEST_STARTING': 'Grand Test Starting Soon',
    'GRAND_TEST_RESULT_AVAILABLE': 'Your Grand Test Result is Available',
    'DAILY_TEST_AVAILABLE': 'Daily Test Available',
    'DAILY_TEST_ENDING': 'Daily Test Ending Soon',
    'DAILY_TEST_RESULT_AVAILABLE': 'Your Daily Test Result is Available',
    'PAYMENT_APPROVED': 'Payment Approved',
    'SUBSCRIPTION_ACTIVATED': 'Subscription Activated',
    'SUBSCRIPTION_EXPIRING': 'Your Access Expires Soon',
    'ANNOUNCEMENT': 'Dr. Gutka Announcement',
}


def resolve_subject(notification):
    """Deterministic, testable — same event_type always produces the same
    base subject, course-prefixed when the notification has one (e.g.
    "CEE-MBBS — Grand Test Reminder"), matching how in-app/push titles
    already read today. Falls back to the notification's own title for
    any event_type not yet in EMAIL_SUBJECTS, so a future event type never
    breaks email delivery — it just gets a less-curated subject until this
    dict is extended."""
    base = EMAIL_SUBJECTS.get(notification.event_type, notification.title)
    if notification.course_id:
        return f'{notification.course.name} — {base}'
    return base


def build_absolute_url(action_url):
    """Turns the notification's own server-set, relative action_url
    (e.g. "/grand-test/82") into an absolute link an email client can
    actually follow — action_url is never derived from user input
    anywhere in this codebase (confirmed, same guarantee Phase 3's push
    payload already relies on), so this never needs to sanitize
    attacker-controlled input, only to resolve a relative path."""
    if not action_url:
        return ''
    if action_url.startswith('http://') or action_url.startswith('https://'):
        return action_url  # already absolute — no current caller does this, but never double-prefix if one ever does
    return urljoin(settings.FRONTEND_URL, action_url)


def render_email(notification):
    """Returns (subject, plain_text_body, html_body) for one Notification.

    Plain text is `Notification.body` verbatim — already exists, zero new
    rendering work (docs/PHASE_4_EMAIL_TRACEABILITY_AND_DESIGN.md §15).
    HTML is rendered through Django's own template engine
    (`notifications/templates/notifications/email_base.html`), which
    autoescapes every interpolated value by default — this is the actual
    P0-5/P0-7 HTML-injection defense (Announcement.message and every
    other interpolated field are escaped by Django's template engine
    itself, not by any custom logic in this module), never
    `{% autoescape off %}` anywhere in that template."""
    subject = resolve_subject(notification)
    cta_url = build_absolute_url(notification.action_url)
    html_body = render_to_string('notifications/email_base.html', {
        'title': notification.title,
        'body': notification.body,
        'course_name': notification.course.name if notification.course_id else '',
        'cta_url': cta_url,
        'cta_label': 'Open Dr. Gutka',
    })
    return subject, notification.body, html_body
