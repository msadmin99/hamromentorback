"""Push subscription lifecycle (Phase 3) — registration, listing, revocation.

Kept separate from services.py (Phase 1's Notification/NotificationDelivery
write path) the same way exam_integration.py/billing_integration.py are
kept separate from it — one small, focused module per concern, matching
this app's own established convention.
"""
import re

from django.utils import timezone as dj_timezone

from .models import PushSubscription

# Deliberately tiny, dependency-free User-Agent parsing — cosmetic fields
# only (browser/browser_version/os are never used for any security or
# delivery decision, only display in the device-management UI, P1-02), so
# a real UA-parsing library is not justified for this. Order matters: Edge
# and Opera both contain "Chrome" in their UA string, so they must be
# checked before the generic Chrome pattern.
_BROWSER_PATTERNS = [
    ('Edge', re.compile(r'Edg(?:A|iOS)?/([\d.]+)')),
    ('Opera', re.compile(r'(?:OPR|Opera)/([\d.]+)')),
    ('Chrome', re.compile(r'Chrome/([\d.]+)')),
    ('Firefox', re.compile(r'Firefox/([\d.]+)')),
    ('Safari', re.compile(r'Version/([\d.]+).*Safari')),
]
_OS_PATTERNS = [
    ('Android', re.compile(r'Android')),
    ('iOS', re.compile(r'iPhone|iPad|iPod')),
    ('Windows', re.compile(r'Windows')),
    ('macOS', re.compile(r'Mac OS X')),
    ('Linux', re.compile(r'Linux')),
]


def parse_user_agent(user_agent):
    """Best-effort, never raises. Returns (browser, browser_version, os) —
    any field left blank if not recognized, never guessed."""
    user_agent = user_agent or ''
    browser, version = '', ''
    for name, pattern in _BROWSER_PATTERNS:
        match = pattern.search(user_agent)
        if match:
            browser, version = name, match.group(1)
            break
    os_name = ''
    for name, pattern in _OS_PATTERNS:
        if pattern.search(user_agent):
            os_name = name
            break
    return browser, version, os_name


def register_subscription(user, endpoint, p256dh, auth, device_label='', user_agent=''):
    """Idempotent by `endpoint` (P0-02): a second registration of the same
    browser subscription updates the existing row — including reassigning
    `user` if a different, now-authenticated user registers the identical
    endpoint (P0-05's account-switching resolution; see docs/
    PHASE_3_WEB_PUSH_TRACEABILITY.md's architectural decision #6 for why
    this is deliberate, not accidental). Always reactivates `status` to
    ACTIVE — re-registering a previously invalid/revoked/stale subscription
    is exactly how a browser tells us "this is good again."""
    browser, browser_version, os_name = parse_user_agent(user_agent)
    subscription, _created = PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            'user': user,
            'p256dh': p256dh,
            'auth': auth,
            'browser': browser,
            'browser_version': browser_version,
            'os': os_name,
            'device_label': device_label or '',
            'status': PushSubscription.STATUS_ACTIVE,
            'last_seen_at': dj_timezone.now(),
        },
    )
    return subscription


def list_subscriptions(user):
    return PushSubscription.objects.filter(user=user).order_by('-last_seen_at')


def revoke_subscription(user, endpoint):
    """Ownership-scoped (P0-04) — filters by `user` as part of the lookup
    itself, so a different user's endpoint simply doesn't match (no row
    updated), never a cross-user mutation. Returns True if a row was
    actually revoked."""
    updated = PushSubscription.objects.filter(user=user, endpoint=endpoint).update(
        status=PushSubscription.STATUS_REVOKED,
    )
    return updated > 0


def has_active_subscription(user):
    """The one function notifications.services.create_notification calls
    to decide whether CHANNEL_PUSH belongs in a notification's automatic
    channel resolution (docs/PHASE_3_WEB_PUSH_TRACEABILITY.md, decision
    #1) — kept as its own named function rather than an inline queryset
    so that resolution logic reads as one sentence at the call site."""
    return PushSubscription.objects.filter(user=user, status=PushSubscription.STATUS_ACTIVE).exists()
