"""Notification service layer (Phase 1) — the single write path.

Every notification is created by calling `create_notification()` here —
never `Notification.objects.create()` directly from anywhere else — so
idempotency (§11), channel/preference resolution (§27), and mandatory-
category enforcement can never be silently bypassed by a call site.

Phase 1 built in-app as the first real channel. Phase 3 added Web Push as
the second, Phase 4 adds Email as the third: `_resolve_channels()` (below)
decides, per notification, whether CHANNEL_PUSH/CHANNEL_EMAIL belong
alongside CHANNEL_IN_APP — no Phase 2 event producer (exam_integration.py,
billing_integration.py, signals.py) had to change to participate for
either (docs/PHASE_3_WEB_PUSH_TRACEABILITY.md decision #1;
docs/PHASE_4_EMAIL_TRACEABILITY_AND_DESIGN.md §5 reaches the identical
conclusion for email). A delivery row is still created for any other
requested channel (SMS/WhatsApp, left in STATUS_QUEUED) so Phases 5-6 can
pick it up later without a schema or call-site change.

Transaction safety (§13): `create_notification` is atomic — either the
Notification and all its NotificationDelivery rows are created together, or
none are. In-app delivery has no external provider, so it is safe to
attempt inside this same atomic block. Web Push and Email both DO have a
real external provider — neither's actual send is ever attempted here;
`create_notification` only ever registers a `transaction.on_commit()`
callback that enqueues a Cloud Tasks job (see notifications/push_tasks.py
/ email_tasks.py), so no provider network call ever happens before this
transaction (or any transaction wrapping this call) actually commits.
"""
from django.db import transaction
from django.utils import timezone as dj_timezone

from .events import (
    CHANNEL_EMAIL,
    CHANNEL_IN_APP,
    CHANNEL_PUSH,
    IMPLEMENTED_CHANNELS,
    MANDATORY_CATEGORIES,
    PRIORITY_NORMAL,
    category_for_event,
)
from .models import Notification, NotificationDelivery

# Retry architecture (§12): attempt 1 -> retry -> attempt 2 -> retry ->
# attempt 3 -> FAILED. Single source of truth for "3" — no channel-specific
# provider integration exists yet to actually exercise this, but the retry
# *state machine* (attempt_count, when to stop) is core infrastructure per
# §14's own "retry state" bullet, so it's built now against the one real
# failure mode Phase 1 can genuinely produce: an unexpected exception raised
# while attempting delivery.
MAX_DELIVERY_ATTEMPTS = 3


def is_channel_enabled(user, category, channel, course=None):
    """Preference resolution (§27). Mandatory categories (security, billing
    — see events.MANDATORY_CATEGORIES) are always enabled, regardless of
    any stored preference row — enforced here in the service layer, not
    only hidden in a future preferences UI, so a direct/forged API call
    can never disable a mandatory message either (§39's spirit).

    Precedence otherwise: a course-specific row (if one exists for this
    exact course) wins; else a platform-wide (course=None) row; else the
    default of enabled=True (a user who has never touched their
    preferences still receives notifications)."""
    if category in MANDATORY_CATEGORIES:
        return True

    from .models import NotificationPreference

    if course is not None:
        row = NotificationPreference.objects.filter(
            user=user, category=category, channel=channel, course=course,
        ).first()
        if row is not None:
            return row.enabled

    row = NotificationPreference.objects.filter(
        user=user, category=category, channel=channel, course__isnull=True,
    ).first()
    if row is not None:
        return row.enabled
    return True


def _deliver_in_app(delivery):
    """In-app has no external provider and therefore no failure mode to
    model at this phase — the "delivery" IS the database row Phase 2's
    frontend API reads. Marking it delivered immediately at creation is
    accurate, not a shortcut: there is nothing further for this channel to
    do until a student's client actually fetches/opens it."""
    now = dj_timezone.now()
    delivery.status = NotificationDelivery.STATUS_DELIVERED
    delivery.attempt_count = 1
    delivery.sent_at = now
    delivery.delivered_at = now
    delivery.save(update_fields=['status', 'attempt_count', 'sent_at', 'delivered_at', 'updated_at'])


def record_delivery_failure(delivery, error_code='', error_message=''):
    """Retry-state transition on a genuine delivery exception (§12/§42 — no
    silent `except Exception: pass`). Increments attempt_count; once
    MAX_DELIVERY_ATTEMPTS is reached the delivery is terminally FAILED,
    otherwise it stays QUEUED for a future retry sweep to pick up (the
    actual re-attempt scheduling is a Phase 3+ concern once a real provider
    exists to retry against)."""
    now = dj_timezone.now()
    delivery.attempt_count += 1
    delivery.error_code = error_code[:100]
    delivery.error_message = error_message[:2000]
    if delivery.attempt_count >= MAX_DELIVERY_ATTEMPTS:
        delivery.status = NotificationDelivery.STATUS_FAILED
        delivery.failed_at = now
    else:
        delivery.status = NotificationDelivery.STATUS_QUEUED
    delivery.save(update_fields=['status', 'attempt_count', 'error_code', 'error_message', 'failed_at', 'updated_at'])
    return delivery


def _resolve_channels(user, category):
    """Phase 3's channel fan-out decision point (docs/
    PHASE_3_WEB_PUSH_TRACEABILITY.md, architectural decision #1): applied
    ONLY when a caller does not explicitly pass `channels=` — every Phase 2
    event producer (exam_integration.py, billing_integration.py,
    signals.py) calls create_notification() without `channels=`, so this
    is the one place that decides fan-out for all of them, with zero
    change needed to any of those files. An explicit `channels=` argument
    always bypasses this entirely (used by tests and any future caller
    that needs an exact, forced channel set).

    In-app is always included, unchanged from Phase 1/2. Push is added
    only if the user actually has an active browser subscription —
    `is_channel_enabled`'s preference check still runs per-channel in the
    caller below regardless, so this function only ever decides "is push
    even worth considering," never "is it allowed" (that stays
    is_channel_enabled's job alone, exactly as for every other channel).

    Phase 4 — email uses the identical shape, but the condition is
    simpler than push's: `accounts.User.email` is a required, unique,
    non-blank field (docs/PHASE_4_EMAIL_TRACEABILITY_AND_DESIGN.md §2) —
    there is no "does this user have an active device" question the way
    there is for push, only a defensive truthiness check (never crashes
    on an edge-case blank value, e.g. an unsaved test User). Preference
    enforcement, exactly like push, stays entirely in is_channel_enabled
    below — this function only ever decides whether email is even a
    candidate, never whether it is allowed."""
    from . import push_service

    channels = [CHANNEL_IN_APP]
    if push_service.has_active_subscription(user):
        channels.append(CHANNEL_PUSH)
    if user.email:
        channels.append(CHANNEL_EMAIL)
    return tuple(channels)


@transaction.atomic
def create_notification(
    user, event_type, title, body='', *,
    course=None, action_url='', priority=None,
    test=None, attempt=None, purchase=None, subscription=None, video=None, question=None,
    metadata=None, dedupe_key=None, event_id='',
    scheduled_for=None, expires_at=None,
    channels=None,
):
    """Create one Notification plus one NotificationDelivery per resolved
    channel. Returns the Notification (existing or newly created).

    Idempotent by `dedupe_key` (§11): a second call with the same
    dedupe_key returns the EXISTING Notification and creates no new rows
    at all — safe to call from a scheduler/task that may run more than
    once for the same logical event (e.g. `grand_test:82:user:123:tminus60`,
    the exact example format §11 gives). Pass dedupe_key=None (the default)
    for a genuinely one-off message where no such guard is meaningful.

    `channels=None` (the default) means "resolve automatically" — see
    _resolve_channels above; every existing Phase 2 caller relies on this
    default and needs no change. Passing an explicit tuple (e.g.
    `channels=(CHANNEL_IN_APP,)`) forces exactly that set, bypassing
    automatic resolution entirely — used by tests that need a fixed,
    predictable channel list regardless of a test user's subscriptions.
    """
    category = category_for_event(event_type)
    priority = priority or PRIORITY_NORMAL
    if channels is None:
        channels = _resolve_channels(user, category)

    if dedupe_key:
        existing = Notification.objects.filter(dedupe_key=dedupe_key).first()
        if existing is not None:
            return existing

    notification = Notification.objects.create(
        user=user, course=course, event_type=event_type, category=category, priority=priority,
        title=title, body=body, action_url=action_url,
        test=test, attempt=attempt, purchase=purchase, subscription=subscription, video=video, question=question,
        metadata=metadata or {}, dedupe_key=dedupe_key, event_id=event_id,
        scheduled_for=scheduled_for, expires_at=expires_at,
        status=Notification.STATUS_SCHEDULED if scheduled_for else Notification.STATUS_DISPATCHED,
    )

    for channel in channels:
        if not is_channel_enabled(user, category, channel, course=course):
            NotificationDelivery.objects.create(
                notification=notification, channel=channel, status=NotificationDelivery.STATUS_SKIPPED,
            )
            continue

        delivery = NotificationDelivery.objects.create(notification=notification, channel=channel)

        if scheduled_for is not None:
            continue  # picked up later by dispatch_due_notifications, exactly like in-app

        if channel == CHANNEL_IN_APP and channel in IMPLEMENTED_CHANNELS:
            try:
                _deliver_in_app(delivery)
            except Exception as exc:  # noqa: BLE001 — never a silent `pass` (§42): always recorded.
                record_delivery_failure(delivery, error_code='unexpected_error', error_message=str(exc))

        elif channel == CHANNEL_PUSH:
            # Phase 3: never call the push provider synchronously inside
            # this atomic block (architecture rule — no provider traffic
            # inside a core business transaction). on_commit is correct
            # regardless of whether an outer transaction wraps this call
            # (immediate if none does, deferred until the real commit if
            # one does) — same discipline Phase 2 already established for
            # billing/exam integrations.
            from . import push_tasks
            transaction.on_commit(lambda delivery_id=delivery.id: push_tasks.enqueue_push_delivery_task(delivery_id))

        elif channel == CHANNEL_EMAIL:
            # Phase 4 — identical on_commit discipline as push, for the
            # identical reason: the SMTP call is a real external provider
            # call and must never happen before this (or any outer)
            # transaction actually commits.
            from . import email_tasks
            transaction.on_commit(lambda delivery_id=delivery.id: email_tasks.enqueue_email_delivery_task(delivery_id))

    return notification


def dispatch_due_notifications(now=None):
    """Scheduling abstraction (§14/§29): finds every Notification still in
    STATUS_SCHEDULED whose scheduled_for has arrived, delivers its
    still-queued in-app deliveries, and flips it to STATUS_DISPATCHED.

    Meant to be called from a scheduler-driven sweep (the exact
    `Scheduler -> find due schedules -> queue tasks -> workers` shape §29
    describes) — DispatchScheduledNotificationsView (views.py) is the
    corresponding cron endpoint, following the same `_check_cron_secret`
    shared-secret convention already used by
    billing.SendRenewalRemindersView / courses.PruneExpiredPackagesView /
    tests_app.FinalizeExpiredAttemptsView (see Phase 0 audit, section E).

    Idempotent the same way create_notification is: a Notification only
    ever transitions SCHEDULED -> DISPATCHED once (the queryset filter
    itself excludes anything already dispatched), so a duplicate sweep
    execution is safe.

    Phase 2 addition — delivery-time eligibility re-check (§31): between
    scheduling and delivery a subscription can expire, an enrollment can
    change, or access can be revoked. Every notification tied to a `test`
    (Grand/Daily Test reminders) is re-checked via
    eligibility.recheck_notification_eligibility() immediately before
    delivery; a now-ineligible notification is cancelled instead of
    delivered — never silently dropped without a record of why."""
    from .eligibility import recheck_notification_eligibility

    now = now or dj_timezone.now()
    due = Notification.objects.filter(status=Notification.STATUS_SCHEDULED, scheduled_for__lte=now)
    counts = {'checked': 0, 'dispatched': 0, 'cancelled_ineligible': 0}
    for notification in due:
        counts['checked'] += 1

        if not recheck_notification_eligibility(notification):
            notification.status = Notification.STATUS_CANCELLED
            notification.save(update_fields=['status'])
            notification.deliveries.filter(status=NotificationDelivery.STATUS_QUEUED).update(
                status=NotificationDelivery.STATUS_SKIPPED,
            )
            counts['cancelled_ineligible'] += 1
            continue

        for delivery in notification.deliveries.filter(status=NotificationDelivery.STATUS_QUEUED):
            if delivery.channel == CHANNEL_IN_APP and delivery.channel in IMPLEMENTED_CHANNELS:
                try:
                    _deliver_in_app(delivery)
                except Exception as exc:  # noqa: BLE001 — see create_notification's own note.
                    record_delivery_failure(delivery, error_code='unexpected_error', error_message=str(exc))
            elif delivery.channel == CHANNEL_PUSH:
                # Phase 3: a scheduled notification's push delivery row was
                # left QUEUED by create_notification() (which only enqueues
                # immediately for scheduled_for=None) — this sweep is where
                # a due, scheduled push reminder actually gets its Cloud
                # Tasks job enqueued, same on_commit discipline as there.
                from . import push_tasks
                transaction.on_commit(lambda delivery_id=delivery.id: push_tasks.enqueue_push_delivery_task(delivery_id))
            elif delivery.channel == CHANNEL_EMAIL:
                # Phase 4 — same reasoning as the push branch above, for a
                # scheduled notification's email delivery.
                from . import email_tasks
                transaction.on_commit(lambda delivery_id=delivery.id: email_tasks.enqueue_email_delivery_task(delivery_id))
        notification.status = Notification.STATUS_DISPATCHED
        notification.save(update_fields=['status'])
        counts['dispatched'] += 1
    return counts


def cancel_notifications_for_session(session):
    """Grand/Daily Test session cancellation (§9/§30): cancels every
    still-scheduled Notification tied to THIS SPECIFIC ExamSession, so a
    cancelled session never delivers a stale reminder.

    Scoped by `metadata__session_id`, not just `test`: a single Test/
    exam_version can legitimately have more than one ExamSession (a
    reschedule with new_version=False, the common case, reuses the same
    Test row for the new session) — filtering by test alone would risk
    cancelling a DIFFERENT, still-valid session's reminders. `session_id`
    lives in metadata rather than as a new FK on Notification — a Phase-1
    model change wasn't needed for this Phase 2-specific relationship (see
    exam_integration.py, which is the only writer of this metadata key).

    Called from ExamSessionViewSet.cancel() only. NOT called from the
    'Reschedule / Schedule Again' flow (exam_versioning.create_reschedule_
    session): real inspection of that function shows it always creates a
    brand-new, independent ExamSession alongside any prior one — the
    codebase deliberately allows a Test to carry multiple concurrent
    sessions (see the 'Session {n}' numbering), and that action never
    identifies which prior session it means to supersede. Auto-cancelling
    "the previous one" there would mean guessing, which the acceptance
    contract prohibits. See reschedule_notifications_for_session below for
    the one case that genuinely does replace this same session's own
    reminders: an in-place time edit."""
    pending = Notification.objects.filter(
        status=Notification.STATUS_SCHEDULED, metadata__session_id=session.id,
    )
    count = 0
    for notification in pending:
        if cancel_notification(notification):
            count += 1
    return count


def reschedule_notifications_for_session(session):
    """The real 'Edit Session' flow (Admin's EditSessionModal, a plain PATCH
    handled by ExamSessionViewSet.perform_update) can change an EXISTING
    session's start_datetime/end_datetime/registration_deadline directly —
    unlike 'Reschedule / Schedule Again', which always creates a new
    session (see cancel_notifications_for_session's docstring). Any
    reminder already scheduled for the OLD time is now wrong and must be
    replaced, not left to fire at a stale moment.

    HARD-deletes (not soft-cancels) this session's still-SCHEDULED
    Notification rows: cancel_notification()'s ordinary soft-cancel keeps
    the row (and its dedupe_key) around for audit history, but
    create_notification()'s idempotency check would then find that same
    dedupe_key on the next schedule_session_reminders() call and return
    the stale, still-cancelled row unchanged instead of creating a fresh
    one with the corrected time — silently leaving the student with no
    reminder at all for the new time. Deleting is safe here specifically
    because STATUS_SCHEDULED means it was never delivered to anyone yet —
    there is no delivered message to preserve a record of.

    Returns the number of stale rows removed. Caller (ExamSessionViewSet.
    perform_update) is responsible for calling
    exam_integration.schedule_session_reminders(session) again right
    after this, to (re)create the correct set for the new time — this
    function only ever clears the way, it never itself recreates
    anything, so a caller that forgets the second call gets silence, not
    a stale reminder (fail safe, not fail stale)."""
    pending = Notification.objects.filter(
        status=Notification.STATUS_SCHEDULED, metadata__session_id=session.id,
    )
    count = 0
    for notification in pending:
        notification.deliveries.all().delete()
        notification.delete()
        count += 1
    return count


def cancel_notification(notification):
    """Cancels one still-scheduled notification and its still-queued
    deliveries — the "Grand Test cancelled -> cancel all future
    notifications" flow (§30) a future Phase 2 exam-lifecycle hook will
    call. A no-op (returns False) once a notification has already been
    dispatched — cancellation only makes sense before delivery."""
    if notification.status != Notification.STATUS_SCHEDULED:
        return False
    notification.status = Notification.STATUS_CANCELLED
    notification.save(update_fields=['status'])
    notification.deliveries.filter(status=NotificationDelivery.STATUS_QUEUED).update(
        status=NotificationDelivery.STATUS_SKIPPED,
    )
    return True


def mark_read(notification):
    if notification.read_at is None:
        notification.read_at = dj_timezone.now()
        notification.save(update_fields=['read_at'])
    return notification


def mark_all_read(user, course=None):
    qs = Notification.objects.filter(user=user, read_at__isnull=True)
    if course is not None:
        qs = qs.filter(course=course)
    return qs.update(read_at=dj_timezone.now())


def mark_clicked(notification):
    """Marks the notification clicked (and, if not already, read — a click
    always implies a read) and advances its in-app delivery's own status to
    CLICKED, matching the funnel §37's analytics section expects
    (delivered -> opened -> clicked) to eventually track per channel."""
    now = dj_timezone.now()
    if notification.read_at is None:
        notification.read_at = now
    if notification.clicked_at is None:
        notification.clicked_at = now
    notification.save(update_fields=['read_at', 'clicked_at'])
    NotificationDelivery.objects.filter(
        notification=notification, channel=CHANNEL_IN_APP, status=NotificationDelivery.STATUS_DELIVERED,
    ).update(status=NotificationDelivery.STATUS_CLICKED, clicked_at=now)
    return notification
