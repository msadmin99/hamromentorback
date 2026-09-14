"""Grand Test / Daily Test domain-event integration (Phase 2).

Both exam types share the identical ExamSession-based scheduling mechanism
— confirmed during the Phase 2 precheck (see docs/
PHASE_2_ACCEPTANCE_TRACEABILITY.md): tests_app/models.py's own comment says
a re-released Daily Test also creates a new ExamSession, so this is one
shared implementation, not two.

REMINDER_OFFSETS below is the exact T-24h/T-1h/T-15m schedule the Phase 2
acceptance contract's own §8 specifies. Phase 0's audit confirmed there is
no pre-existing exam-reminder schedule anywhere in this codebase to
preserve instead — this defines it for the first time, exactly as given.

Three call sites in tests_app touch an ExamSession's schedule; each is
hooked independently, in tests_app/views.py and exam_versioning.py (not
in this module — this module only ever computes/creates the
notifications themselves, never decides when it's called):
  1. tests_app.exam_versioning.create_reschedule_session() — the
     "Reschedule / Schedule Again" action, and the ONLY one of the three
     that is actually reachable from the real, deployed frontend/admin
     UI today (confirmed by grepping both apps for real API calls).
     Calls schedule_session_reminders() for the brand-new session it
     creates; never touches any other session (see that function's own
     comment on why "cancel the previous one" would be a guess here).
  2. ExamSessionViewSet.perform_create() — the plain ModelViewSet
     `create` action. Wired for correctness/defense-in-depth, but real
     inspection of ExamSessionSerializer shows `exam_template`/
     `exam_version` are read_only there, so a bare POST can't actually
     satisfy those required FKs — this path is not reachable from any
     real caller today. Documented here rather than silently assumed
     live, per the acceptance contract's "verify, don't assume" rule.
  3. ExamSessionViewSet.perform_update() — real Admin "Edit Session"
     flow (a plain PATCH). When start_datetime/end_datetime actually
     changes, calls services.reschedule_notifications_for_session()
     (hard-delete of this session's stale scheduled reminders) THEN
     schedule_session_reminders() again, so an edited exam time never
     leaves a reminder pointing at the old time.

Every created Notification carries `metadata['session_id']` so
services.cancel_notifications_for_session() /
reschedule_notifications_for_session() can find exactly this session's
own reminders later, never a different session's.
"""
from datetime import timedelta

from django.utils import timezone as dj_timezone

from courses.models import Enrollment

from . import services, templates
from .eligibility import is_eligible_for_daily_test_notification, is_eligible_for_grand_test_notification

GRAND_REMINDER_OFFSETS = [
    ('t_minus_24h', timedelta(hours=24), '24 hours'),
    ('t_minus_1h', timedelta(hours=1), '1 hour'),
    ('t_minus_15m', timedelta(minutes=15), '15 minutes'),
]
DAILY_ENDING_OFFSET = timedelta(minutes=30)


def _eligible_students_for_test(test):
    """Yields (user, course) for every actively-enrolled student in any of
    this test's courses, paired with the SPECIFIC course that makes them
    eligible — never an arbitrary/first course when a test spans several
    (architecture prompt §1: never fake or guess a course). A student
    enrolled in more than one of the test's courses is yielded once per
    matching course; each is a separately, correctly course-scoped
    notification. Commercial+view eligibility (§31, §2) is checked here,
    at schedule time, via the real existing functions — never
    re-implemented — so an ineligible student never gets a row created for
    them in the first place (delivery-time re-checking in
    services.dispatch_due_notifications handles the case where eligibility
    changes AFTER scheduling)."""
    is_eligible = is_eligible_for_grand_test_notification if test.exam_type == 'grand' else is_eligible_for_daily_test_notification
    seen = set()
    for course in test.courses.all():
        for enrollment in Enrollment.objects.filter(course=course, is_active=True).select_related('user'):
            key = (enrollment.user_id, course.id)
            if key in seen:
                continue
            seen.add(key)
            if is_eligible(enrollment.user, test):
                yield enrollment.user, course


def _render(event_type, course, test, **extra):
    context = {'course': course.name, 'test_title': test.title, **extra}
    if templates.has_template(event_type):
        return templates.render(event_type, context)
    return event_type.replace('_', ' ').title(), test.title  # safe fallback, never crashes


def schedule_session_reminders(session):
    """Called once, additively, whenever a real ExamSession is created for
    a Grand Test or Daily Test — see module docstring for both call sites.
    Idempotent: every Notification created here carries a stable
    dedupe_key, so calling this twice for the same session creates no
    duplicate rows (create_notification's own guarantee, exercised
    end-to-end here rather than re-implemented).

    Returns the list of Notifications created (for tests to inspect);
    returns [] for any Test that isn't 'grand' or 'daily', or has no
    courses assigned at all (nothing to resolve an audience from — never
    guesses one)."""
    test = session.exam_version
    if test is None or test.exam_type not in ('grand', 'daily'):
        return []
    if not test.courses.exists():
        return []

    now = dj_timezone.now()
    action_url = f'/grand-test/{test.id}' if test.exam_type == 'grand' else f'/daily-test/{test.id}'
    created = []

    for user, course in _eligible_students_for_test(test):
        immediate_event = 'GRAND_TEST_SCHEDULED' if test.exam_type == 'grand' else 'DAILY_TEST_AVAILABLE'
        title, body = _render(immediate_event, course, test, start_time=session.start_datetime.isoformat())
        created.append(services.create_notification(
            user, immediate_event, title, body,
            course=course, test=test, action_url=action_url,
            metadata={'session_id': session.id},
            dedupe_key=f'exam_session:{session.id}:created:{user.id}',
        ))

        if test.exam_type == 'grand':
            for label, offset, human_label in GRAND_REMINDER_OFFSETS:
                remind_at = session.start_datetime - offset
                if remind_at <= now:
                    continue  # never schedule a reminder for a moment already in the past
                title, body = _render(
                    'GRAND_TEST_REMINDER', course, test,
                    time_remaining=human_label, start_time=session.start_datetime.isoformat(),
                )
                created.append(services.create_notification(
                    user, 'GRAND_TEST_REMINDER', title, body,
                    course=course, test=test, action_url=action_url,
                    metadata={'session_id': session.id, 'offset': label},
                    scheduled_for=remind_at,
                    dedupe_key=f'exam_session:{session.id}:reminder:{label}:{user.id}',
                ))
            if session.start_datetime > now:
                title, body = _render('GRAND_TEST_STARTING', course, test)
                created.append(services.create_notification(
                    user, 'GRAND_TEST_STARTING', title, body,
                    course=course, test=test, action_url=action_url,
                    metadata={'session_id': session.id},
                    scheduled_for=session.start_datetime,
                    dedupe_key=f'exam_session:{session.id}:starting:{user.id}',
                ))
        else:  # daily
            ending_at = session.end_datetime - DAILY_ENDING_OFFSET
            if ending_at > now:
                title, body = _render('DAILY_TEST_ENDING', course, test)
                created.append(services.create_notification(
                    user, 'DAILY_TEST_ENDING', title, body,
                    course=course, test=test, action_url=action_url,
                    metadata={'session_id': session.id},
                    scheduled_for=ending_at,
                    dedupe_key=f'exam_session:{session.id}:ending:{user.id}',
                ))

    return created


def notify_result_available(attempt):
    """Fires once an attempt's result/rank is genuinely viewable — meant to
    be called from wherever solutions/rank actually become visible.
    Phase 2 does not touch that release logic itself; this is only ever
    called AFTER the existing entitlements.services.can_view_solutions /
    can_view_rank checks already say yes, never instead of them."""
    test = attempt.test
    if test.exam_type not in ('grand', 'daily'):
        return None
    course = test.courses.first()  # a single attempt belongs to one student's own course context
    event_type = 'GRAND_TEST_RESULT_AVAILABLE' if test.exam_type == 'grand' else 'DAILY_TEST_RESULT_AVAILABLE'
    title, body = _render(event_type, course, test) if course else (test.title, '')
    action_url = f'/tests/result/{attempt.id}'
    return services.create_notification(
        attempt.user, event_type, title, body,
        course=course, test=test, attempt=attempt, action_url=action_url,
        dedupe_key=f'attempt:{attempt.id}:result_available',
    )
