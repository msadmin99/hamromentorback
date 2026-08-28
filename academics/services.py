"""Student-question performance write path — the single place that turns
"a student answered a question" (from QBank practice or from a submitted
test) into QuestionAttempt's running totals + an immutable QuestionEvent
log entry. Kept deliberately simple (no ML, no full spaced-repetition) per
the product brief: a fixed-interval revision schedule and threshold-based
mastery buckets, both tunable via QuestionBankConfig.
"""
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import Option, Question, QuestionAttempt, QuestionBankConfig, QuestionEvent


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


def _apply_question_stat_delta(question_id, total_delta, correct_delta, option_deltas):
    """Question.total_attempts/correct_attempts and Option.pick_count are
    live "one vote per distinct student" aggregates (not per-attempt —
    record_question_result's caller-facing delta math above already
    guarantees that), updated here via atomic F() expressions rather than
    read-modify-write on Python objects, since many students can answer
    the same question concurrently. select_for_update() on the Question
    row turns the increment + percentage-recompute below into one
    consistent critical section per question (not globally), so
    percentages can never be read mid-update or drift from pick_count."""
    Question.objects.select_for_update().get(pk=question_id)
    if total_delta or correct_delta:
        Question.objects.filter(pk=question_id).update(
            total_attempts=F('total_attempts') + total_delta,
            correct_attempts=F('correct_attempts') + correct_delta,
        )
    for option_id, delta in option_deltas.items():
        if delta:
            Option.objects.filter(pk=option_id).update(pick_count=F('pick_count') + delta)

    total_attempts = Question.objects.values_list('total_attempts', flat=True).get(pk=question_id)
    for option in Option.objects.filter(question_id=question_id).only('pk', 'pick_count', 'pick_percentage'):
        pct = round(option.pick_count / total_attempts * 100) if total_attempts else 0
        if option.pick_percentage != pct:
            Option.objects.filter(pk=option.pk).update(pick_percentage=pct)


@transaction.atomic
def record_question_result(user, question, is_correct, source, selected_option=None, time_taken_seconds=None, confidence=None):
    """The one write path for "a student answered this question" — called
    from QBank practice (QuestionViewSet.answer) and from final test
    submission (SubmitTestView, once per answered question, not per
    answer-change), so Weak/Mastered/Mistake Bank reflect the whole
    platform. Increments running totals (never overwrites them), recomputes
    mastery_status/revision_due_at, and appends an immutable QuestionEvent.
    Never touches is_bookmarked — that stays a separate, independent flag.
    `confidence` is QBank-practice-only (Test Mode never passes it, so a
    test submission never overwrites a student's last self-reported
    confidence with a blank).

    Also maintains Question.total_attempts/correct_attempts and
    Option.pick_count/pick_percentage — but as one vote per distinct
    student, not one per attempt: QuestionAttempt is already "latest wins"
    per (user, question), so a student changing their answer on a retry
    must move their one vote, not add another, or "X% of students got
    this right" would inflate every time someone retries until correct.
    """
    config = QuestionBankConfig.load()

    attempt, created = QuestionAttempt.objects.get_or_create(user=user, question=question)
    if not created:
        attempt = QuestionAttempt.objects.select_for_update().get(pk=attempt.pk)

    previous_option_id = attempt.selected_option_id
    previous_is_correct = attempt.is_correct

    attempt.attempts_count += 1
    if is_correct:
        attempt.correct_count += 1
    else:
        attempt.incorrect_count += 1
    attempt.is_correct = is_correct
    attempt.last_result = is_correct
    if selected_option is not None:
        attempt.selected_option = selected_option
    if confidence:
        attempt.confidence = confidence
    attempt.mastery_status = _compute_mastery_status(attempt.attempts_count, attempt.correct_count, config)

    interval_days = config.revision_interval_correct_days if is_correct else config.revision_interval_incorrect_days
    attempt.revision_due_at = timezone.now() + timezone.timedelta(days=interval_days)
    attempt.save()

    QuestionEvent.objects.create(
        user=user, question=question, is_correct=is_correct, source=source,
        time_taken_seconds=time_taken_seconds,
    )

    if selected_option is not None and selected_option.id != previous_option_id:
        option_deltas = {selected_option.id: 1}
        if previous_option_id:
            option_deltas[previous_option_id] = option_deltas.get(previous_option_id, 0) - 1
            total_delta = 0
            if previous_is_correct and not is_correct:
                correct_delta = -1
            elif not previous_is_correct and is_correct:
                correct_delta = 1
            else:
                correct_delta = 0
        else:
            total_delta = 1
            correct_delta = 1 if is_correct else 0
        _apply_question_stat_delta(question.id, total_delta, correct_delta, option_deltas)

    return attempt
