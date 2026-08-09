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
