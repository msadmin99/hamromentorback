from django.urls import path

from .views import (
    DispatchScheduledNotificationsView,
    AdminEmailTestSendView,
    EmailProcessDeliveryView,
    MarkAllReadView,
    MarkNotificationReadView,
    MyNotificationsView,
    MyPreferencesView,
    NotificationClickView,
    PushDevicesView,
    PushProcessDeliveryView,
    PushSubscribeView,
    PushUnsubscribeView,
    UnreadCountView,
    VapidPublicKeyView,
)

urlpatterns = [
    path('notifications/mine/', MyNotificationsView.as_view(), name='notifications-mine'),
    path('notifications/unread-count/', UnreadCountView.as_view(), name='notifications-unread-count'),
    path('notifications/mark-all-read/', MarkAllReadView.as_view(), name='notifications-mark-all-read'),
    path('notifications/<int:pk>/read/', MarkNotificationReadView.as_view(), name='notifications-read'),
    path('notifications/<int:pk>/click/', NotificationClickView.as_view(), name='notifications-click'),
    path('notifications/preferences/', MyPreferencesView.as_view(), name='notifications-preferences'),
    path(
        'cron/dispatch-scheduled-notifications/', DispatchScheduledNotificationsView.as_view(),
        name='cron-dispatch-scheduled-notifications',
    ),
    # Phase 3 — Web Push
    path('notifications/push/vapid-public-key/', VapidPublicKeyView.as_view(), name='push-vapid-public-key'),
    path('notifications/push/subscribe/', PushSubscribeView.as_view(), name='push-subscribe'),
    path('notifications/push/unsubscribe/', PushUnsubscribeView.as_view(), name='push-unsubscribe'),
    path('notifications/push/devices/', PushDevicesView.as_view(), name='push-devices'),
    path('notifications/push/process/', PushProcessDeliveryView.as_view(), name='push-process-delivery'),
    # Phase 4 — Email
    path('notifications/email/test-send/', AdminEmailTestSendView.as_view(), name='email-test-send'),
    path('notifications/email/process/', EmailProcessDeliveryView.as_view(), name='email-process-delivery'),
]
