"""Pure, source-scoped aggregation functions — mirrors the design
philosophy of tests_app/performance.py ("pure aggregation... no new
models"), but scoped to one SourceContext's single resolved attempt
instead of a student's whole platform history. A source test has at most
a few hundred questions, so recomputing this per-request is cheap — no
persisted table, same reasoning tests_app/performance.py already
documents for its own platform-wide (much larger) aggregations."""
from collections import defaultdict

from django.db.models import Q

from tests_app.models import Answer


def source_missed_questions(ctx):
    """Wrong OR skipped answers from the source attempt — Mode A (Retry
    Mistakes) candidate set."""
    return (
        Answer.objects.filter(attempt=ctx.attempt)
        .filter(Q(selected_option__isnull=True) | Q(is_correct=False))
        .select_related('question', 'question__subject', 'question__chapter', 'question__topic')
    )


def source_topic_mastery(ctx, weak_max_pct):
    """Per-topic accuracy within the source attempt only — accuracy is
    computed only over answers with a real selected_option (mirrors
    SubmitTestView.post's own `if answer.selected_option_id` guard), so a
    skipped question doesn't silently count as 'wrong' and drag a topic's
    accuracy down. Topics with zero answered questions in this attempt
    are omitted (there's nothing to be 'weak' or 'strong' in yet)."""
    answers = (
        Answer.objects.filter(attempt=ctx.attempt, selected_option__isnull=False)
        .select_related('question')
    )

    per_topic = defaultdict(lambda: {'attempted': 0, 'correct': 0, 'subject_id': None, 'chapter_id': None})
    topic_names = {}
    for answer in answers:
        q = answer.question
        if not q.topic_id:
            continue
        bucket = per_topic[q.topic_id]
        bucket['attempted'] += 1
        bucket['subject_id'] = q.subject_id
        bucket['chapter_id'] = q.chapter_id
        if answer.is_correct:
            bucket['correct'] += 1
        if q.topic_id not in topic_names and q.topic:
            topic_names[q.topic_id] = q.topic.name

    result = []
    for topic_id, agg in per_topic.items():
        accuracy = round(agg['correct'] / agg['attempted'] * 100, 2) if agg['attempted'] else 0.0
        result.append({
            'topic_id': topic_id,
            'topic_name': topic_names.get(topic_id, ''),
            'subject_id': agg['subject_id'],
            'chapter_id': agg['chapter_id'],
            'attempted': agg['attempted'],
            'correct': agg['correct'],
            'accuracy': accuracy,
            'is_weak': accuracy <= weak_max_pct,
        })
    return sorted(result, key=lambda r: r['accuracy'])
