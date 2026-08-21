"""Notification channel abstraction for renewal reminders and confirmations.

Email is the one channel that actually delivers (via Django's send_mail, the
same pattern already used for grand-test-access emails in views.py) — it
starts working for real the moment SMTP env vars are set, exactly like every
other email in this project. SMS/WhatsApp/Push have no third-party account
configured anywhere in this project, so their `send_*` functions are stubs:
every attempt is logged as 'skipped' with a clear reason rather than silently
pretending to have sent something. Wiring a real provider later means filling
in one function body — nothing about the calling code needs to change.
"""
from django.conf import settings
from django.core.mail import send_mail

from .models import NotificationLog

MESSAGES = {
    'reminder_30': ('Your {product} access expires in 30 days', 'Your {product} access for {course} expires on {expiry}. Renew anytime before then to keep uninterrupted access.'),
    'reminder_15': ('Your {product} access expires in 15 days', 'Your {product} access for {course} expires on {expiry} — just 15 days away.'),
    'reminder_7': ('Your {product} access expires in 7 days', 'Your {product} access for {course} expires on {expiry} — one week left.'),
    'reminder_3': ('Your {product} access expires in 3 days', 'Your {product} access for {course} expires on {expiry} — only 3 days left.'),
    'reminder_1': ('Your {product} access expires tomorrow', 'Your {product} access for {course} expires tomorrow ({expiry}). Renew today to avoid interruption.'),
    'expiry': ('Your {product} access has expired', 'Your {product} access for {course} expired on {expiry}. Renew anytime to restore access.'),
    'grace_period': ('Renew now to avoid losing your progress', 'Your {product} access for {course} expired on {expiry}. You are in a short grace period — renew now to avoid losing access.'),
    'renewal_confirmation': ('Your {product} access has been renewed', 'Thanks! Your {product} access for {course} has been renewed and now runs until {expiry}.'),
}


def _render(notification_type, subscription):
    subject_tpl, body_tpl = MESSAGES[notification_type]
    ctx = {
        'product': subscription.get_product_type_display(),
        'course': subscription.course.name,
        'expiry': subscription.expires_at.strftime('%d %b %Y') if subscription.expires_at else 'N/A',
    }
    return subject_tpl.format(**ctx), body_tpl.format(**ctx)


def _send_email(user, subject, body):
    if not user.email:
        return 'skipped', 'user has no email on file'
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=False)
        return 'sent', ''
    except Exception as exc:  # noqa: BLE001 — log and move on, never break the reminder loop over one bad send
        return 'failed', str(exc)[:500]


def _send_sms(user, subject, body):
    return 'skipped', 'SMS channel not configured — no provider account set up yet'


def _send_whatsapp(user, subject, body):
    return 'skipped', 'WhatsApp channel not configured — no Business API account set up yet'


def _send_push(user, subject, body):
    return 'skipped', 'Push channel not configured — no push token/provider set up yet'


CHANNEL_SENDERS = {
    'email': _send_email,
    'sms': _send_sms,
    'whatsapp': _send_whatsapp,
    'push': _send_push,
}


def send_notification(user, notification_type, subscription, channels=('email',)):
    """Sends (or logs a skip) on every requested channel, one NotificationLog
    row per channel. Returns the list of created NotificationLog rows."""
    subject, body = _render(notification_type, subscription)
    logs = []
    for channel in channels:
        sender = CHANNEL_SENDERS.get(channel)
        if not sender:
            continue
        status, detail = sender(user, subject, body)
        logs.append(
            NotificationLog.objects.create(
                user=user, subscription=subscription, channel=channel,
                notification_type=notification_type, status=status, detail=detail,
            )
        )
    return logs


# --- Payment (Purchase) notifications ---
# A separate render path from the reminder MESSAGES above: those are always
# Subscription-shaped (course/expiry), but a payment event can be for a
# subscription, a Grand Test, or a Teacher Course purchase, and needs to
# reflect the actual admin_note on rejection. Kept as plain string templates
# (not the MESSAGES dict) since the context shape is different enough that
# forcing them into the same _render() would need more branching than it's
# worth.

PAYMENT_MESSAGES = {
    'payment_submitted': (
        'We received your payment proof — {item}',
        'Thanks! We received your payment proof for {item} (Order {order_id}, Rs. {amount}). '
        "We'll verify it and activate your access shortly.",
    ),
    'payment_approved': (
        'Payment verified — {item} is now active',
        'Your payment has been verified successfully. Your purchased product is now available. '
        '{item} (Order {order_id}, Rs. {amount}) is now active on your account.',
    ),
    'payment_rejected': (
        'Payment could not be verified — {item}',
        'Your payment could not be verified. Reason: {reason}. '
        'Order {order_id} for {item} — you can submit a new payment reference and screenshot anytime.',
    ),
    'payment_expired': (
        'Your payment window has expired — {item}',
        'Your 30-minute payment window for {item} (Order {order_id}, Rs. {amount}) has expired '
        'without a submitted payment. Start a new order anytime to try again.',
    ),
}


def _purchase_item_label(purchase):
    if purchase.kind == 'grand_test':
        return purchase.grand_test.title if purchase.grand_test_id else 'your Grand Test'
    if purchase.kind == 'teacher_course':
        return purchase.teacher_course.title if purchase.teacher_course_id else 'your course'
    return purchase.plan.name if purchase.plan_id else 'your subscription'


def _render_payment(notification_type, purchase):
    subject_tpl, body_tpl = PAYMENT_MESSAGES[notification_type]
    ctx = {
        'item': _purchase_item_label(purchase),
        'order_id': purchase.order_id,
        'amount': purchase.final_amount,
        'reason': purchase.admin_note or 'Not specified.',
    }
    return subject_tpl.format(**ctx), body_tpl.format(**ctx)


def send_payment_notification(user, notification_type, purchase, channels=('email',)):
    """Same channel/logging shape as send_notification, for Purchase-shaped
    events (submitted/approved/rejected/expired) instead of Subscription-
    shaped reminders."""
    subject, body = _render_payment(notification_type, purchase)
    logs = []
    for channel in channels:
        sender = CHANNEL_SENDERS.get(channel)
        if not sender:
            continue
        status, detail = sender(user, subject, body)
        logs.append(
            NotificationLog.objects.create(
                user=user, purchase=purchase, channel=channel,
                notification_type=notification_type, status=status, detail=detail,
            )
        )
    return logs
