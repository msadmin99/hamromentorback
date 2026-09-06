"""Student-question performance write path — the single place that turns
"a student answered a question" (from QBank practice or from a submitted
test) into QuestionAttempt's running totals + an immutable QuestionEvent
log entry. Kept deliberately simple (no ML, no full spaced-repetition) per
the product brief: a fixed-interval revision schedule and threshold-based
mastery buckets, both tunable via QuestionBankConfig.
"""
import logging
import time

from django.db import IntegrityError, OperationalError, transaction
from django.db.models import F
from django.utils import timezone

from .models import Option, Question, QuestionAttempt, QuestionBankConfig, QuestionEvent

logger = logging.getLogger(__name__)

# MySQL errno for a detected deadlock / lock-wait timeout — the only errors
# apply_question_stats_deltas() retries. See its docstring: this is a
# safety net for the async stats worker, not the primary fix (the primary
# fix is applying deltas in a consistent, ascending question_id order so a
# wait-for cycle across concurrent submissions can't form in the first
# place — see academics/services.py docstring history / the scalability
# audit's deadlock investigation for the full root cause).
_RETRYABLE_MYSQL_ERRNOS = (1213, 1205)


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
    to_update = []
    for option in Option.objects.filter(question_id=question_id).only('pk', 'pick_count', 'pick_percentage'):
        pct = round(option.pick_count / total_attempts * 100) if total_attempts else 0
        if option.pick_percentage != pct:
            option.pick_percentage = pct
            to_update.append(option)
    # Scalability audit (Phase 2.1): a 4-option question used to issue up
    # to 4 separate UPDATE statements here — one per changed option. A
    # single-question change is negligible, but this function runs once
    # per answered question, so a 300-question exam submission used to
    # multiply this into up to 1200 individual UPDATEs. bulk_update()
    # collapses them into one CASE WHEN statement per question, same
    # computed values, same "only touch what actually changed" behavior.
    if to_update:
        Option.objects.bulk_update(to_update, ['pick_percentage'])


def record_question_result(
    user, question, is_correct, source, selected_option=None, time_taken_seconds=None, confidence=None,
    defer_stats=False, config=None, atomic=True,
):
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

    Scalability audit deadlock fix: this Question/Option aggregate-stat
    update (via _apply_question_stat_delta, NOT the QuestionAttempt/
    QuestionEvent writes above — those are per-(user, question) and were
    never the problem) is what deadlocked under concurrent exam
    submissions sharing a question pool. `defer_stats=True` (used only by
    SubmitTestView) skips applying it here and instead returns the
    computed delta so the caller can apply a whole submission's deltas
    later, in a consistent order, outside the request. QBank's caller
    (QuestionViewSet.answer) never passes this — its behavior, return
    value, and locking are completely unchanged, since a single QBank
    answer was never the concurrency profile causing deadlocks.

    Scalability audit Phase 1: `config`, when passed, is used instead of a
    fresh QuestionBankConfig.load() — this singleton row never changes
    within one request, but this function runs once per answered question,
    so a 300-question submission was issuing 300 identical, redundant
    SELECTs for the same row. Every caller that omits it (QBank, Smart
    Practice, tests) gets the exact original per-call load() behavior —
    this is purely additive. The caller decides the memoization lifetime
    (SubmitTestView loads it once per request, not once globally), so
    there is no cross-request staleness: a config change saved by an admin
    mid-request is picked up by the very next request regardless.

    Scalability audit Phase 2: `atomic` (default True) controls whether
    this call opens its own transaction.atomic() — for QBank's answer()
    and Smart Practice's record_session_answer(), this is their ONLY
    transaction boundary (neither view has an outer atomic block), so it
    must stay on, exactly as before. SubmitTestView is the one exception:
    it already runs every answer inside its own outer `with transaction.
    atomic():`, and it never catches an exception from this call — any
    failure propagates straight to that outer block, which rolls back the
    whole submission regardless of whether an inner savepoint existed. So
    for SubmitTestView specifically, this function's own atomic wrapping
    was a redundant SAVEPOINT/RELEASE SAVEPOINT pair on every single
    answered question with no effect on the final DB state — SubmitTestView
    passes atomic=False to skip it. Every other/future caller that omits
    the argument gets the exact original fully-wrapped behavior."""
    if atomic:
        with transaction.atomic():
            return _record_question_result(
                user, question, is_correct, source, selected_option, time_taken_seconds, confidence,
                defer_stats, config,
            )
    return _record_question_result(
        user, question, is_correct, source, selected_option, time_taken_seconds, confidence,
        defer_stats, config,
    )


def _record_question_result(
    user, question, is_correct, source, selected_option, time_taken_seconds, confidence, defer_stats, config,
):
    """Actual write logic for record_question_result() — see that function's
    docstring for the full contract. Split out so record_question_result()
    can conditionally wrap this in transaction.atomic() (see its `atomic`
    param) without duplicating the body."""
    if config is None:
        config = QuestionBankConfig.load()

    # Scalability audit Phase 3: get_or_create() itself never locks anything
    # (both its internal reads are plain SELECTs), so the repeat-encounter
    # case — the common one — used to pay for an unlocked SELECT via
    # get_or_create() and then throw the result away in favor of a second,
    # locked select_for_update().get(pk=...). Locking the FIRST read instead
    # collapses that to one query for the row-already-exists case, while
    # keeping the exact same race-safety contract get_or_create() has always
    # provided for a genuinely new (user, question) pair: select_for_update()
    # on a non-matching row costs the same as a plain SELECT (nothing to
    # lock), so the first-encounter path is unchanged at 2 queries, and a
    # concurrent create race is still caught by the unique_together
    # constraint and recovered via the same locked re-fetch get_or_create()
    # itself falls back to internally.
    attempt = QuestionAttempt.objects.select_for_update().filter(user=user, question=question).first()
    created = False
    if attempt is None:
        try:
            with transaction.atomic():
                attempt = QuestionAttempt.objects.create(user=user, question=question)
            created = True
        except IntegrityError:
            attempt = QuestionAttempt.objects.select_for_update().get(user=user, question=question)

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

    delta = None
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
        if defer_stats:
            delta = {
                'question_id': question.id, 'total_delta': total_delta, 'correct_delta': correct_delta,
                'option_deltas': option_deltas,
            }
        else:
            _apply_question_stat_delta(question.id, total_delta, correct_delta, option_deltas)

    if defer_stats:
        return attempt, delta
    return attempt


def apply_question_stats_deltas(deltas):
    """Applies a batch of deltas (as returned by record_question_result(...,
    defer_stats=True)) — called by the async stats worker (tests_app.
    stats_tasks.process_question_stats), never inline during a student's
    submission request.

    Deltas are applied in ascending question_id order, every time,
    regardless of the order questions were answered/submitted in — this
    is the actual deadlock fix: the original code locked Question rows in
    whatever order a student's (randomly shuffled) answers happened to
    come back in, so two concurrent students sharing a question pool
    could lock the same two questions in opposite orders and deadlock.
    A single, consistent lock order across every caller makes that
    wait-for cycle impossible to form.

    Each question's delta is applied in its own short transaction (not
    one transaction for the whole batch) — this keeps a lock held only
    for that one question's own critical section, and means a deadlock/
    lock-timeout on one question doesn't force retrying every other
    question in the same submission. A transient deadlock (MySQL errno
    1213) or lock-wait timeout (1205) on a single question's apply is
    retried a few times with a short backoff — a safety net for residual
    contention (e.g. a QBank answer landing on the same question at the
    same instant), not the primary fix; the primary fix is the ordering
    above."""
    for delta in sorted(deltas, key=lambda d: d['question_id']):
        _apply_one_delta_with_retry(delta)


def _apply_one_delta_with_retry(delta, max_attempts=3):
    option_deltas = {int(option_id): amount for option_id, amount in delta['option_deltas'].items()}
    for attempt_no in range(max_attempts):
        try:
            with transaction.atomic():
                _apply_question_stat_delta(delta['question_id'], delta['total_delta'], delta['correct_delta'], option_deltas)
            return
        except OperationalError as exc:
            errno = exc.args[0] if exc.args else None
            is_last_attempt = attempt_no == max_attempts - 1
            if errno not in _RETRYABLE_MYSQL_ERRNOS or is_last_attempt:
                raise
            logger.warning(
                'apply_question_stats_deltas: retryable DB error (errno %s) applying stats for question %s, '
                'attempt %s/%s', errno, delta['question_id'], attempt_no + 1, max_attempts,
            )
            time.sleep(0.1 * (2 ** attempt_no))
