"""Source authorization — the ONE place a Smart Practice request resolves
"which Test, is this student even allowed near it, and what question pool
may a recommendation draw from." Deliberately thin: every actual
eligibility rule is delegated to the existing, already-hardened
tests_app.access / billing.access / academics.access modules — this file
composes them, it never reimplements them.

Grand Test is rejected here, before any authorization check runs at all,
so a leaked/guessed Grand Test id can never reach an authorization branch
that might (today or in a future edit) accidentally allow it through.
"""
from dataclasses import dataclass, field

from django.db.models import Q

from academics.access import locked_subject_ids, question_course_scoped
from academics.models import Question
from billing.access import has_daily_test_access, has_mock_test_access, has_pyq_access
from tests_app.access import can_access_test
from tests_app.models import Test, TestAttempt


class SourceScopeError(Exception):
    """code is one of: grand_test_excluded | not_found | not_authorized |
    subscription_required | no_submitted_attempt — endpoints translate
    this to an HTTP status, no authorization logic lives in the views."""

    def __init__(self, code, message=''):
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class SourceContext:
    test: Test
    attempt: TestAttempt
    exam_type: str
    course_ids: frozenset
    subject_ids: frozenset
    chapter_ids: frozenset
    topic_ids: frozenset
    question_ids: frozenset
    expansion_pool: object = field(repr=False)  # authorized Question queryset, narrower than platform-wide


def _has_commercial_access(user, test):
    """Mirrors tests_app.views._start_attempt's exact per-exam_type
    branching (the daily free-preview allowance included) — re-checked
    here rather than assumed, since a subscription can lapse between
    taking a test and later requesting Smart Practice on it."""
    if not test.is_pro:
        return True
    if test.exam_type in ('mock', 'qbank'):
        return has_mock_test_access(user, test)
    if test.exam_type == 'daily':
        return has_daily_test_access(user, test) or test.free_preview_questions > 0
    if test.exam_type == 'pyq':
        return has_pyq_access(user, test)
    return True


def resolve_source_scope(user, source_test_id):
    try:
        test = Test.objects.select_related('subject').get(pk=source_test_id)
    except Test.DoesNotExist as exc:
        raise SourceScopeError('not_found') from exc

    if test.exam_type == 'grand':
        raise SourceScopeError('grand_test_excluded', 'Smart Practice is not available for Grand Test.')

    if not can_access_test(user, test):
        raise SourceScopeError('not_authorized')

    if not _has_commercial_access(user, test):
        raise SourceScopeError('subscription_required')

    attempt = (
        TestAttempt.objects.filter(user=user, test=test, status='submitted')
        .order_by('-start_time')
        .first()
    )
    if not attempt:
        raise SourceScopeError('no_submitted_attempt')

    test_questions = list(Question.objects.filter(tests=test).select_related('subject', 'chapter', 'topic'))
    subject_ids = frozenset(q.subject_id for q in test_questions if q.subject_id)
    chapter_ids = frozenset(q.chapter_id for q in test_questions if q.chapter_id)
    topic_ids = frozenset(q.topic_id for q in test_questions if q.topic_id)
    question_ids = frozenset(q.id for q in test_questions)

    course_ids = frozenset(test.courses.values_list('id', flat=True))

    # Expansion pool: platform-wide course-eligible questions (the same
    # rule QBank practice itself uses) narrowed to (a) THIS test's own
    # course(s) specifically — not just any course the student happens to
    # be enrolled in, which would leak a shared subject across an
    # unrelated course (the exact leak class tests_app/performance.py's
    # recommendations() already had to fix once) — and (b) not a
    # Pro-locked subject the student has no QBank subscription for.
    expansion_pool = question_course_scoped(Question.objects.all(), user)
    if course_ids:
        expansion_pool = expansion_pool.filter(
            Q(courses__id__in=course_ids) | Q(courses__isnull=True, subject__courses__id__in=course_ids)
        )
    locked = locked_subject_ids(user)
    if locked:
        expansion_pool = expansion_pool.exclude(subject_id__in=locked)
    expansion_pool = expansion_pool.distinct()

    return SourceContext(
        test=test, attempt=attempt, exam_type=test.exam_type,
        course_ids=course_ids, subject_ids=subject_ids, chapter_ids=chapter_ids,
        topic_ids=topic_ids, question_ids=question_ids, expansion_pool=expansion_pool,
    )
