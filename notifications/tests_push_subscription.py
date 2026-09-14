"""Phase 3 — Push subscription lifecycle tests (P0-01, P0-02, P0-03, P0-05).

Drives the real API endpoints, not push_service functions directly, per
this project's own established convention (Phase 2's own test files did
the same for exam/billing integration)."""
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from .models import PushSubscription

User = get_user_model()

CHROME_MAC_UA = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
)
FIREFOX_UA = 'Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0'


def _make_user(username='push_student'):
    return User.objects.create_user(username=username, email=f'{username}@example.com', password='pw12345')


def _subscribe(client, endpoint='https://fcm.googleapis.com/fcm/send/abc123', ua=CHROME_MAC_UA, label=''):
    return client.post(
        reverse('push-subscribe'),
        {'endpoint': endpoint, 'keys': {'p256dh': 'p256dh-key', 'auth': 'auth-key'}, 'device_label': label},
        format='json', HTTP_USER_AGENT=ua,
    )


class PushSubscribeTests(APITestCase):
    def setUp(self):
        self.user = _make_user()
        self.client.force_authenticate(self.user)

    def test_register_creates_active_subscription(self):
        response = _subscribe(self.client)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        subscription = PushSubscription.objects.get(user=self.user)
        self.assertEqual(subscription.status, PushSubscription.STATUS_ACTIVE)
        self.assertEqual(subscription.endpoint, 'https://fcm.googleapis.com/fcm/send/abc123')
        self.assertEqual(subscription.p256dh, 'p256dh-key')
        self.assertEqual(subscription.auth, 'auth-key')
        self.assertIsNotNone(subscription.last_seen_at)
        # Parsed server-side from the real User-Agent header, never trusted from the client.
        self.assertEqual(subscription.browser, 'Chrome')
        self.assertEqual(subscription.os, 'macOS')

    def test_unauthenticated_registration_rejected(self):
        self.client.force_authenticate(None)
        response = _subscribe(self.client)
        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
        self.assertFalse(PushSubscription.objects.exists())

    def test_missing_keys_rejected(self):
        response = self.client.post(
            reverse('push-subscribe'), {'endpoint': 'https://fcm.googleapis.com/fcm/send/xyz', 'keys': {'p256dh': 'only-one'}},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(PushSubscription.objects.exists())

    def test_duplicate_register_reuses_existing_row(self):
        _subscribe(self.client)
        _subscribe(self.client)
        _subscribe(self.client)
        self.assertEqual(PushSubscription.objects.filter(user=self.user).count(), 1)

    def test_different_endpoints_create_separate_rows(self):
        _subscribe(self.client, endpoint='https://fcm.googleapis.com/fcm/send/device-a', ua=CHROME_MAC_UA)
        _subscribe(self.client, endpoint='https://fcm.googleapis.com/fcm/send/device-b', ua=FIREFOX_UA)
        self.assertEqual(PushSubscription.objects.filter(user=self.user).count(), 2)

    def test_reregistering_invalid_subscription_reactivates_it(self):
        _subscribe(self.client)
        sub = PushSubscription.objects.get(user=self.user)
        sub.status = PushSubscription.STATUS_INVALID
        sub.save(update_fields=['status'])

        _subscribe(self.client)
        sub.refresh_from_db()
        self.assertEqual(sub.status, PushSubscription.STATUS_ACTIVE)


class PushUnsubscribeTests(APITestCase):
    def setUp(self):
        self.user = _make_user()
        self.client.force_authenticate(self.user)
        _subscribe(self.client)
        self.subscription = PushSubscription.objects.get(user=self.user)

    def test_unsubscribe_revokes_own_subscription(self):
        response = self.client.post(reverse('push-unsubscribe'), {'endpoint': self.subscription.endpoint}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['revoked'])
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, PushSubscription.STATUS_REVOKED)

    def test_unsubscribe_missing_endpoint_rejected(self):
        response = self.client.post(reverse('push-unsubscribe'), {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unsubscribe_unknown_endpoint_reports_not_revoked(self):
        response = self.client.post(reverse('push-unsubscribe'), {'endpoint': 'https://nope.example/x'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['revoked'])


class PushDevicesListTests(APITestCase):
    def setUp(self):
        self.user = _make_user()
        self.client.force_authenticate(self.user)

    def test_lists_own_devices_without_credentials(self):
        _subscribe(self.client, endpoint='https://fcm.googleapis.com/fcm/send/a', ua=CHROME_MAC_UA, label='My MacBook')
        _subscribe(self.client, endpoint='https://fcm.googleapis.com/fcm/send/b', ua=FIREFOX_UA)

        response = self.client.get(reverse('push-devices'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        for row in response.data:
            self.assertNotIn('endpoint', row)
            self.assertNotIn('p256dh', row)
            self.assertNotIn('auth', row)
        labels = {row['device_label'] for row in response.data}
        self.assertIn('My MacBook', labels)


class VapidPublicKeyViewTests(APITestCase):
    def test_returns_public_key_unauthenticated(self):
        response = self.client.get(reverse('push-vapid-public-key'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['public_key'])

    def test_response_never_contains_private_key_field(self):
        from django.conf import settings

        response = self.client.get(reverse('push-vapid-public-key'))
        body = str(response.data)
        self.assertNotIn(settings.VAPID_PRIVATE_KEY, body)
        self.assertNotIn('private', body.lower())
