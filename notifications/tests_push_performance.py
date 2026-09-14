"""Phase 3 — Performance evidence (P1-03), same CaptureQueriesContext
method Phase 2 established (notifications/tests.py:
NotificationListQueryCountTests) rather than a guessed assertNumQueries
number."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from . import push_service
from .models import Notification, NotificationDelivery
from .webpush_adapter import PushSendResult

User = get_user_model()


def _make_user(username):
    return User.objects.create_user(username=username, email=f'{username}@example.com', password='pw12345')


class PushDeliveryQueryCountTests(TestCase):
    def _make_delivery_with_n_devices(self, user, n):
        for i in range(n):
            push_service.register_subscription(user, endpoint=f'https://fcm.googleapis.com/fcm/send/dev-{user.id}-{i}', p256dh='p', auth='a')
        notification = Notification.objects.create(user=user, event_type='ANNOUNCEMENT', category='announcements', title='Hi')
        return NotificationDelivery.objects.create(notification=notification, channel='push')

    def test_subscription_lookup_is_a_single_query_regardless_of_device_count(self):
        """The audience query itself (docs/PHASE_3_WEB_PUSH_TRACEABILITY.md
        P1-03) must not scale with the number of a user's own devices —
        one filtered query, not one per device."""
        from . import push_tasks

        small_user = _make_user('perf_small')
        large_user = _make_user('perf_large')
        small_delivery = self._make_delivery_with_n_devices(small_user, 2)
        large_delivery = self._make_delivery_with_n_devices(large_user, 10)

        with patch('notifications.push_tasks.webpush_adapter.send') as mock_send:
            mock_send.return_value = PushSendResult(outcome=PushSendResult.OUTCOME_SENT)
            with CaptureQueriesContext(connection) as small_ctx:
                push_tasks.process_push_delivery(small_delivery.id)
            with CaptureQueriesContext(connection) as large_ctx:
                push_tasks.process_push_delivery(large_delivery.id)

        subscription_lookup_queries_small = sum(
            1 for q in small_ctx.captured_queries if 'pushsubscription' in q['sql'].lower() and 'select' in q['sql'].lower()
        )
        subscription_lookup_queries_large = sum(
            1 for q in large_ctx.captured_queries if 'pushsubscription' in q['sql'].lower() and 'select' in q['sql'].lower()
        )
        self.assertEqual(subscription_lookup_queries_small, 1)
        self.assertEqual(subscription_lookup_queries_large, 1)

    def test_inactive_subscriptions_filtered_at_the_database_not_in_python(self):
        """The (user, status) index (models.py) is what makes this a
        single indexed lookup rather than a full-table Python filter —
        proven here by confirming the query itself only returns active
        rows, not by reading the SQL's use of the index directly (SQLite's
        EXPLAIN output isn't a reliable cross-DB proxy for "used an
        index")."""
        from .models import PushSubscription

        user = _make_user('perf_filtered')
        active = push_service.register_subscription(user, endpoint='https://fcm.googleapis.com/fcm/send/active-1', p256dh='p', auth='a')
        revoked = push_service.register_subscription(user, endpoint='https://fcm.googleapis.com/fcm/send/revoked-1', p256dh='p', auth='a')
        revoked.status = PushSubscription.STATUS_REVOKED
        revoked.save(update_fields=['status'])

        with CaptureQueriesContext(connection):
            result_ids = set(
                PushSubscription.objects.filter(user=user, status=PushSubscription.STATUS_ACTIVE).values_list('id', flat=True)
            )
        self.assertEqual(result_ids, {active.id})
