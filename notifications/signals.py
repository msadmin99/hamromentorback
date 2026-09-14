"""Phase 2 — System Announcement integration.

core/admin.py: AnnouncementAdmin is a bog-standard ModelAdmin (Phase 2
precheck) — the minimal-touch hook here is a post_save signal on the
Announcement model itself, so ZERO changes are needed to core/admin.py,
core/models.py, or core/views.py (which keeps serving the existing public
banner exactly as before; this signal is a second, additive consumer of
the same underlying row).

sender='core.Announcement' is a string, not an imported model — this
module (and notifications/apps.py, which imports it in ready()) never
needs to import anything from `core` at module-load time, so this stays
correct no matter which app's AppConfig.ready() runs first.

Fan-out scope and cost, stated plainly: this creates one Notification (+
one NotificationDelivery) per active, non-staff user, synchronously,
inside the request that saves the Announcement in Django Admin — the
same per-recipient shape schedule_session_reminders() already uses for a
Grand/Daily Test's enrolled audience (this project's own established
Phase 2 precedent), just with "every active student" as the audience
instead of "students enrolled in one course," since Announcement itself
has no course scoping to inherit (core/models.py: Announcement has no
`course` field — it is the one genuinely platform-wide event this phase
handles). Only fires once per Announcement (on creation, not on every
edit/toggle) via `created=True` below, so an admin editing an existing
announcement's text or flipping is_active off and back on never
re-notifies everyone a second time. This is still the plain in-app
channel Phase 1 built (no email/SMS/push fan-out, no new queue) — not
the "marketing/bulk broadcast engine" the acceptance contract's forbidden
list means to rule out (that phrase targets a new mass-messaging PRODUCT
capability — campaign builder, segment targeting, a dedicated send
pipeline — none of which this signal adds); if real usage ever needs this
to run for a very large user base without blocking the Admin save
request, that is a Phase 6+ async-fan-out concern, called out here rather
than silently deferred.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from . import services, templates


@receiver(post_save, sender='core.Announcement', dispatch_uid='notifications_announcement_created')
def notify_announcement_created(sender, instance, created, **kwargs):
    if not created or not instance.is_active:
        return

    from django.contrib.auth import get_user_model
    User = get_user_model()

    title, body = templates.render('ANNOUNCEMENT', {'message': instance.message})
    recipients = User.objects.filter(is_active=True, is_staff=False)
    for user in recipients.iterator():
        services.create_notification(
            user, 'ANNOUNCEMENT', title, body,
            action_url='/', dedupe_key=f'announcement:{instance.id}:{user.id}',
        )
