"""Central notification event vocabulary (Phase 1 — Notification Core).

Per the governing architecture prompt §8 and this app's own Phase 0 audit
(docs/NOTIFICATION_SYSTEM_ARCHITECTURE_AUDIT.md, section P): a plain choices
list, not a database table — adding a new event type is a one-line addition
here, never a migration or a redesign of anything that consumes it.

Phase 1 defines this full vocabulary so `Notification.event_type` has a
real, closed value space from day one, but does NOT wire any real domain
code (payment_service.py, exam scheduling, etc.) to actually raise these
events yet — per the prompt's own phase boundary, that connection work is
Phase 2's job (§18 "PHASE 2 EVENTS TO CONNECT FIRST"). Every event below is
a name the *rest* of the platform can call `create_notification(event_type=...)`
with once that wiring happens; none of them fire on their own today.
"""

# Every event's category — drives NotificationPreference grouping (§27)
# and which events a "mandatory, cannot be disabled" rule applies to.
CATEGORY_TESTS = 'tests'
CATEGORY_RESULTS = 'results'
CATEGORY_LEARNING = 'learning'
CATEGORY_COURSES = 'courses'
CATEGORY_BILLING = 'billing'
CATEGORY_ANNOUNCEMENTS = 'announcements'
CATEGORY_MARKETING = 'marketing'
CATEGORY_SECURITY = 'security'

CATEGORY_CHOICES = [
    (CATEGORY_TESTS, 'Tests'),
    (CATEGORY_RESULTS, 'Results'),
    (CATEGORY_LEARNING, 'Learning'),
    (CATEGORY_COURSES, 'Courses'),
    (CATEGORY_BILLING, 'Billing'),
    (CATEGORY_ANNOUNCEMENTS, 'Announcements'),
    (CATEGORY_MARKETING, 'Marketing'),
    (CATEGORY_SECURITY, 'Security'),
]

# Per the architecture prompt §27: "Do NOT allow users to disable security/
# mandatory transactional messages." Enforced in services.is_channel_enabled()
# — never bypassable via the preferences API, regardless of what rows a user
# has saved. Billing is included because payment/subscription-state changes
# are transactional facts about the user's own account/access, the same
# class of message the prompt calls "mandatory," not a marketing send.
MANDATORY_CATEGORIES = {CATEGORY_SECURITY, CATEGORY_BILLING}

# event_type -> category. Every event_type below appears in exactly one
# category; EVENT_TYPE_CHOICES and EVENT_CATEGORY are derived from this one
# table so the two can never drift apart.
_EVENT_DEFINITIONS = {
    # ACCOUNT / SECURITY
    'USER_REGISTERED': CATEGORY_SECURITY,
    'EMAIL_VERIFIED': CATEGORY_SECURITY,
    'PASSWORD_CHANGED': CATEGORY_SECURITY,
    'LOGIN_SECURITY_ALERT': CATEGORY_SECURITY,
    # COURSE
    'COURSE_ENROLLED': CATEGORY_COURSES,
    'COURSE_ACCESS_EXPIRING': CATEGORY_COURSES,
    'COURSE_ACCESS_EXPIRED': CATEGORY_COURSES,
    # BILLING
    'PURCHASE_CREATED': CATEGORY_BILLING,
    'PAYMENT_PENDING': CATEGORY_BILLING,
    'PAYMENT_APPROVED': CATEGORY_BILLING,
    'PAYMENT_REJECTED': CATEGORY_BILLING,
    'PAYMENT_FAILED': CATEGORY_BILLING,
    'SUBSCRIPTION_ACTIVATED': CATEGORY_BILLING,
    'SUBSCRIPTION_EXPIRING': CATEGORY_BILLING,
    'SUBSCRIPTION_EXPIRED': CATEGORY_BILLING,
    # DAILY TEST
    'DAILY_TEST_PUBLISHED': CATEGORY_TESTS,
    'DAILY_TEST_AVAILABLE': CATEGORY_TESTS,
    'DAILY_TEST_ENDING': CATEGORY_TESTS,
    'DAILY_TEST_CLOSED': CATEGORY_TESTS,
    'DAILY_TEST_RESULT_AVAILABLE': CATEGORY_RESULTS,
    # MOCK TEST
    'MOCK_TEST_AVAILABLE': CATEGORY_TESTS,
    'MOCK_TEST_RESULT_AVAILABLE': CATEGORY_RESULTS,
    # GRAND TEST
    'GRAND_TEST_SCHEDULED': CATEGORY_TESTS,
    'GRAND_TEST_REMINDER': CATEGORY_TESTS,
    'GRAND_TEST_STARTING': CATEGORY_TESTS,
    'GRAND_TEST_LIVE': CATEGORY_TESTS,
    'GRAND_TEST_ENDING': CATEGORY_TESTS,
    'GRAND_TEST_CLOSED': CATEGORY_TESTS,
    'GRAND_TEST_RESULT_AVAILABLE': CATEGORY_RESULTS,
    'GRAND_TEST_RANK_AVAILABLE': CATEGORY_RESULTS,
    # QBANK / LEARNING
    'NEW_QUESTIONS_AVAILABLE': CATEGORY_LEARNING,
    'SMART_PRACTICE_READY': CATEGORY_LEARNING,
    'REVISION_DUE': CATEGORY_LEARNING,
    'WEAK_TOPIC_DETECTED': CATEGORY_LEARNING,
    'LEARNING_STREAK': CATEGORY_LEARNING,
    # VIDEO
    'NEW_VIDEO_AVAILABLE': CATEGORY_LEARNING,
    'NEW_CHAPTER_AVAILABLE': CATEGORY_LEARNING,
    # SYSTEM
    'ANNOUNCEMENT': CATEGORY_ANNOUNCEMENTS,
    'MAINTENANCE': CATEGORY_ANNOUNCEMENTS,
    'IMPORTANT_SYSTEM_ALERT': CATEGORY_ANNOUNCEMENTS,
}

EVENT_TYPE_CHOICES = [(name, name.replace('_', ' ').title()) for name in _EVENT_DEFINITIONS]
EVENT_CATEGORY = dict(_EVENT_DEFINITIONS)

CHANNEL_IN_APP = 'in_app'
CHANNEL_PUSH = 'push'
CHANNEL_EMAIL = 'email'
CHANNEL_SMS = 'sms'
CHANNEL_WHATSAPP = 'whatsapp'

CHANNEL_CHOICES = [
    (CHANNEL_IN_APP, 'In-App'),
    (CHANNEL_PUSH, 'Web Push'),
    (CHANNEL_EMAIL, 'Email'),
    (CHANNEL_SMS, 'SMS'),
    (CHANNEL_WHATSAPP, 'WhatsApp'),
]

# Phase 1 builds only the in-app channel end-to-end (per the governing
# prompt's own instruction — "Do NOT build Web Push/Email/SMS/WhatsApp
# provider integration yet... At this stage, in-app is the first real
# channel"). The other four are real, valid choices on NotificationDelivery
# today (so the schema never needs to change when Phases 3-6 add them) but
# services.create_notification() only ever attempts real delivery for
# CHANNEL_IN_APP — see that module's own docstring.
IMPLEMENTED_CHANNELS = {CHANNEL_IN_APP}

PRIORITY_LOW = 'low'
PRIORITY_NORMAL = 'normal'
PRIORITY_HIGH = 'high'
PRIORITY_CRITICAL = 'critical'

PRIORITY_CHOICES = [
    (PRIORITY_LOW, 'Low'),
    (PRIORITY_NORMAL, 'Normal'),
    (PRIORITY_HIGH, 'High'),
    (PRIORITY_CRITICAL, 'Critical'),
]


def category_for_event(event_type):
    """Raises KeyError for an unregistered event_type — deliberately not a
    silent `.get(..., default)`: a caller passing a typo'd or new-but-
    unregistered event_type must fail loudly at creation time, not create a
    Notification with a wrong/blank category (which would silently escape
    the mandatory-category and preference-filtering logic in services.py)."""
    return EVENT_CATEGORY[event_type]
