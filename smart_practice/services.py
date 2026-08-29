"""Candidate generation + session lifecycle. The single global mastery
write path (academics.services.record_question_result) is reused
unmodified — this module never writes QuestionAttempt/QuestionEvent
directly, it only calls into that existing function with source='smart'."""
from django.utils import timezone

from academics.models import QuestionAttempt
from academics.services import record_question_result
from tests_app.models import TestQuestion

from .access import SourceScopeError, resolve_source_scope
from .models import SmartPracticeConfig, SmartPracticeSession, SmartPracticeSessionQuestion
from .source_performance import source_missed_questions, source_topic_mastery

# Every mode's questions must come from an already-authorized pool —
# ctx.expansion_pool (course/Pro-lock scoped by resolve_source_scope) or a
# subset of ctx.expansion_pool via QuestionAttempt.question__in=. Global
# signals (revision_due_at, is_bookmarked, "never attempted") live on
# QuestionAttempt platform-wide, but every mode here intersects them with
# THIS source's authorized pool first — a student due for review on a
# question from an unrelated course must never surface inside this test's
# Smart Practice, even though the review signal itself is real and global.
MODES_FOR_AI_MIX = ('retry_mistakes', 'source_weak_areas', 'due_review', 'new_questions')


def _test_question_order(ctx):
    return dict(TestQuestion.objects.filter(test=ctx.test).values_list('question_id', 'order'))


def due_review_candidates(ctx, user):
    now = timezone.now()
    due_ids = list(
        QuestionAttempt.objects.filter(user=user, revision_due_at__lte=now, question__in=ctx.expansion_pool)
        .order_by('revision_due_at')
        .values_list('question_id', flat=True)
    )
    by_id = {q.id: q for q in ctx.expansion_pool.filter(id__in=due_ids)}
    return [by_id[qid] for qid in due_ids if qid in by_id]


def new_question_candidates(ctx, user):
    attempted_ids = QuestionAttempt.objects.filter(user=user).values_list('question_id', flat=True)
    return list(ctx.expansion_pool.exclude(id__in=attempted_ids).order_by('?'))


def bookmarked_candidates(ctx, user):
    bookmarked_ids = QuestionAttempt.objects.filter(
        user=user, is_bookmarked=True, question__in=ctx.expansion_pool,
    ).values_list('question_id', flat=True)
    return list(ctx.expansion_pool.filter(id__in=bookmarked_ids))


def build_candidates(ctx, mode, count, user=None):
    """Returns [(Question, origin_code), ...], authorization-filtered
    (every question here already passed through resolve_source_scope's
    expansion_pool, which is course/Pro-lock scoped) BEFORE any ranking —
    weakness/relevance ranking never sees an unauthorized question."""
    order_map = _test_question_order(ctx)
    candidates = []
    seen_ids = set()

    def add_missed(topic_filter=None):
        missed = source_missed_questions(ctx)
        for answer in sorted(missed, key=lambda a: order_map.get(a.question_id, 0)):
            q = answer.question
            if q.id in seen_ids:
                continue
            if topic_filter is not None and q.topic_id not in topic_filter:
                continue
            candidates.append((q, 'source_mistake'))
            seen_ids.add(q.id)

    def add_from_list(questions, origin, limit=None):
        added = 0
        for q in questions:
            if limit is not None and added >= limit:
                break
            if q.id in seen_ids:
                continue
            candidates.append((q, origin))
            seen_ids.add(q.id)
            added += 1

    if mode == 'retry_mistakes':
        add_missed()

    elif mode == 'source_weak_areas':
        config = SmartPracticeConfig.load()
        topics = source_topic_mastery(ctx, config.weak_topic_accuracy_max_pct)
        weak_topic_ids = {t['topic_id'] for t in topics if t['is_weak']}
        add_missed(topic_filter=weak_topic_ids)
        if weak_topic_ids and len(candidates) < count:
            expansion = ctx.expansion_pool.filter(topic_id__in=weak_topic_ids).exclude(id__in=seen_ids)
            for q in expansion.order_by('?')[: max(count * 2, count - len(candidates))]:
                if q.id in seen_ids:
                    continue
                candidates.append((q, 'source_weak_topic'))
                seen_ids.add(q.id)

    elif mode == 'concept_reinforcement':
        config = SmartPracticeConfig.load()
        topics = source_topic_mastery(ctx, config.weak_topic_accuracy_max_pct)
        weak_chapter_ids = {t['chapter_id'] for t in topics if t['is_weak'] and t['chapter_id']}
        add_missed()
        if weak_chapter_ids and len(candidates) < count:
            expansion = ctx.expansion_pool.filter(chapter_id__in=weak_chapter_ids).exclude(id__in=seen_ids)
            for q in expansion.order_by('?')[: max(count * 2, count - len(candidates))]:
                if q.id in seen_ids:
                    continue
                candidates.append((q, 'expansion_pool'))
                seen_ids.add(q.id)

    elif mode == 'due_review':
        add_from_list(due_review_candidates(ctx, user), 'due_review')

    elif mode == 'new_questions':
        add_from_list(new_question_candidates(ctx, user), 'new_question')

    elif mode == 'bookmarked':
        add_from_list(bookmarked_candidates(ctx, user), 'bookmarked')

    elif mode == 'ai_mixed':
        # A deterministic, round-robin BLEND of the other source-scoped
        # strategies — no LLM call, matching the platform's own rule that
        # AI never performs selection, only explains an already-selected
        # result. "AI" here means "balanced across everything relevant",
        # not "model-generated".
        buckets = {
            'retry_mistakes': [q for q, _ in build_candidates(ctx, 'retry_mistakes', count, user=user)],
            'source_weak_areas': [q for q, _ in build_candidates(ctx, 'source_weak_areas', count, user=user)],
            'due_review': due_review_candidates(ctx, user),
            'new_questions': new_question_candidates(ctx, user),
        }
        origin_map = {
            'retry_mistakes': 'source_mistake', 'source_weak_areas': 'source_weak_topic',
            'due_review': 'due_review', 'new_questions': 'new_question',
        }
        round_robin = True
        while round_robin and len(candidates) < count:
            round_robin = False
            for key in MODES_FOR_AI_MIX:
                if len(candidates) >= count:
                    break
                bucket = buckets[key]
                while bucket:
                    q = bucket.pop(0)
                    if q.id not in seen_ids:
                        candidates.append((q, origin_map[key]))
                        seen_ids.add(q.id)
                        round_robin = True
                        break

    else:
        raise ValueError(f'Unknown mode: {mode}')

    # Relevance is not more important than count — if the authorized,
    # relevant pool is smaller than requested, return fewer questions
    # rather than padding with unrelated content (never pad with anything
    # outside `candidates` — everything already added is already
    # authorized+relevant, this is a trim, not a fill).
    return candidates[:count]


def _build_selection_reason(mode, candidates, ctx):
    n = len(candidates)
    if n == 0:
        return 'Not enough relevant questions are currently available in this practice context.'
    plural = 's' if n != 1 else ''
    if mode == 'retry_mistakes':
        return f'{n} question{plural} you missed in {ctx.test.title}.'
    if mode == 'source_weak_areas':
        return f'{n} question{plural} targeting the topics you struggled with in {ctx.test.title}.'
    if mode == 'concept_reinforcement':
        return f'{n} question{plural} to reinforce the concepts behind your mistakes in {ctx.test.title}.'
    if mode == 'due_review':
        return f'{n} question{plural} due for review from {ctx.test.title}.'
    if mode == 'new_questions':
        return f'{n} question{plural} from {ctx.test.title} you have not attempted yet.'
    if mode == 'bookmarked':
        return f'{n} bookmarked question{plural} from {ctx.test.title}.'
    return f'{n} question{plural} — a balanced mix of mistakes, weak areas, review, and new questions from {ctx.test.title}.'


def create_session(user, source_test_id, mode, question_count=None):
    config = SmartPracticeConfig.load()
    if not config.enabled:
        raise SourceScopeError('feature_disabled', 'Smart Practice is currently disabled.')

    ctx = resolve_source_scope(user, source_test_id)
    # Defense-in-depth: re-derive and re-assert independent of
    # resolve_source_scope's own check, so this function is safe even if
    # a future caller ever bypasses resolve_source_scope.
    if ctx.exam_type == 'grand':
        raise SourceScopeError('grand_test_excluded')

    if mode not in dict(SmartPracticeSession.MODE_CHOICES):
        raise ValueError(f'Unknown mode: {mode}')

    count = question_count or config.default_questions_per_session
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


def record_session_answer(user, session, question_id, option_id, time_taken_seconds=None):
    from django.shortcuts import get_object_or_404

    from academics.models import Option

    if session.user_id != user.id:
        raise SourceScopeError('not_authorized')
    if session.status != 'in_progress':
        raise SourceScopeError('session_not_in_progress')

    sq = get_object_or_404(SmartPracticeSessionQuestion, session=session, question_id=question_id)
    option = None
    is_correct = False
    if option_id:
        option = get_object_or_404(Option, pk=option_id, question_id=question_id)
        is_correct = option.is_correct

    sq.selected_option = option
    sq.is_correct = is_correct
    sq.time_taken_seconds = time_taken_seconds
    sq.answered_at = timezone.now()
    sq.save()

    # The single global mastery write path, untouched — Smart Practice is
    # just a new caller with source='smart', same as QBank ('qbank') and
    # formal Tests ('test').
    record_question_result(
        user, sq.question, is_correct, source='smart',
        selected_option=option, time_taken_seconds=time_taken_seconds,
    )
    return sq


def complete_session(user, session):
    if session.user_id != user.id:
        raise SourceScopeError('not_authorized')

    answered = session.questions.filter(is_correct__isnull=False)
    total_answered = answered.count()
    correct = answered.filter(is_correct=True).count()
    session.accuracy = round(correct / total_answered * 100, 2) if total_answered else 0
    session.score = correct
    session.status = 'completed'
    session.completed_at = timezone.now()
    session.save()
    return session
