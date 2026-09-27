"""Exam reminder notification AUDIENCE — business-rule requirement:

    Notification eligibility = active registration in the exam's course.
    NOT: exam purchase / subscription / entitlement.

A student enrolled in a course must receive the "exam starts soon" reminder
whether or not they have bought that specific exam. Commercial entitlement
(billing.access.get_grand_test_access / has_daily_test_access) continues to
gate whether they can actually START the exam — that system is untouched;
only the notification-audience question changed (notifications/eligibility.py).

This file is deliberately separate from notifications/tests_exam_integration.py
(the existing Phase 2 suite, which already proves the reminder LIFECYCLE —
T-24h/T-1h/T-15m scheduling, idempotency, cross-course isolation, cancel,
reschedule — end to end via the real call sites). Two of that file's own
tests directly encoded the bug this fixes and were corrected in place
(see their updated names/docstrings). This file adds the audience-specific
proof the fix requires, numbered to match the acceptance criteria.
"""
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from billing.models import GrandTestAccess
from courses.models import Course, Enrollment
from tests_app.models import ExamSession, ExamTemplate, Test

from . import exam_integration, services
from .events import CHANNEL_EMAIL, CHANNEL_IN_APP, CHANNEL_PUSH
from .models import Notification, NotificationDelivery, NotificationPreference, PushSubscription

User = get_user_model()


def _user(username, **kw):
    # email_verified=True by default: this file proves the course-
    # registration AUDIENCE rule and (in ChannelPreferenceTests)
    # push/email preference fan-out — both predate and are unrelated to
    # email verification, which accounts/tests_email_verification.py
    # covers on its own (including the audience/channel interaction).
    kw.setdefault('email_verified', True)
    return User.objects.create_user(username=username, email=f'{username}@example.com', password='pw12345', **kw)


def _course(name):
    return Course.objects.create(name=name, prefix=name[:10])


def _enroll(user, course, **kw):
    return Enrollment.objects.create(user=user, course=course, **kw)


def _test(exam_type='grand', title='Grand Test I', courses=(), is_pro=False):
    t = Test.objects.create(title=title, exam_type=exam_type, is_draft=False, is_pro=is_pro)
    if courses:
        t.courses.set(courses)
    return t


def _session(test, start, end, exam_template=None):
    template = exam_template or ExamTemplate.objects.create(title=test.title, exam_type=test.exam_type)
    return ExamSession.objects.create(
        exam_template=template, exam_version=test, session_name=f'{test.title} — Session',
        start_datetime=start, end_datetime=end, status='scheduled',
    )


def _push_subscription(user, endpoint_suffix='1'):
    return PushSubscription.objects.create(
        user=user, endpoint=f'https://push.example.com/{user.id}/{endpoint_suffix}',
        p256dh='k' * 20, auth='a' * 10, status=PushSubscription.STATUS_ACTIVE,
    )


class CriticalTestCases(TestCase):
    """Test 1 through 5 from the acceptance criteria, verbatim."""

    def setUp(self):
        self.course_a = _course('CEE-MBBS')
        self.course_b = _course('CEE-BDS')
        self.exam = _test('grand', 'CEE-MBBS Grand Test-I', courses=[self.course_a])
        self.start = timezone.now() + timedelta(days=2)  # comfortably clears the t_minus_24h offset

    def _schedule(self):
        session = _session(self.exam, self.start, self.start + timedelta(hours=2))
        return exam_integration.schedule_session_reminders(session)

    def test_1_registered_no_purchase_receives_reminder(self):
        student = _user('t1_student')
        _enroll(student, self.course_a)

        created = self._schedule()

        self.assertIn(student.id, {n.user_id for n in created})
        self.assertTrue(GrandTestAccess.objects.filter(user=student, test=self.exam).exists() is False)

    def test_2_registered_and_purchased_receives_reminder(self):
        student = _user('t2_student')
        _enroll(student, self.course_a)
        GrandTestAccess.objects.create(user=student, test=self.exam)

        created = self._schedule()

        self.assertIn(student.id, {n.user_id for n in created})

    def test_3_not_registered_but_purchased_does_not_receive_the_course_wide_reminder(self):
        outsider = _user('t3_outsider')
        GrandTestAccess.objects.create(user=outsider, test=self.exam)
        # Deliberately no Enrollment in course_a (or any course) for `outsider`.

        created = self._schedule()

        self.assertNotIn(outsider.id, {n.user_id for n in created})
        self.assertFalse(Notification.objects.filter(user=outsider).exists())

    def test_4_wrong_course_registration_receives_no_reminder(self):
        other_course_student = _user('t4_student')
        _enroll(other_course_student, self.course_b)  # exam belongs to course_a only

        created = self._schedule()

        self.assertNotIn(other_course_student.id, {n.user_id for n in created})

    def test_5_dual_course_student_receives_exactly_one_reminder_per_offset(self):
        student = _user('t5_student')
        _enroll(student, self.course_a)
        _enroll(student, self.course_b)  # also enrolled elsewhere; exam belongs to course_a only

        created = self._schedule()

        student_notifications = [n for n in created if n.user_id == student.id]
        # One SCHEDULED, one each of the 3 GRAND_TEST_REMINDER offsets, one STARTING — never doubled.
        self.assertEqual(len(student_notifications), 5)
        course_ids = {n.course_id for n in student_notifications}
        self.assertEqual(course_ids, {self.course_a.id})
        reminder_offsets = [n.metadata['offset'] for n in student_notifications if n.event_type == 'GRAND_TEST_REMINDER']
        self.assertEqual(sorted(reminder_offsets), ['t_minus_15m', 't_minus_1h', 't_minus_24h'])


class CourseSpecificAudienceScaleTests(TestCase):
    """"Course A: 100 registered / Course B: 50 registered; exam belongs to
    Course A -> exactly the 100 Course A students, never Course B, never
    "all users", never filtered by exam purchase." Uses smaller counts for
    test speed but the identical shape/assertions."""

    def test_course_specific_audience_not_purchasers_not_all_users_not_other_course(self):
        course_a = _course('Course A')
        course_b = _course('Course B')
        a_students = [_user(f'a{i}') for i in range(12)]
        b_students = [_user(f'b{i}') for i in range(6)]
        for s in a_students:
            _enroll(s, course_a)
        for s in b_students:
            _enroll(s, course_b)
        # One Course A student purchases; one non-enrolled user purchases; neither changes the audience.
        exam = _test('grand', 'Course A Exam', courses=[course_a])
        GrandTestAccess.objects.create(user=a_students[0], test=exam)
        stranger = _user('stranger')
        GrandTestAccess.objects.create(user=stranger, test=exam)

        start = timezone.now() + timedelta(days=1)
        session = _session(exam, start, start + timedelta(hours=2))
        created = exam_integration.schedule_session_reminders(session)

        notified_ids = {n.user_id for n in created}
        self.assertEqual(notified_ids, {s.id for s in a_students})
        self.assertEqual(len({n.user_id for n in created if n.event_type == 'GRAND_TEST_SCHEDULED'}), 12)
        for s in b_students:
            self.assertNotIn(s.id, notified_ids)
        self.assertNotIn(stranger.id, notified_ids)


class ChannelPreferenceTests(TestCase):
    """Test 6 / 7 — email/push preference resolution is untouched by the
    audience fix; a course-registered, non-purchasing student still gets
    channel fan-out exactly like any other notification."""

    def setUp(self):
        self.course = _course('CEE-MBBS')
        self.exam = _test('grand', 'Grand Test', courses=[self.course])

    def _immediate_notification_for(self, student):
        session = _session(self.exam, timezone.now() + timedelta(days=1), timezone.now() + timedelta(days=1, hours=2))
        created = exam_integration.schedule_session_reminders(session)
        return next(n for n in created if n.user_id == student.id and n.event_type == 'GRAND_TEST_SCHEDULED')

    def test_6_email_disabled_push_enabled_sends_push_only(self):
        student = _user('pref_student1')
        _enroll(student, self.course)
        _push_subscription(student)
        NotificationPreference.objects.create(user=student, category='tests', channel=CHANNEL_EMAIL, enabled=False)

        n = self._immediate_notification_for(student)

        channels = set(n.deliveries.exclude(status=NotificationDelivery.STATUS_SKIPPED).values_list('channel', flat=True))
        self.assertIn(CHANNEL_PUSH, channels)
        self.assertNotIn(CHANNEL_EMAIL, channels)
        skipped = set(n.deliveries.filter(status=NotificationDelivery.STATUS_SKIPPED).values_list('channel', flat=True))
        self.assertIn(CHANNEL_EMAIL, skipped)

    def test_7_no_push_subscription_email_enabled_sends_email_only(self):
        student = _user('pref_student2')
        _enroll(student, self.course)
        # No PushSubscription at all for this student.

        n = self._immediate_notification_for(student)

        channels = set(n.deliveries.values_list('channel', flat=True))
        self.assertNotIn(CHANNEL_PUSH, channels)  # never even a row: _resolve_channels excludes it
        self.assertIn(CHANNEL_EMAIL, channels)
        email_delivery = n.deliveries.get(channel=CHANNEL_EMAIL)
        self.assertNotEqual(email_delivery.status, NotificationDelivery.STATUS_SKIPPED)

    def test_both_channels_enabled_and_available_sends_both(self):
        student = _user('pref_student3')
        _enroll(student, self.course)
        _push_subscription(student)

        n = self._immediate_notification_for(student)

        channels = set(n.deliveries.exclude(status=NotificationDelivery.STATUS_SKIPPED).values_list('channel', flat=True))
        self.assertEqual(channels, {CHANNEL_IN_APP, CHANNEL_PUSH, CHANNEL_EMAIL})

    def test_both_channels_disabled_delivers_neither(self):
        student = _user('pref_student4')
        _enroll(student, self.course)
        _push_subscription(student)
        NotificationPreference.objects.create(user=student, category='tests', channel=CHANNEL_EMAIL, enabled=False)
        NotificationPreference.objects.create(user=student, category='tests', channel=CHANNEL_PUSH, enabled=False)

        n = self._immediate_notification_for(student)

        delivered = set(n.deliveries.exclude(status=NotificationDelivery.STATUS_SKIPPED).values_list('channel', flat=True))
        self.assertEqual(delivered, {CHANNEL_IN_APP})  # in-app is not preference-gated by this test's setup either way — mandatory-category style always fires
        skipped = set(n.deliveries.filter(status=NotificationDelivery.STATUS_SKIPPED).values_list('channel', flat=True))
        self.assertEqual(skipped, {CHANNEL_EMAIL, CHANNEL_PUSH})

    def test_purchase_status_has_no_bearing_on_channel_resolution(self):
        """The audience fix must not accidentally couple channel fan-out to
        commercial access either — a purchaser and a non-purchaser get
        identical channel treatment for the same preferences."""
        purchaser = _user('purchaser')
        non_purchaser = _user('non_purchaser')
        for s in (purchaser, non_purchaser):
            _enroll(s, self.course)
            _push_subscription(s)
        GrandTestAccess.objects.create(user=purchaser, test=self.exam)

        n1 = self._immediate_notification_for(purchaser)
        n2 = self._immediate_notification_for(non_purchaser)

        c1 = set(n1.deliveries.exclude(status=NotificationDelivery.STATUS_SKIPPED).values_list('channel', flat=True))
        c2 = set(n2.deliveries.exclude(status=NotificationDelivery.STATUS_SKIPPED).values_list('channel', flat=True))
        self.assertEqual(c1, c2)


class EnrollmentStatusTests(TestCase):
    """Test 8 / 9 — active-registration rule, using the existing
    Enrollment.is_active / expires_at fields (courses.access.eligible_course_ids),
    never a new enrollment-status concept."""

    def setUp(self):
        self.course = _course('CEE-MBBS')
        self.exam = _test('grand', 'Grand Test', courses=[self.course])

    def _schedule(self):
        start = timezone.now() + timedelta(days=1)
        session = _session(self.exam, start, start + timedelta(hours=2))
        return exam_integration.schedule_session_reminders(session)

    def test_8_expired_enrollment_is_excluded(self):
        student = _user('expired_student')
        _enroll(student, self.course, expires_at=timezone.now() - timedelta(days=1))

        created = self._schedule()

        self.assertNotIn(student.id, {n.user_id for n in created})

    def test_8_revoked_is_active_false_enrollment_is_excluded(self):
        student = _user('revoked_student')
        _enroll(student, self.course, is_active=False)

        created = self._schedule()

        self.assertNotIn(student.id, {n.user_id for n in created})

    def test_8_not_yet_expired_enrollment_is_included(self):
        student = _user('still_active_student')
        _enroll(student, self.course, expires_at=timezone.now() + timedelta(days=30))

        created = self._schedule()

        self.assertIn(student.id, {n.user_id for n in created})

    def test_9_registering_after_the_reminder_was_already_sent_is_not_retroactive(self):
        """Scheduling only ever considers who is enrolled AT SCHEDULE TIME —
        schedule_session_reminders is called once (real call sites: session
        create/reschedule), not re-run as new students enroll, so a student
        who registers afterward gets no reminder for a session already
        scheduled before they joined."""
        already_registered = _user('early_student')
        _enroll(already_registered, self.course)

        created = self._schedule()
        self.assertIn(already_registered.id, {n.user_id for n in created})

        late_student = _user('late_student')
        _enroll(late_student, self.course)  # registers AFTER scheduling already ran

        self.assertFalse(Notification.objects.filter(user=late_student).exists())

    def test_9_delivery_time_recheck_excludes_enrollment_that_lapsed_after_scheduling(self):
        """Companion to Test 9: if an enrollment becomes inactive between
        scheduling and the reminder's due time, dispatch_due_notifications'
        delivery-time re-check (eligibility.recheck_notification_eligibility)
        cancels it rather than delivering to a no-longer-registered student."""
        student = _user('lapsing_student')
        enrollment = _enroll(student, self.course)
        start = timezone.now() + timedelta(minutes=90)
        session = _session(self.exam, start, start + timedelta(hours=2))
        exam_integration.schedule_session_reminders(session)
        reminder = Notification.objects.get(user=student, event_type='GRAND_TEST_REMINDER', metadata__offset='t_minus_1h')

        enrollment.is_active = False
        enrollment.save(update_fields=['is_active'])

        counts = services.dispatch_due_notifications(now=start - timedelta(hours=1) + timedelta(seconds=1))

        reminder.refresh_from_db()
        self.assertEqual(reminder.status, Notification.STATUS_CANCELLED)
        self.assertEqual(counts['cancelled_ineligible'], 1)


class DeduplicationTests(TestCase):
    """Test 10 — scheduler run twice / retried task / duplicate deployment
    trigger must never double up a student's reminder on any channel."""

    def setUp(self):
        self.course = _course('CEE-MBBS')
        self.student = _user('dedup_student')
        _enroll(self.student, self.course)
        _push_subscription(self.student)
        self.exam = _test('grand', 'Grand Test', courses=[self.course])

    def test_10_scheduler_run_twice_creates_exactly_one_notification_per_offset(self):
        start = timezone.now() + timedelta(days=2)  # comfortably clears the t_minus_24h offset
        session = _session(self.exam, start, start + timedelta(hours=2))

        exam_integration.schedule_session_reminders(session)
        exam_integration.schedule_session_reminders(session)  # simulates a re-run (retry/redeploy/duplicate trigger)

        for label in ('t_minus_24h', 't_minus_1h', 't_minus_15m'):
            self.assertEqual(
                Notification.objects.filter(user=self.student, event_type='GRAND_TEST_REMINDER', metadata__offset=label).count(),
                1, f'offset {label} was duplicated',
            )
        self.assertEqual(Notification.objects.filter(user=self.student, event_type='GRAND_TEST_SCHEDULED').count(), 1)

    def test_10_dispatch_due_notifications_run_twice_delivers_each_channel_once(self):
        start = timezone.now() + timedelta(minutes=90)
        session = _session(self.exam, start, start + timedelta(hours=2))
        exam_integration.schedule_session_reminders(session)
        due_at = start - timedelta(hours=1) + timedelta(seconds=1)

        services.dispatch_due_notifications(now=due_at)
        services.dispatch_due_notifications(now=due_at)  # duplicate sweep

        reminder = Notification.objects.get(user=self.student, event_type='GRAND_TEST_REMINDER', metadata__offset='t_minus_1h')
        self.assertEqual(reminder.status, Notification.STATUS_DISPATCHED)
        # dispatch is a status-gated transition (STATUS_SCHEDULED -> DISPATCHED); the
        # second sweep's queryset no longer matches this notification at all.
        self.assertEqual(
            Notification.objects.filter(id=reminder.id).exclude(status=Notification.STATUS_DISPATCHED).count(), 0,
        )

    def test_10_concurrent_identical_dedupe_key_calls_create_only_one_row(self):
        """The lower-level guarantee create_notification itself provides
        (concurrent/duplicate-process safe, per its own docstring), proven
        directly rather than only through the scheduler wrapper above."""
        from . import services as svc
        key = f'exam_session:999:reminder:t_minus_1h:{self.student.id}'
        n1 = svc.create_notification(self.student, 'GRAND_TEST_REMINDER', 'Reminder', dedupe_key=key)
        n2 = svc.create_notification(self.student, 'GRAND_TEST_REMINDER', 'Reminder (again)', dedupe_key=key)
        self.assertEqual(n1.id, n2.id)
        self.assertEqual(Notification.objects.filter(dedupe_key=key).count(), 1)


class RescheduleCancelLateSchedulingTests(TestCase):
    """Test 11 / 12 / 13."""

    def setUp(self):
        self.course = _course('CEE-MBBS')
        self.student = _user('resched_student')
        _enroll(self.student, self.course)
        self.exam = _test('grand', 'Grand Test', courses=[self.course])
        self.template = ExamTemplate.objects.create(title=self.exam.title, exam_type='grand')

    def test_11_rescheduling_cancels_old_reminder_and_creates_new_one_never_both(self):
        old_start = timezone.now() + timedelta(days=2)
        session = _session(self.exam, old_start, old_start + timedelta(hours=2), exam_template=self.template)
        exam_integration.schedule_session_reminders(session)
        old_reminder_id = Notification.objects.get(
            user=self.student, event_type='GRAND_TEST_REMINDER', metadata__offset='t_minus_1h',
        ).id

        new_start = old_start + timedelta(hours=5)  # e.g. 8:00 -> 9:00 the next relevant day, in spirit
        session.start_datetime = new_start
        session.end_datetime = new_start + timedelta(hours=2)
        session.save(update_fields=['start_datetime', 'end_datetime'])
        services.reschedule_notifications_for_session(session)
        exam_integration.schedule_session_reminders(session)

        # Old reminder is gone entirely (hard-deleted, not left pending to double-send)...
        self.assertFalse(Notification.objects.filter(id=old_reminder_id).exists())
        # ...exactly one new one exists, at the corrected time, and nothing was ever double-sent.
        new_reminders = Notification.objects.filter(
            user=self.student, event_type='GRAND_TEST_REMINDER', metadata__offset='t_minus_1h',
        )
        self.assertEqual(new_reminders.count(), 1)
        self.assertEqual(new_reminders.get().scheduled_for, new_start - timedelta(hours=1))

    def test_12_cancelling_the_exam_cancels_the_pending_reminder_none_is_sent(self):
        start = timezone.now() + timedelta(minutes=90)
        session = _session(self.exam, start, start + timedelta(hours=2), exam_template=self.template)
        exam_integration.schedule_session_reminders(session)

        services.cancel_notifications_for_session(session)

        reminder = Notification.objects.get(user=self.student, event_type='GRAND_TEST_REMINDER', metadata__offset='t_minus_1h')
        self.assertEqual(reminder.status, Notification.STATUS_CANCELLED)
        # Even if the scheduler sweep runs at the reminder's original due time, nothing is delivered.
        counts = services.dispatch_due_notifications(now=start - timedelta(hours=1) + timedelta(seconds=1))
        self.assertEqual(counts['dispatched'], 0)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, Notification.STATUS_CANCELLED)

    def test_13_exam_created_less_than_one_hour_before_start_never_sends_a_fake_past_reminder(self):
        """Late scheduling: the exam starts in 30 minutes, so the T-24h and
        T-1h offsets already point at the past — schedule_session_reminders
        never creates those rows at all (skipped by construction, per its
        own `if remind_at <= now: continue` guard) rather than creating one
        that would fire immediately/late with a wrong "1 hour before"
        claim. Only the still-future T-15m reminder (and STARTING) exist."""
        start = timezone.now() + timedelta(minutes=30)
        session = _session(self.exam, start, start + timedelta(hours=1), exam_template=self.template)

        created = exam_integration.schedule_session_reminders(session)

        offsets_created = {n.metadata['offset'] for n in created if n.event_type == 'GRAND_TEST_REMINDER'}
        self.assertEqual(offsets_created, {'t_minus_15m'})
        self.assertFalse(Notification.objects.filter(
            user=self.student, event_type='GRAND_TEST_REMINDER', metadata__offset='t_minus_1h',
        ).exists())
        self.assertFalse(Notification.objects.filter(
            user=self.student, event_type='GRAND_TEST_REMINDER', metadata__offset='t_minus_24h',
        ).exists())

    def test_13_exam_created_after_its_own_start_time_sends_no_reminders_at_all(self):
        """An even later case: the exam already started by the time the
        session is created (e.g. a backfilled/late admin action) — every
        offset AND the STARTING notification are in the past; only the
        immediate SCHEDULED/AVAILABLE announcement is created."""
        start = timezone.now() - timedelta(minutes=5)
        session = _session(self.exam, start, start + timedelta(hours=1), exam_template=self.template)

        created = exam_integration.schedule_session_reminders(session)

        self.assertEqual([n.event_type for n in created], ['GRAND_TEST_SCHEDULED'])


class TimezoneTests(TestCase):
    """Test 14 — Asia/Kathmandu (UTC+05:45), never a naive local timestamp.
    ExamSession.start_datetime is a tz-aware DateTimeField (settings.
    USE_TZ=True); this proves the T-1h arithmetic operates on the correct
    absolute instant when the session's wall-clock time is expressed in
    Nepal's own timezone, not just when it happens to already be UTC."""

    def test_reminder_is_exactly_one_hour_before_start_in_kathmandu_time(self):
        course = _course('CEE-MBBS')
        student = _user('ktm_student')
        _enroll(student, course)
        exam = _test('grand', 'Grand Test', courses=[course])

        kathmandu = ZoneInfo('Asia/Kathmandu')
        # 8:00 AM Kathmandu, three days out — constructed via an explicit
        # zone, never `datetime(...)` left naive.
        local_start = (timezone.now() + timedelta(days=3)).astimezone(kathmandu).replace(
            hour=8, minute=0, second=0, microsecond=0,
        )
        session = _session(exam, local_start, local_start + timedelta(hours=2))

        created = exam_integration.schedule_session_reminders(session)

        reminder = next(n for n in created if n.event_type == 'GRAND_TEST_REMINDER' and n.metadata['offset'] == 't_minus_1h')
        # The absolute instant is exactly 1 hour earlier, regardless of which
        # zone either side is expressed in.
        self.assertEqual(reminder.scheduled_for, local_start - timedelta(hours=1))
        # And explicitly, in Kathmandu wall-clock terms: 8:00 AM -> 7:00 AM.
        self.assertEqual(reminder.scheduled_for.astimezone(kathmandu).strftime('%H:%M'), '07:00')
        self.assertEqual(local_start.astimezone(kathmandu).strftime('%H:%M'), '08:00')
        # And the UTC offset arithmetic is correct (Kathmandu = UTC+05:45):
        self.assertEqual(local_start.astimezone(ZoneInfo('UTC')).strftime('%H:%M'), '02:15')
        self.assertEqual(reminder.scheduled_for.astimezone(ZoneInfo('UTC')).strftime('%H:%M'), '01:15')


class EntitlementIndependenceTests(TestCase):
    """Test 15 / 16 — the two directions of "keep these systems separate":
    (15) no exam entitlement is required to RECEIVE the reminder;
    (16) receiving the reminder does not GRANT entitlement — exam access
    still runs through the untouched, existing billing/access system."""

    def setUp(self):
        self.course = _course('CEE-MBBS')
        self.student = _user('separation_student')
        _enroll(self.student, self.course)
        self.exam = _test('grand', 'Grand Test', courses=[self.course])

    def test_15_no_grandtestaccess_row_exists_yet_reminder_is_still_created(self):
        self.assertFalse(GrandTestAccess.objects.filter(user=self.student, test=self.exam).exists())

        start = timezone.now() + timedelta(days=1)
        session = _session(self.exam, start, start + timedelta(hours=2))
        created = exam_integration.schedule_session_reminders(session)

        self.assertIn(self.student.id, {n.user_id for n in created})
        # Still no entitlement — proving the reminder path never reads/writes billing state.
        self.assertFalse(GrandTestAccess.objects.filter(user=self.student, test=self.exam).exists())

    def test_16_receiving_the_reminder_does_not_grant_exam_access(self):
        from billing.access import get_grand_test_access
        from tests_app.access import can_access_test

        start = timezone.now() + timedelta(days=1)
        session = _session(self.exam, start, start + timedelta(hours=2))
        exam_integration.schedule_session_reminders(session)
        self.assertTrue(Notification.objects.filter(user=self.student).exists())

        # Course registration lets the student SEE the exam exists (unchanged, pre-existing behavior)...
        self.assertTrue(can_access_test(self.student, self.exam))
        # ...but commercial access is still absent — receiving a reminder granted nothing.
        self.assertIsNone(get_grand_test_access(self.student, self.exam))

    def test_16_start_attempt_style_access_check_still_requires_commercial_entitlement(self):
        """Backward compatibility: a student with no subscription must still
        be blocked from actually starting a restricted exam, exactly as
        before this change — billing.access.get_grand_test_access is
        entirely untouched by the notification-audience fix."""
        from billing.access import get_grand_test_access

        start = timezone.now() + timedelta(days=1)
        session = _session(self.exam, start, start + timedelta(hours=2))
        exam_integration.schedule_session_reminders(session)

        self.assertIsNone(get_grand_test_access(self.student, self.exam))
        GrandTestAccess.objects.create(user=self.student, test=self.exam)
        self.assertIsNotNone(get_grand_test_access(self.student, self.exam))


class AudienceCountsTests(TestCase):
    """Admin-facing preview numbers (registered / subscribers / reminder
    audience) — informational only, never used to filter the real audience."""

    def test_counts_distinguish_registered_from_subscribers(self):
        course = _course('CEE-MBBS')
        exam = _test('grand', 'Grand Test', courses=[course])
        registered = [_user(f'aud{i}') for i in range(5)]
        for s in registered:
            _enroll(s, course)
        GrandTestAccess.objects.create(user=registered[0], test=exam)
        GrandTestAccess.objects.create(user=registered[1], test=exam)
        outsider = _user('aud_outsider')
        GrandTestAccess.objects.create(user=outsider, test=exam)  # purchased, not registered

        counts = exam_integration.audience_counts(exam)

        self.assertEqual(counts['registered_students'], 5)
        self.assertEqual(counts['reminder_audience'], 5)  # course registration is the whole audience
        self.assertEqual(counts['exam_subscribers'], 2)  # informational only; outsider's purchase doesn't count here

    def test_counts_are_zero_for_an_exam_with_no_courses(self):
        exam = _test('grand', 'Unassigned', courses=())
        self.assertEqual(
            exam_integration.audience_counts(exam),
            {'registered_students': 0, 'exam_subscribers': 0, 'reminder_audience': 0},
        )

    def test_counts_exclude_a_draft_exam_from_reminder_audience_but_not_registered_count(self):
        course = _course('CEE-MBBS')
        exam = _test('grand', 'Draft Exam', courses=[course])
        exam.is_draft = True
        exam.save(update_fields=['is_draft'])
        student = _user('draft_student')
        _enroll(student, course)

        counts = exam_integration.audience_counts(exam)

        self.assertEqual(counts['registered_students'], 1)
        self.assertEqual(counts['reminder_audience'], 0)  # can_view_test denies a draft exam
