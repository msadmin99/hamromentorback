"""Phase 3 — Security tests (P0-04, P0-05). Mandatory cross-user/forged-
input coverage per the Phase 3 brief §22."""
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from . import push_service
from .models import PushSubscription

User = get_user_model()


def _make_user(username):
    return User.objects.create_user(username=username, email=f'{username}@example.com', password='pw12345')


class CrossUserSecurityTests(APITestCase):
    def setUp(self):
        self.user_a = _make_user('push_user_a')
        self.user_b = _make_user('push_user_b')
        self.subscription_a = push_service.register_subscription(
            self.user_a, endpoint='https://fcm.googleapis.com/fcm/send/a-device', p256dh='p', auth='a',
        )

    def test_cannot_revoke_another_users_subscription(self):
        self.client.force_authenticate(self.user_b)
        response = self.client.post(reverse('push-unsubscribe'), {'endpoint': self.subscription_a.endpoint}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['revoked'])  # reports "nothing happened", never confirms existence
        self.subscription_a.refresh_from_db()
        self.assertEqual(self.subscription_a.status, PushSubscription.STATUS_ACTIVE)

    def test_cannot_list_another_users_subscriptions(self):
        self.client.force_authenticate(self.user_b)
        response = self.client.get(reverse('push-devices'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_forged_user_id_in_registration_body_is_ignored(self):
        """The registration serializer doesn't even accept a `user`/`user_id`
        field, but prove the real end-to-end behavior anyway: whatever is
        sent, ownership always resolves to request.user."""
        self.client.force_authenticate(self.user_b)
        response = self.client.post(
            reverse('push-subscribe'),
            {
                'endpoint': 'https://fcm.googleapis.com/fcm/send/forged-device',
                'keys': {'p256dh': 'p', 'auth': 'a'},
                'user': self.user_a.id, 'user_id': self.user_a.id,
            },
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        created = PushSubscription.objects.get(endpoint='https://fcm.googleapis.com/fcm/send/forged-device')
        self.assertEqual(created.user_id, self.user_b.id)

    def test_unauthenticated_devices_list_rejected(self):
        response = self.client.get(reverse('push-devices'))
        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_unauthenticated_unsubscribe_rejected(self):
        response = self.client.post(reverse('push-unsubscribe'), {'endpoint': self.subscription_a.endpoint}, format='json')
        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
        self.subscription_a.refresh_from_db()
        self.assertEqual(self.subscription_a.status, PushSubscription.STATUS_ACTIVE)

    def test_malicious_endpoint_value_rejected_by_url_validation(self):
        self.client.force_authenticate(self.user_a)
        response = self.client.post(
            reverse('push-subscribe'),
            {'endpoint': 'javascript:alert(1)', 'keys': {'p256dh': 'p', 'auth': 'a'}},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class AccountSwitchingSecurityTests(APITestCase):
    """P0-05 — the same browser (same push endpoint) must never remain
    incorrectly associated with the wrong user once a different user
    authenticates and re-registers it."""

    def setUp(self):
        self.user_a = _make_user('switch_user_a')
        self.user_b = _make_user('switch_user_b')
        self.shared_endpoint = 'https://fcm.googleapis.com/fcm/send/shared-browser'

    def test_reregistering_same_endpoint_as_different_user_reassigns_ownership(self):
        self.client.force_authenticate(self.user_a)
        self.client.post(
            reverse('push-subscribe'), {'endpoint': self.shared_endpoint, 'keys': {'p256dh': 'p1', 'auth': 'a1'}},
            format='json',
        )
        self.assertEqual(PushSubscription.objects.get(endpoint=self.shared_endpoint).user_id, self.user_a.id)

        # User A logs out; User B logs in on the same browser and enables notifications.
        self.client.force_authenticate(self.user_b)
        self.client.post(
            reverse('push-subscribe'), {'endpoint': self.shared_endpoint, 'keys': {'p256dh': 'p2', 'auth': 'a2'}},
            format='json',
        )

        self.assertEqual(PushSubscription.objects.filter(endpoint=self.shared_endpoint).count(), 1)
        subscription = PushSubscription.objects.get(endpoint=self.shared_endpoint)
        self.assertEqual(subscription.user_id, self.user_b.id)
        self.assertEqual(subscription.p256dh, 'p2')  # keys refreshed to B's real subscription too

    def test_old_owner_no_longer_receives_after_reassignment(self):
        from . import services

        self.client.force_authenticate(self.user_a)
        self.client.post(
            reverse('push-subscribe'), {'endpoint': self.shared_endpoint, 'keys': {'p256dh': 'p1', 'auth': 'a1'}},
            format='json',
        )
        self.client.force_authenticate(self.user_b)
        self.client.post(
            reverse('push-subscribe'), {'endpoint': self.shared_endpoint, 'keys': {'p256dh': 'p2', 'auth': 'a2'}},
            format='json',
        )

        # A notification for A now resolves zero active subscriptions for A.
        notification = services.create_notification(self.user_a, 'ANNOUNCEMENT', 'For A only')
        from .models import NotificationDelivery
        self.assertFalse(
            NotificationDelivery.objects.filter(notification=notification, channel='push').exists()
        )
