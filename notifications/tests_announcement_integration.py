"""Phase 2 — System Announcement integration tests.

Drives the real trigger: creating a core.models.Announcement row (exactly
what Django Admin's AnnouncementAdmin does — zero changes needed there,
per notifications/signals.py's own docstring).
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import Announcement

from .models import Notification

User = get_user_model()


def _make_user(username, is_staff=False, is_active=True):
    return User.objects.create_user(
        username=username, email=f'{username}@example.com', password='pw12345',
        is_staff=is_staff, is_active=is_active,
    )


class AnnouncementNotificationTests(TestCase):
    def test_creating_an_active_announcement_notifies_every_active_student(self):
        student_a = _make_user('ann_student_a')
        student_b = _make_user('ann_student_b')
        staff = _make_user('ann_staff', is_staff=True)
        inactive = _make_user('ann_inactive', is_active=False)

        announcement = Announcement.objects.create(message='50% off this weekend only!')

        for user in (student_a, student_b):
            n = Notification.objects.get(user=user, event_type='ANNOUNCEMENT')
            self.assertEqual(n.body, announcement.message)
            self.assertEqual(n.title, 'Dr. Gutka')

        self.assertFalse(Notification.objects.filter(user=staff, event_type='ANNOUNCEMENT').exists())
        self.assertFalse(Notification.objects.filter(user=inactive, event_type='ANNOUNCEMENT').exists())

    def test_inactive_announcement_notifies_no_one(self):
        student = _make_user('ann_student_c')
        Announcement.objects.create(message='Draft banner', is_active=False)
        self.assertFalse(Notification.objects.filter(user=student, event_type='ANNOUNCEMENT').exists())

    def test_editing_an_existing_announcement_does_not_renotify(self):
        student = _make_user('ann_student_d')
        announcement = Announcement.objects.create(message='Original message')
        self.assertEqual(Notification.objects.filter(user=student, event_type='ANNOUNCEMENT').count(), 1)

        announcement.message = 'Edited message'
        announcement.save()

        self.assertEqual(Notification.objects.filter(user=student, event_type='ANNOUNCEMENT').count(), 1)
        # The existing notification still shows the ORIGINAL text — Phase 2
        # deliberately does not retroactively rewrite an already-created
        # notification when the source row is edited later.
        n = Notification.objects.get(user=student, event_type='ANNOUNCEMENT')
        self.assertEqual(n.body, 'Original message')

    def test_two_users_created_after_the_announcement_are_not_retroactively_notified(self):
        Announcement.objects.create(message='Early bird message')
        late_signup = _make_user('ann_late_signup')
        self.assertFalse(Notification.objects.filter(user=late_signup, event_type='ANNOUNCEMENT').exists())
