"""Student-question performance write path — the single place that turns
"a student answered a question" (from QBank practice or from a submitted
test) into QuestionAttempt's running totals + an immutable QuestionEvent
log entry. Kept deliberately simple (no ML, no full spaced-repetition) per
the product brief: a fixed-interval revision schedule and threshold-based
mastery buckets, both tunable via QuestionBankConfig.
"""
from django.db import transaction
from django.utils import timezone

from .models import QuestionAttempt, QuestionBankConfig, QuestionEvent


def _compute_mastery_status(attempts_count, correct_count, config):
    if attempts_count == 0:
        return 'new'
    accuracy = correct_count / attempts_count * 100
    if attempts_count >= 2 and accuracy >= config.mastered_min_pct:
        return 'mastered'
    if accuracy <= config.weak_max_pct:
        return 'weak'
    if attempts_count == 1:
        return 'learning'
    return 'need_practice'


@transaction.atomic
def record_question_result(user, question, is_correct, source, selected_option=None):
    """The one write path for "a student answered this question" — called
    from QBank practice (QuestionViewSet.answer) and from final test
    submission (SubmitTestView, once per answered question, not per
    answer-change), so Weak/Mastered/Mistake Bank reflect the whole
    platform. Increments running totals (never overwrites them), recomputes
    mastery_status/revision_due_at, and appends an immutable QuestionEvent.
    Never touches is_bookmarked — that stays a separate, independent flag.
    """
    config = QuestionBankConfig.load()

    attempt, created = QuestionAttempt.objects.get_or_create(user=user, question=question)
    if not created:
        attempt = QuestionAttempt.objects.select_for_update().get(pk=attempt.pk)

    attempt.attempts_count += 1
    if is_correct:
        attempt.correct_count += 1
    else:
        attempt.incorrect_count += 1
    attempt.is_correct = is_correct
    attempt.last_result = is_correct
    if selected_option is not None:
        attempt.selected_option = selected_option
    attempt.mastery_status = _compute_mastery_status(attempt.attempts_count, attempt.correct_count, config)

    interval_days = config.revision_interval_correct_days if is_correct else config.revision_interval_incorrect_days
    attempt.revision_due_at = timezone.now() + timezone.timedelta(days=interval_days)
    attempt.save()

    QuestionEvent.objects.create(user=user, question=question, is_correct=is_correct, source=source)
    return attempt
