"""Grand Test 3.0 / GT3-6 — the ONLY place Grand Test results ever touch
`smart_practice`. Deliberately thin (per the GT3-6 spec's own §39
"GrandTestPerformanceAdapter... integration layer should remain thin"):
it builds a smart_practice.access.SourceContext for an appeared Grand
Test and calls the EXISTING, unmodified build_candidates()/session
machinery — no algorithm here is a copy of anything in
smart_practice/services.py or source_performance.py, both of which are
imported and reused as-is.

Absolute rule preserved, not weakened: smart_practice.access.
resolve_source_scope() and smart_practice.services.create_session() are
BYTE-FOR-BYTE UNCHANGED by this file — both still reject exam_type=
'grand' exactly as before (confirmed by the full smart_practice
regression suite, unmodified, still passing). This module is a second,
independent, narrower call path that only a Grand-Test-specific view
(GrandTestRecommendationsView / GrandTestPracticeSessionView) ever
invokes — a student can never reach it through the ordinary /smart-
practice/sessions/ endpoint, so a Grand Test still cannot become
'practiceable' through the general-purpose route.

Restricted to mode='source_weak_areas' ONLY (never 'retry_mistakes'
alone, 'concept_reinforcement', 'due_review', 'bookmarked', or
'ai_mixed') — deliberately the one mode whose own candidate logic
(smart_practice.services.build_candidates) already blends a FEW of the
source attempt's own missed questions (bounded to weak topics) with NEW
questions from the authorized expansion pool, rather than only ever
re-serving the Grand Test's original content. This is the same practice
non-scored review any Mock/Daily/PYQ test's own Smart Practice already
offers — it does not create a second official attempt, does not touch
score/rank/percentile, and never lets a Grand Test become unlimited
practice material (GT3-6 spec §15/§55)."""
from dataclasses import dataclass

from academics.access import locked_subject_ids, question_course_scoped
from academics.models import Question
from billing.access import get_grand_test_access
from django.db.models import Q

from .access import SourceContext, SourceScopeError
from .models import SmartPracticeConfig, SmartPracticeSession, SmartPracticeSessionQuestion
from .services import _build_selection_reason, build_candidates
from .source_performance import source_subject_mastery, source_topic_mastery


class GrandTestRecommendationError(SourceScopeError):
    """Same shape as SourceScopeError (code + message) — a distinct class
    only so a caller can tell 'this came from the Grand Test bridge' apart
    from an ordinary smart_practice error if it ever matters; every
    existing _ERROR_STATUS-style status-code mapping still works since
    this IS-A SourceScopeError."""


def resolve_grand_test_source_scope(user, test):
    """The Grand Test analogue of smart_practice.access.resolve_source_scope
    — same SourceContext shape, same expansion-pool derivation logic
    (copied from that function's own body, not import-reused, because the
    authorization CHECKS differ: GrandTestAccess/participation-status
    instead of can_access_test/has_*_access — but the pool-narrowing
    logic itself, once authorized, is identical and correctly duplicated
    here rather than parameterizing the original with a Grand-Test branch
    that would blur its own single responsibility).

    Requires an APPEARED (participation_status == 'completed') Grand
    Test — a missed or in-progress/upcoming/live Grand Test has no
    submitted Answer data to build a SourceContext from at all (GT3-6
    spec §19/§20's own explicit rule: a missed student has content-review
    data, never performance data)."""
    from tests_app.lifecycle import grand_test_participation_status
    from tests_app.models import TestAttempt

    if test.exam_type != 'grand':
        raise GrandTestRecommendationError('not_a_grand_test')

    access = get_grand_test_access(user, test)
    if not access:
        raise GrandTestRecommendationError('not_entitled')

    participation = grand_test_participation_status(user=user, test=test)
    if participation == 'missed':
        raise GrandTestRecommendationError('missed_no_performance_data')
    if participation != 'completed':
        raise GrandTestRecommendationError('not_yet_appeared')

    attempt = (
        TestAttempt.objects.filter(user=user, test=test, status='submitted')
        .order_by('-start_time')
        .first()
    )
    if not attempt:
        # Defensive — 'completed' above already implies this exists;
        # never silently fabricate a SourceContext with no real attempt.
        raise GrandTestRecommendationError('no_submitted_attempt')

    test_questions = list(Question.objects.filter(tests=test).select_related('subject', 'chapter', 'topic'))
    subject_ids = frozenset(q.subject_id for q in test_questions if q.subject_id)
    chapter_ids = frozenset(q.chapter_id for q in test_questions if q.chapter_id)
    topic_ids = frozenset(q.topic_id for q in test_questions if q.topic_id)
    question_ids = frozenset(q.id for q in test_questions)
    course_ids = frozenset(test.courses.values_list('id', flat=True))

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


def create_grand_test_practice_session(user, test):
    """Mirrors smart_practice.services.create_session()'s body exactly —
    same config bounds, same SmartPracticeSession/SmartPracticeSessionQuestion
    creation, same selection-reason builder (all imported, none copied) —
    but resolves scope via resolve_grand_test_source_scope() above instead
    of the ordinary resolve_source_scope(), and hardcodes mode=
    'source_weak_areas' (see this module's own docstring for why that is
    the one safe mode for a Grand Test source)."""
    config = SmartPracticeConfig.load()
    if not config.enabled:
        raise GrandTestRecommendationError('feature_disabled', 'Smart Practice is currently disabled.')

    ctx = resolve_grand_test_source_scope(user, test)
    mode = 'source_weak_areas'
    count = config.default_questions_per_session
    count = max(config.min_questions_per_session, min(count, config.max_questions_per_session))

    candidates = build_candidates(ctx, mode, count, user=user)

    course = None
    if ctx.course_ids:
        from courses.models import Course
        course = Course.objects.filter(id__in=ctx.course_ids).first()

    session = SmartPracticeSession.objects.create(
        user=user, source_test=ctx.test, source_attempt=ctx.attempt, course=course,
        mode=mode, question_count=len(candidates),
        selection_reason=_build_selection_reason(mode, candidates, ctx),
    )
    SmartPracticeSessionQuestion.objects.bulk_create([
        SmartPracticeSessionQuestion(session=session, question=question, order=i, origin=origin)
        for i, (question, origin) in enumerate(candidates)
    ])
    return session


@dataclass(frozen=True)
class GrandTestDiagnosis:
    """The read-only diagnostic summary a Grand Test result page needs —
    subject/topic weakness plus enough counts to decide whether a
    recommendation is even worth showing. Never includes another
    student's data; always scoped to the one (user, test, attempt) this
    was resolved for."""
    subjects: list
    weak_subject: dict | None
    weak_topics: list
    unanswered_count: int
    incorrect_count: int
    correct_count: int
    attempted_count: int


def diagnose_grand_test_performance(ctx):
    """Pure — no writes, safe to call on every result-page load (GT3-6
    §43 caches at the view layer if this ever becomes measurably
    expensive; not pre-optimized here since a single attempt's question
    count is small, matching source_performance.py's own stated
    reasoning for not persisting these aggregates)."""
    from tests_app.models import Answer

    config = SmartPracticeConfig.load()
    subjects = source_subject_mastery(ctx, config.weak_topic_accuracy_max_pct)
    topics = source_topic_mastery(ctx, config.weak_topic_accuracy_max_pct)
    weak_topics = [t for t in topics if t['is_weak']]

    # GT3-6 §6: "Do not blindly use the lowest subject if insufficient
    # data exists" — only ever name a priority subject when it actually
    # has enough attempted questions to be a meaningful signal, not a
    # single lucky/unlucky guess.
    weak_subject = None
    eligible_subjects = [s for s in subjects if s['attempted'] >= config.min_mistakes_to_recommend]
    if eligible_subjects and eligible_subjects[0]['is_weak']:
        weak_subject = eligible_subjects[0]

    answers = Answer.objects.filter(attempt=ctx.attempt)
    incorrect_count = answers.filter(selected_option__isnull=False, is_correct=False).count()
    correct_count = answers.filter(is_correct=True).count()
    # A question the student never touched at all gets no Answer row
    # (only MarkForReviewView/SubmitAnswerView create one) — so
    # 'unanswered' is derived against the test's own question count, not
    # just the null-selected_option Answer rows, or a fully-skipped
    # question would silently vanish from this count entirely.
    unanswered_count = max(len(ctx.question_ids) - correct_count - incorrect_count, 0)

    return GrandTestDiagnosis(
        subjects=subjects, weak_subject=weak_subject, weak_topics=weak_topics,
        unanswered_count=unanswered_count, incorrect_count=incorrect_count, correct_count=correct_count,
        attempted_count=incorrect_count + correct_count,
    )
