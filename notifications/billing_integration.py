"""Billing domain-event integration.

Phase 2 built PAYMENT_APPROVED / SUBSCRIPTION_ACTIVATED, the two events its
acceptance contract named.

Phase 2b (email-verification-audit follow-up: "implement the full
subscription notification lifecycle, reusing this exact infrastructure")
adds the other real Purchase-lifecycle transitions this module was always
structured to grow into: PAYMENT_PENDING (submit), PAYMENT_REJECTED
(reject), PURCHASE_CANCELLED (cancel), and the renewal sweep's own
SUBSCRIPTION_EXPIRING/SUBSCRIPTION_EXPIRED days (billing/views.py's
SendRenewalRemindersView). No new business states anywhere — every
function below fires from a state transition billing/payment_service.py
(or the renewal sweep) already makes; see each function's own docstring
for its exact real-code trigger.

billing/payment_service.py's own functions already place their existing
`send_payment_notification(...)` / `send_notification(...)` calls (the
legacy email/SMS-only system, billing/notifications.py +
billing.NotificationLog) OUTSIDE/AFTER their `with transaction.atomic():`
block — this module's entry points are called from those exact same
places, immediately alongside them. Not a replacement: the legacy system
keeps running completely unchanged (its own dedup via NotificationLog is
untouched); this module is a second, additive path through the modern
Notification/NotificationDelivery pipeline (in-app + push + email,
preference-aware, and — since the email-verification feature — gated on
the recipient's email_verified flag for the EMAIL leg only).

`transaction.on_commit` is used anyway (belt-and-suspenders) in every
function below: the real caller is itself sometimes invoked from within an
outer atomic block (e.g. a bulk-approve admin action, or the renewal
sweep's own per-subscription loop), and on_commit is always correct
regardless of nesting depth, while a plain call after `with
transaction.atomic():` is only correct when that's the outermost block.
"""
from django.db import transaction

from . import services, templates


def _course_and_item_for_purchase(purchase):
    """Best-effort (course, item_label) for a Purchase, covering every
    real `Purchase.kind` (see billing/models.py: Purchase.KIND_CHOICES).
    Never guesses a course when a purchase genuinely spans more than one
    (combo/grand_test_package) or has none (teacher_course, a marketplace
    concept unrelated to courses.Course) — `course=None` in that case,
    matching create_notification's own `course=None` default rather than
    inventing a fake one."""
    if purchase.kind == 'subscription' and purchase.plan_id:
        return purchase.plan.course, purchase.plan.name
    if purchase.kind == 'grand_test' and purchase.grand_test_id:
        return None, purchase.grand_test.title
    if purchase.kind == 'combo' and purchase.combo_plan_id:
        return purchase.combo_plan.course, purchase.combo_plan.name
    if purchase.kind == 'grand_test_package' and purchase.grand_test_package_id:
        return None, purchase.grand_test_package.name
    if purchase.kind == 'teacher_course' and purchase.teacher_course_id:
        return None, getattr(purchase.teacher_course, 'title', 'your course')
    return None, 'your purchase'


def _subscription_expiry_suffix(purchase, course):
    """Best-effort ' until <isoformat date>' for a subscription/combo
    purchase, '' otherwise (grand_test/teacher_course/package purchases
    have no Subscription row at all; a combo spanning several courses has
    one Subscription per course and no single "the" expiry — deliberately
    left blank rather than guessing which one, same "never guess when
    genuinely ambiguous" rule _course_and_item_for_purchase documents for
    course/item above)."""
    if purchase.kind != 'subscription' or course is None:
        return ''
    from billing.models import Subscription

    qs = Subscription.objects.filter(user=purchase.user, course=course)
    if purchase.plan_id:
        qs = qs.filter(plan=purchase.plan_id)
    sub = qs.order_by('-created_at').first()
    if sub is None or sub.expires_at is None:
        return ''
    return f' until {sub.expires_at.isoformat()}'


def notify_payment_approved(purchase):
    """Called once, additively, from billing.payment_service.activate() —
    the single real "a purchase was approved" moment in this codebase.

    ONE MEANINGFUL EVENT -> ONE NOTIFICATION (product decision, subscription-
    notification-model follow-up): a course subscription/combo purchase
    (kind in ('subscription', 'combo') AND a real course was resolved —
    see _course_and_item_for_purchase) fires ONLY SUBSCRIPTION_ACTIVATED,
    never also the generic PAYMENT_APPROVED — the student does not need two
    notifications for the same successful activation.

    PAYMENT_APPROVED is still the right (and only) notification for every
    OTHER purchase kind, which has no "subscription activation" concept at
    all and would otherwise get silently un-notified: grand_test,
    teacher_course, grand_test_package, and a "build your own" combo whose
    combo_plan_id is null (no single course resolves, so course is None
    even though kind == 'combo' — see _course_and_item_for_purchase's own
    "never guess when genuinely ambiguous" rule). The condition below is
    deliberately IDENTICAL to the one that decides whether
    SUBSCRIPTION_ACTIVATED fires, so the two branches can never both fire
    and can never both stay silent for the same purchase.

    The underlying Purchase.status='approved' transition, billing records,
    and audit trail (PaymentAuditLog via record_payment_event) are
    completely unaffected — this function only ever decides which
    student-facing notification(s) to create, never anything about the
    actual payment/approval state itself."""
    course, item = _course_and_item_for_purchase(purchase)
    is_subscription_activation = purchase.kind in ('subscription', 'combo') and course is not None

    def _notify():
        if is_subscription_activation:
            expiry_suffix = _subscription_expiry_suffix(purchase, course)
            title, body = templates.render('SUBSCRIPTION_ACTIVATED', {'course': course.name, 'expiry_suffix': expiry_suffix})
            services.create_notification(
                purchase.user, 'SUBSCRIPTION_ACTIVATED', title, body,
                course=course, purchase=purchase, action_url='/billing/history',
                dedupe_key=f'purchase:{purchase.id}:subscription_activated',
            )
        else:
            title, body = templates.render('PAYMENT_APPROVED', {'item': item})
            services.create_notification(
                purchase.user, 'PAYMENT_APPROVED', title, body,
                course=course, purchase=purchase, action_url='/billing/history',
                dedupe_key=f'purchase:{purchase.id}:payment_approved',
            )

    transaction.on_commit(_notify)


def notify_payment_pending(purchase):
    """Additive, from billing.payment_service.submit() — "your proof of
    payment was received and is awaiting verification" (Purchase
    'unpaid'/'resubmission_requested' -> 'pending'). Deliberately NOT also
    firing PURCHASE_CREATED (a separate, unwired vocabulary entry for the
    earlier unpaid-cart-creation moment) — that step has no proof
    submitted yet and nothing new to tell the student that the checkout
    screen itself hasn't already shown; firing it too would be a near-
    duplicate of this notification moments later."""
    course, item = _course_and_item_for_purchase(purchase)

    def _notify():
        title, body = templates.render('PAYMENT_PENDING', {'item': item})
        services.create_notification(
            purchase.user, 'PAYMENT_PENDING', title, body,
            course=course, purchase=purchase, action_url='/billing/history',
            dedupe_key=f'purchase:{purchase.id}:payment_pending',
        )

    transaction.on_commit(_notify)


def notify_payment_rejected(purchase, reason):
    """Additive, from billing.payment_service.reject() (Purchase 'pending'/
    'resubmission_requested' -> 'rejected'). `reason` is the admin-written
    rejection reason already shown to this same student by the legacy
    send_payment_notification() call right next to this one — safe to
    reuse verbatim, not a new information disclosure."""
    course, item = _course_and_item_for_purchase(purchase)

    def _notify():
        title, body = templates.render('PAYMENT_REJECTED', {'item': item, 'reason': reason})
        services.create_notification(
            purchase.user, 'PAYMENT_REJECTED', title, body,
            course=course, purchase=purchase, action_url='/billing/history',
            dedupe_key=f'purchase:{purchase.id}:payment_rejected',
        )

    transaction.on_commit(_notify)


def notify_purchase_cancelled(purchase):
    """Additive, from billing.payment_service.cancel() (Purchase 'unpaid'/
    'resubmission_requested' -> 'cancelled' — a final, terminal state;
    cancel() only ever runs once per purchase, so no separate "cancellation
    requested but not yet confirmed" step exists to avoid double-notifying
    here)."""
    course, item = _course_and_item_for_purchase(purchase)

    def _notify():
        title, body = templates.render('PURCHASE_CANCELLED', {'item': item})
        services.create_notification(
            purchase.user, 'PURCHASE_CANCELLED', title, body,
            course=course, purchase=purchase, action_url='/billing/history',
            dedupe_key=f'purchase:{purchase.id}:purchase_cancelled',
        )

    transaction.on_commit(_notify)


def notify_subscription_lifecycle(subscription, notification_type):
    """Additive, from billing/views.py's SendRenewalRemindersView — the
    ALREADY-EXISTING 30/15/7/3/1-days-before-expiry + on-expiry cron sweep
    (billing.REMINDER_SCHEDULE), which already dedupes its own legacy
    NotificationLog sends per (subscription, notification_type). Reuses
    the identical sweep and the identical days-before-expiry business rule
    — this function does not decide timing, it only decides what to tell
    the modern pipeline once the sweep has already decided "today is one
    of this subscription's reminder/expiry days."

    `notification_type` is one of REMINDER_SCHEDULE's own keys
    ('reminder_30'..'reminder_1' or 'expiry') — 'grace_period' is
    deliberately not mapped to a modern event (no matching vocabulary
    entry was requested; the legacy email channel still covers it
    unchanged). Maps: 'expiry' -> SUBSCRIPTION_EXPIRED, any reminder_N ->
    SUBSCRIPTION_EXPIRING — dedupe_key includes notification_type so each
    of the 5 reminder days plus the expiry day is its own idempotent
    event, exactly mirroring NotificationLog's own per-type dedupe."""
    if notification_type == 'expiry':
        event_type = 'SUBSCRIPTION_EXPIRED'
    elif notification_type.startswith('reminder_'):
        event_type = 'SUBSCRIPTION_EXPIRING'
    else:
        return  # 'grace_period' — not mapped, see docstring.

    course = subscription.course
    expiry = subscription.expires_at.isoformat() if subscription.expires_at else ''

    def _notify():
        title, body = templates.render(event_type, {'course': course.name, 'expiry': expiry})
        services.create_notification(
            subscription.user, event_type, title, body,
            course=course, action_url='/billing/history',
            dedupe_key=f'subscription:{subscription.id}:{notification_type}',
        )

    transaction.on_commit(_notify)
