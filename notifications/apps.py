from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'notifications'
    verbose_name = 'Notifications (centralized, course-aware notification engine)'

    def ready(self):
        # Phase 2 — System Announcement integration. Imported here (not
        # at module scope) so it registers exactly once, at app-loading
        # time, matching Django's own documented pattern. signals.py uses
        # sender='core.Announcement' (a string) rather than importing the
        # Announcement model directly, so this app has no import-time
        # dependency on core's own app-loading order (INSTALLED_APPS does
        # not currently list 'notifications' after 'core' by any
        # guarantee this should rely on).
        from . import signals  # noqa: F401
