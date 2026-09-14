from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from billing.models import GrandTestAccess
from courses.models import Course
from tests_app.models import Test

from . import eligibility, services
from .events import CATEGORY_BILLING, CATEGORY_SECURITY, CATEGORY_TESTS, CHANNEL_EMAIL, CHANNEL_IN_APP, category_for_event
from .models import Notification, NotificationDelivery, NotificationPreference

User = get_user_model()


def _make_user(username='student1'):
    return User.objects.create_user(username=username, email=f'{username}@example.com', password='pw12345')


def _make_course(name='CEE-MBBS'):
    return Course.objects.create(name=name, prefix=name[:10])


def _make_test(user, exam_type='grand', title='Grand Test I'):
    # created_by=user makes tests_app.access.can_access_test's very first
    # branch ("staff or the test's own creator can always see it") apply,
    # sidestepping the unrelated course/batch/draft-assignment machinery
    # entirely — this test is about eligibility.py correctly calling BOTH
    # get_grand_test_access AND can_view_test, not about re-testing
    # can_access_test's own course-scoping rules (already covered by
    # tests_app's own test suite).
    return Test.objects.create(title=title, exam_type=exam_type, created_by=user)


# =====================================================================
# events.py — vocabulary integrity
# =====================================================================

class EventVocabularyTests(APITestCase):
    def test_category_for_event_is_correct_for_known_events(self):
        self.assertEqual(category_for_event('GRAND_TEST_REMINDER'), CATEGORY_TESTS)
        self.assertEqual(category_for_event('LOGIN_SECURITY_ALERT'), CATEGORY_SECURITY)
        self.assertEqual(category_for_event('SUBSCRIPTION_EXPIRING'), CATEGORY_BILLING)

    def test_unregistered_event_type_fails_loudly(self):
        """A typo'd/unknown event_type must never silently create a
        Notification with a blank category — that would escape mandatory-
        category and preference filtering entirely."""
        with self.assertRaises(KeyError):
            category_for_event('NOT_A_REAL_EVENT')


# =====================================================================
# services.create_notification — the single write path
# =====================================================================

class CreateNotificationTests(APITestCase):
    def setUp(self):
        self.user = _make_user()
        self.course = _make_course()

    def test_creates_notification_and_one_delivery_per_channel(self):
        notification = services.create_notification(
            self.user, 'ANNOUNCEMENT', 'Maintenance tonight', 'The site will be briefly unavailable.',
            channels=(CHANNEL_IN_APP,),
        )
        self.assertEqual(Notification.objects.count(), 1)
        deliveries = list(notification.deliveries.all())
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].channel, CHANNEL_IN_APP)

    def test_in_app_delivery_is_immediately_delivered(self):
        """In-app has no external provider — the delivery row IS the
        record Phase 2's frontend reads, so it must already be in a
        terminal DELIVERED state right after creation, not left QUEUED
        forever waiting for a send step that doesn't exist for this
        channel."""
        notification = services.create_notification(self.user, 'ANNOUNCEMENT', 'Hi', channels=(CHANNEL_IN_APP,))
        delivery = notification.deliveries.get(channel=CHANNEL_IN_APP)
        self.assertEqual(delivery.status, NotificationDelivery.STATUS_DELIVERED)
        self.assertIsNotNone(delivery.sent_at)
        self.assertIsNotNone(delivery.delivered_at)

    def test_unimplemented_channel_creates_a_queued_row_but_attempts_nothing(self):
        """Per the governing prompt's phase boundary: Phase 1 must not
        build email/SMS/WhatsApp/push provider integration. Requesting a
        non-in-app channel must still produce a real delivery row (so a
        later phase never needs a schema change) but must NOT be marked
        sent/delivered — nothing actually attempted delivery."""
        notification = services.create_notification(
            self.user, 'ANNOUNCEMENT', 'Hi', channels=(CHANNEL_EMAIL,),
        )
        delivery = notification.deliveries.get(channel=CHANNEL_EMAIL)
        self.assertEqual(delivery.status, NotificationDelivery.STATUS_QUEUED)
        self.assertEqual(delivery.attempt_count, 0)

    def test_global_notification_has_no_course(self):
        notification = services.create_notification(self.user, 'ANNOUNCEMENT', 'Site-wide message')
        self.assertIsNone(notification.course)

    def test_course_scoped_notification_stores_the_course(self):
        notification = services.create_notification(
            self.user, 'GRAND_TEST_REMINDER', 'Reminder', course=self.course,
        )
        self.assertEqual(notification.course_id, self.course.id)

    def test_dedupe_key_prevents_duplicate_creation(self):
        """Architecture prompt §11: 'A duplicate scheduler execution must
        not create duplicate notifications.' Simulates exactly that: the
        same logical event fired twice with the same dedupe_key."""
        key = 'grand_test:82:user:123:tminus60'
        first = services.create_notification(self.user, 'GRAND_TEST_REMINDER', 'Reminder', dedupe_key=key)
        second = services.create_notification(self.user, 'GRAND_TEST_REMINDER', 'Reminder', dedupe_key=key)

        self.assertEqual(first.id, second.id)
        self.assertEqual(Notification.objects.filter(dedupe_key=key).count(), 1)
        # in_app + email (Phase 4 — this test user has no active push
        # subscription, so push is not a candidate channel here; see
        # notifications/services.py: _resolve_channels).
        self.assertEqual(NotificationDelivery.objects.filter(notification=first).count(), 2)

    def test_notifications_without_a_dedupe_key_are_never_deduplicated_against_each_other(self):
        first = services.create_notification(self.user, 'ANNOUNCEMENT', 'Hi')
        second = services.create_notification(self.user, 'ANNOUNCEMENT', 'Hi')
        self.assertNotEqual(first.id, second.id)

    def test_scheduled_notification_does_not_deliver_immediately(self):
        future = timezone.now() + timedelta(hours=1)
        notification = services.create_notification(
            self.user, 'GRAND_TEST_REMINDER', 'Reminder', scheduled_for=future,
        )
        self.assertEqual(notification.status, Notification.STATUS_SCHEDULED)
        delivery = notification.deliveries.get(channel=CHANNEL_IN_APP)
        self.assertEqual(delivery.status, NotificationDelivery.STATUS_QUEUED)


# =====================================================================
# Preferences (§27) — including the mandatory-category rule
# =====================================================================

class PreferenceResolutionTests(APITestCase):
    def setUp(self):
        self.user = _make_user()
        self.course = _make_course()

    def test_default_with_no_stored_preference_is_enabled(self):
        self.assertTrue(services.is_channel_enabled(self.user, CATEGORY_TESTS, CHANNEL_IN_APP))

    def test_platform_wide_disable_is_respected(self):
        NotificationPreference.objects.create(
            user=self.user, category=CATEGORY_TESTS, channel=CHANNEL_IN_APP, course=None, enabled=False,
        )
        self.assertFalse(services.is_channel_enabled(self.user, CATEGORY_TESTS, CHANNEL_IN_APP))

    def test_course_specific_row_overrides_platform_wide_row(self):
        other_course = _make_course('BDS')
        NotificationPreference.objects.create(
            user=self.user, category=CATEGORY_TESTS, channel=CHANNEL_IN_APP, course=None, enabled=False,
        )
        NotificationPreference.objects.create(
            user=self.user, category=CATEGORY_TESTS, channel=CHANNEL_IN_APP, course=self.course, enabled=True,
        )
        self.assertTrue(services.is_channel_enabled(self.user, CATEGORY_TESTS, CHANNEL_IN_APP, course=self.course))
        self.assertFalse(services.is_channel_enabled(self.user, CATEGORY_TESTS, CHANNEL_IN_APP, course=other_course))

    def test_mandatory_category_cannot_be_disabled_even_with_an_explicit_row(self):
        """§27: 'Do NOT allow users to disable security/mandatory
        transactional messages.' This must hold even if a preference row
        exists saying otherwise — enforced in the service layer, not only
        by refusing to create such a row."""
        NotificationPreference.objects.create(
            user=self.user, category=CATEGORY_SECURITY, channel=CHANNEL_IN_APP, course=None, enabled=False,
        )
        self.assertTrue(services.is_channel_enabled(self.user, CATEGORY_SECURITY, CHANNEL_IN_APP))

    def test_disabled_preference_produces_a_skipped_delivery_not_a_missing_one(self):
        NotificationPreference.objects.create(
            user=self.user, category=CATEGORY_TESTS, channel=CHANNEL_IN_APP, course=None, enabled=False,
        )
        notification = services.create_notification(self.user, 'GRAND_TEST_REMINDER', 'Reminder')
        delivery = notification.deliveries.get(channel=CHANNEL_IN_APP)
        self.assertEqual(delivery.status, NotificationDelivery.STATUS_SKIPPED)


# =====================================================================
# Read/click/mark-all-read
# =====================================================================

class ReadStateTests(APITestCase):
    def setUp(self):
        self.user = _make_user()
        self.other_user = _make_user('student2')

    def test_mark_read_sets_read_at_once(self):
        notification = services.create_notification(self.user, 'ANNOUNCEMENT', 'Hi')
        self.assertIsNone(notification.read_at)
        services.mark_read(notification)
        notification.refresh_from_db()
        self.assertIsNotNone(notification.read_at)

    def test_mark_read_twice_does_not_change_the_timestamp(self):
        notification = services.create_notification(self.user, 'ANNOUNCEMENT', 'Hi')
        services.mark_read(notification)
        first_read_at = Notification.objects.get(pk=notification.pk).read_at
        services.mark_read(notification)
        self.assertEqual(Notification.objects.get(pk=notification.pk).read_at, first_read_at)

    def test_mark_all_read_only_touches_the_given_user(self):
        mine = services.create_notification(self.user, 'ANNOUNCEMENT', 'Hi')
        theirs = services.create_notification(self.other_user, 'ANNOUNCEMENT', 'Hi')
        services.mark_all_read(self.user)
        self.assertIsNotNone(Notification.objects.get(pk=mine.pk).read_at)
        self.assertIsNone(Notification.objects.get(pk=theirs.pk).read_at)

    def test_mark_all_read_scoped_to_course_leaves_other_courses_untouched(self):
        course_a = _make_course('CEE-MBBS')
        course_b = _make_course('BDS')
        notif_a = services.create_notification(self.user, 'GRAND_TEST_REMINDER', 'A', course=course_a)
        notif_b = services.create_notification(self.user, 'GRAND_TEST_REMINDER', 'B', course=course_b)
        services.mark_all_read(self.user, course=course_a)
        self.assertIsNotNone(Notification.objects.get(pk=notif_a.pk).read_at)
        self.assertIsNone(Notification.objects.get(pk=notif_b.pk).read_at)

    def test_mark_clicked_also_marks_read_and_advances_delivery_status(self):
        notification = services.create_notification(self.user, 'ANNOUNCEMENT', 'Hi')
        services.mark_clicked(notification)
        notification.refresh_from_db()
        self.assertIsNotNone(notification.read_at)
        self.assertIsNotNone(notification.clicked_at)
        delivery = notification.deliveries.get(channel=CHANNEL_IN_APP)
        self.assertEqual(delivery.status, NotificationDelivery.STATUS_CLICKED)


# =====================================================================
# Scheduling abstraction (§29) — dispatch_due_notifications / cancel
# =====================================================================

class SchedulingTests(APITestCase):
    def setUp(self):
        self.user = _make_user()

    def test_dispatch_delivers_due_notifications(self):
        past = timezone.now() - timedelta(minutes=5)
        notification = services.create_notification(
            self.user, 'GRAND_TEST_REMINDER', 'Reminder', scheduled_for=past,
        )
        counts = services.dispatch_due_notifications()
        self.assertEqual(counts['dispatched'], 1)
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.STATUS_DISPATCHED)
        delivery = notification.deliveries.get(channel=CHANNEL_IN_APP)
        self.assertEqual(delivery.status, NotificationDelivery.STATUS_DELIVERED)

    def test_dispatch_leaves_future_scheduled_notifications_alone(self):
        future = timezone.now() + timedelta(hours=1)
        notification = services.create_notification(
            self.user, 'GRAND_TEST_REMINDER', 'Reminder', scheduled_for=future,
        )
        counts = services.dispatch_due_notifications()
        self.assertEqual(counts['dispatched'], 0)
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.STATUS_SCHEDULED)

    def test_dispatch_is_idempotent_across_duplicate_runs(self):
        """A Cloud Scheduler hit (or Cloud Tasks retry) firing this sweep
        twice must never double-dispatch."""
        past = timezone.now() - timedelta(minutes=5)
        services.create_notification(self.user, 'GRAND_TEST_REMINDER', 'Reminder', scheduled_for=past)
        first = services.dispatch_due_notifications()
        second = services.dispatch_due_notifications()
        self.assertEqual(first['dispatched'], 1)
        self.assertEqual(second['dispatched'], 0)

    def test_cancel_scheduled_notification_skips_its_queued_deliveries(self):
        future = timezone.now() + timedelta(hours=1)
        notification = services.create_notification(
            self.user, 'GRAND_TEST_REMINDER', 'Reminder', scheduled_for=future,
        )
        result = services.cancel_notification(notification)
        self.assertTrue(result)
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.STATUS_CANCELLED)
        delivery = notification.deliveries.get(channel=CHANNEL_IN_APP)
        self.assertEqual(delivery.status, NotificationDelivery.STATUS_SKIPPED)

    def test_cancel_is_a_no_op_once_already_dispatched(self):
        notification = services.create_notification(self.user, 'ANNOUNCEMENT', 'Hi')  # dispatched immediately
        result = services.cancel_notification(notification)
        self.assertFalse(result)
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.STATUS_DISPATCHED)


# =====================================================================
# Eligibility (§31) — the one real, working example
# =====================================================================

class GrandTestEligibilityTests(APITestCase):
    def setUp(self):
        self.user = _make_user()
        self.test = _make_test(self.user)

    def test_ineligible_with_no_access_grant_at_all(self):
        self.assertFalse(eligibility.is_eligible_for_grand_test_notification(self.user, self.test))

    def test_eligible_once_a_real_grant_exists(self):
        GrandTestAccess.objects.create(user=self.user, test=self.test, granted_at=timezone.now())
        self.assertTrue(eligibility.is_eligible_for_grand_test_notification(self.user, self.test))

    def test_ineligible_once_the_grant_is_revoked(self):
        """§31: eligibility can change between scheduling and delivery — a
        revoked GrandTestAccess (e.g. the paying purchase was refunded)
        must fail this check even though a row still exists."""
        GrandTestAccess.objects.create(
            user=self.user, test=self.test, granted_at=timezone.now(), revoked_at=timezone.now(),
        )
        self.assertFalse(eligibility.is_eligible_for_grand_test_notification(self.user, self.test))


# =====================================================================
# API — ownership/security (§39's spirit) and basic behavior
# =====================================================================

@override_settings(CRON_SECRET='test-cron-secret')
class NotificationApiTests(APITestCase):
    def setUp(self):
        self.user = _make_user()
        self.other_user = _make_user('student2')
        self.course_a = _make_course('CEE-MBBS')
        self.course_b = _make_course('BDS')

    def test_unauthenticated_request_is_rejected(self):
        response = self.client.get(reverse('notifications-mine'))
        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_list_only_returns_the_authenticated_users_own_notifications(self):
        """Mandatory security test per architecture prompt §39: 'user A
        reading user B notifications' must be impossible."""
        services.create_notification(self.user, 'ANNOUNCEMENT', 'Mine')
        services.create_notification(self.other_user, 'ANNOUNCEMENT', 'Theirs')

        self.client.force_authenticate(self.user)
        response = self.client.get(reverse('notifications-mine'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        titles = [row['title'] for row in response.data['results']]
        self.assertEqual(titles, ['Mine'])

    def test_course_filter_only_returns_matching_course(self):
        services.create_notification(self.user, 'GRAND_TEST_REMINDER', 'A', course=self.course_a)
        services.create_notification(self.user, 'GRAND_TEST_REMINDER', 'B', course=self.course_b)

        self.client.force_authenticate(self.user)
        response = self.client.get(reverse('notifications-mine'), {'course': self.course_a.id})

        titles = [row['title'] for row in response.data['results']]
        self.assertEqual(titles, ['A'])

    def test_cannot_mark_another_users_notification_as_read(self):
        """Mandatory §39 test, API-level: forging another user's
        notification id must 404, never succeed and never leak existence
        via a 403."""
        theirs = services.create_notification(self.other_user, 'ANNOUNCEMENT', 'Theirs')

        self.client.force_authenticate(self.user)
        response = self.client.post(reverse('notifications-read', args=[theirs.id]))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.read_at)

    def test_unread_count_reflects_real_state(self):
        n1 = services.create_notification(self.user, 'ANNOUNCEMENT', 'A')
        services.create_notification(self.user, 'ANNOUNCEMENT', 'B')
        services.mark_read(n1)

        self.client.force_authenticate(self.user)
        response = self.client.get(reverse('notifications-unread-count'))

        self.assertEqual(response.data['unread_count'], 1)

    def test_click_endpoint_returns_the_action_url_and_marks_read(self):
        notification = services.create_notification(
            self.user, 'GRAND_TEST_REMINDER', 'Reminder', action_url='/grand-test/45',
        )
        self.client.force_authenticate(self.user)
        response = self.client.post(reverse('notifications-click', args=[notification.id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['action_url'], '/grand-test/45')
        notification.refresh_from_db()
        self.assertIsNotNone(notification.clicked_at)

    def test_preferences_endpoint_only_lists_the_authenticated_users_own_rows(self):
        NotificationPreference.objects.create(user=self.user, category=CATEGORY_TESTS, channel=CHANNEL_IN_APP, enabled=False)
        NotificationPreference.objects.create(user=self.other_user, category=CATEGORY_TESTS, channel=CHANNEL_IN_APP, enabled=False)

        self.client.force_authenticate(self.user)
        response = self.client.get(reverse('notifications-preferences'))

        self.assertEqual(len(response.data), 1)

    def test_preferences_put_rejects_disabling_a_mandatory_category(self):
        self.client.force_authenticate(self.user)
        response = self.client.put(
            reverse('notifications-preferences'),
            [{'category': CATEGORY_SECURITY, 'channel': CHANNEL_IN_APP, 'course': None, 'enabled': False}],
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(NotificationPreference.objects.filter(user=self.user, category=CATEGORY_SECURITY).exists())

    def test_preferences_put_accepts_disabling_a_non_mandatory_category(self):
        self.client.force_authenticate(self.user)
        response = self.client.put(
            reverse('notifications-preferences'),
            [{'category': CATEGORY_TESTS, 'channel': CHANNEL_IN_APP, 'course': None, 'enabled': False}],
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            NotificationPreference.objects.filter(user=self.user, category=CATEGORY_TESTS, enabled=False).exists()
        )

    def test_cron_endpoint_rejects_missing_or_wrong_secret(self):
        response = self.client.post(reverse('cron-dispatch-scheduled-notifications'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        response = self.client.post(
            reverse('cron-dispatch-scheduled-notifications'), HTTP_X_CRON_SECRET='wrong-secret',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_cron_endpoint_dispatches_due_notifications_with_correct_secret(self):
        past = timezone.now() - timedelta(minutes=5)
        notification = services.create_notification(
            self.user, 'GRAND_TEST_REMINDER', 'Reminder', scheduled_for=past,
        )
        response = self.client.post(
            reverse('cron-dispatch-scheduled-notifications'), HTTP_X_CRON_SECRET='test-cron-secret',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['dispatched'], 1)
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.STATUS_DISPATCHED)


# =====================================================================
# P1-PERFORMANCE — query count must not scale with row count
# =====================================================================

class NotificationListQueryCountTests(APITestCase):
    """MyNotificationsView already does select_related('course') (Phase 1)
    specifically to avoid an N+1 on course_name — this proves it, with a
    real query-count number, not just an assertion that the field is
    present. Also covers UnreadCountView, which is a single COUNT(*) by
    construction (no serialization at all)."""

    def setUp(self):
        self.user = _make_user('perf_student')
        self.course = _make_course('CEE-MBBS')
        self.client.force_authenticate(self.user)

    def _query_count_for_n_notifications(self, n):
        Notification.objects.filter(user=self.user).delete()
        for i in range(n):
            services.create_notification(self.user, 'ANNOUNCEMENT', f'Msg {i}', course=self.course)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse('notifications-mine'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), n)
        return len(ctx.captured_queries)

    def test_query_count_does_not_scale_with_row_count(self):
        small = self._query_count_for_n_notifications(5)
        large = self._query_count_for_n_notifications(15)  # still under the 20-per-page cap
        self.assertEqual(
            small, large,
            'MyNotificationsView issued a different number of queries for 5 vs 15 notifications — '
            'select_related(\'course\') is no longer preventing an N+1.',
        )

    def test_unread_count_endpoint_is_a_single_query(self):
        for i in range(10):
            services.create_notification(self.user, 'ANNOUNCEMENT', f'Msg {i}')
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse('notifications-unread-count'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['unread_count'], 10)
        self.assertEqual(len(ctx.captured_queries), 1)
