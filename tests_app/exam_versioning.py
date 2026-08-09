"""Reschedule / Exam Versioning service layer.

Kept separate from views.py so the core logic (lazy template/session
adoption, new-version cloning, double-click-safe session creation) is
independently testable and has one place that understands the
Template -> Version(Test) -> Session -> Attempt relationship.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import ExamSession, ExamTemplate, Test, TestQuestion, TestAttempt


class RescheduleError(Exception):
    """Raised for any validation failure — views translate this to a 400."""


def validate_session_window(start_datetime, end_datetime, registration_deadline=None, *, allow_past=False):
    if end_datetime <= start_datetime:
        raise RescheduleError('End time must be after the start time.')
    if registration_deadline and registration_deadline > start_datetime:
        raise RescheduleError('Registration deadline must be before the exam start time.')
    if not allow_past and start_datetime < timezone.now():
        raise RescheduleError('Cannot schedule an exam session in the past.')


def _infer_backfilled_status(start_datetime, end_datetime):
    """Only used when backfilling a session from a Test's pre-existing
    scheduled_start/end on first reschedule — an explicit, one-time
    reconstruction of history, not the ongoing auto-transition rule."""
    now = timezone.now()
    if end_datetime and end_datetime < now:
        return 'completed'
    if start_datetime and start_datetime <= now:
        return 'live'
    return 'scheduled'


def adopt_test_into_template(test, requesting_user):
    """Idempotent: the first time a Test is rescheduled, give it a stable
    ExamTemplate identity, backfill a Session #1 from its existing
    scheduled_start/end, link its existing attempts to that session (purely
    additive FK assignment — never touches score/rank/answers), and hide it
    from the old flat listing going forward. Returns the (possibly
    newly-created) ExamTemplate. Caller must already hold a lock on `test`."""
    if test.exam_template_id:
        return test.exam_template

    template = ExamTemplate.objects.create(
        title=test.title, exam_type=test.exam_type, created_by=test.created_by or requesting_user,
    )
    test.exam_template = template
    test.version_number = 1
    test.is_draft = True
    test.save(update_fields=['exam_template', 'version_number', 'is_draft'])

    start = test.scheduled_start or test.created_at
    end = test.scheduled_end or (start + timedelta(minutes=test.duration_minutes))
    session = ExamSession.objects.create(
        exam_template=template, exam_version=test,
        session_name=f'{test.title} — Session 1',
        start_datetime=start, end_datetime=end,
        max_attempts=test.max_attempts,
        status=_infer_backfilled_status(start, end),
        created_by=requesting_user,
    )
    TestAttempt.objects.filter(test=test, session__isnull=True).update(session=session)
    return template


@transaction.atomic
def create_reschedule_session(test_id, requesting_user, *, session_name=None, start_datetime, end_datetime,
                               registration_deadline=None, tz_name='Asia/Kathmandu', access_type='all',
                               access_course_ids=None, password='', max_attempts=1,
                               new_version=False, new_version_question_ids=None):
    """The 'Reschedule / Schedule Again' action. By default reuses the same
    Test (Exam Version) and question set — no new Test/TestQuestion rows.
    select_for_update() on the Test row serializes concurrent reschedule
    requests against it, so a double-click can never create two sessions.

    new_version_question_ids is only ever applied when new_version=True —
    structurally, there is no code path here that can modify an existing
    version's questions. Requesting a different question set without
    new_version=True simply leaves the current version untouched."""
    validate_session_window(start_datetime, end_datetime, registration_deadline)

    test = Test.objects.select_for_update().get(pk=test_id)
    template = adopt_test_into_template(test, requesting_user)

    target_test = test
    if new_version:
        target_test = clone_test_as_new_version(test, requesting_user, exam_template=template)
        if new_version_question_ids:
            TestQuestion.objects.filter(test=target_test).delete()
            TestQuestion.objects.bulk_create([
                TestQuestion(test=target_test, question_id=qid, order=i)
                for i, qid in enumerate(new_version_question_ids)
            ])

    if not session_name:
        next_number = template.sessions.count() + 1
        session_name = f'{template.title} — Session {next_number}'

    session = ExamSession.objects.create(
        exam_template=template, exam_version=target_test, session_name=session_name,
        start_datetime=start_datetime, end_datetime=end_datetime,
        registration_deadline=registration_deadline, timezone=tz_name,
        access_type=access_type, password=password, max_attempts=max_attempts,
        status='scheduled', created_by=requesting_user,
    )
    if access_type == 'course' and access_course_ids:
        session.access_courses.set(access_course_ids)
    return session


def clone_test_as_new_version(test, requesting_user, exam_template=None, **overrides):
    """Deep-copies a Test + its TestQuestion rows into a brand new Test row.
    Used by two distinct flows:
      - Create New Version (exam_template=the existing template): the new
        Test becomes the next version under the same exam, for use by new
        sessions going forward. The original version and every session/
        attempt against it are untouched.
      - Duplicate Exam (exam_template=None): a wholly independent exam —
        new template will be created lazily only if *that* copy is itself
        rescheduled later; until then it behaves like any ordinary Test.
    """
    fields = {
        'title': test.title, 'description': test.description, 'difficulty': test.difficulty,
        'exam_type': test.exam_type, 'subject': test.subject, 'duration_minutes': test.duration_minutes,
        'questions_per_page': test.questions_per_page, 'negative_marking': test.negative_marking,
        'shuffle_questions': test.shuffle_questions, 'shuffle_options': test.shuffle_options,
        'max_attempts': test.max_attempts, 'solutions_visibility': test.solutions_visibility,
        'is_pro': test.is_pro, 'is_new': test.is_new, 'price': test.price,
        'access_password': test.access_password, 'free_preview_questions': test.free_preview_questions,
        'academic_year': test.academic_year, 'is_draft': True,
    }
    fields.update(overrides)

    version_number = 1
    if exam_template:
        version_number = exam_template.versions.count() + 1
        fields['title'] = overrides.get('title', test.title)

    new_test = Test.objects.create(
        exam_template=exam_template, version_number=version_number,
        created_by=requesting_user, **fields,
    )
    new_test.courses.set(test.courses.all())
    TestQuestion.objects.bulk_create([
        TestQuestion(test=new_test, question=tq.question, order=tq.order)
        for tq in test.testquestion_set.all()
    ])
    return new_test
