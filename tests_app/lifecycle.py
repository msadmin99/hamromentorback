"""Phase 6 — Exam Session, Scheduling & Attempt Lifecycle.

The server-authoritative source for "when does this attempt actually end,"
and the single place an in-progress attempt is ever scored/ranked/marked
submitted — whether that happens because the student clicked Submit or
because the server discovered, on some later request, that time had
already run out.

Deliberately NOT a new TestAttempt status enum (see PHASE_6_ARCHITECTURE.md
for the full reasoning). An auto-submitted attempt is `status='submitted'`
exactly like a manually-submitted one — same scoring, same ranking pool,
same review/result behavior, zero new branching needed anywhere that
already filters on `status='submitted'` — distinguished only by the new
`auto_submitted` boolean, an informational marker, not a lifecycle state.

Two ways this module's `finalize_attempt()` gets reached, matching the
"request-time checks AND background jobs complement each other, neither
alone is sufficient" requirement:

  1. Request-time (primary, always-correct): every view that touches an
     in-progress attempt (SubmitAnswerView, MarkForReviewView,
     AttemptDetailView, TestResultView, SubmitTestView) calls
     `ensure_finalized_if_expired()` first. A student (or admin) who ever
     revisits an expired attempt gets it finalized right there — the same
     lazy-reactive pattern this codebase already uses for
     ExamSession.refresh_status().
  2. Background sweep (secondary, best-effort): `manage.py
     finalize_expired_attempts` finds and finalizes every expired
     in-progress attempt platform-wide, for the case where literally no
     one ever revisits it. NOT relied on for correctness — every request
     path is already correct without it (see its own module docstring).

`finalize_attempt()` is race-safe (`select_for_update()` + a post-lock
status re-check) and idempotent — calling it on an already-submitted
attempt is a safe no-op, so overlapping requests, a request racing the
background sweep, or the sweep running twice can never double-score.

Phase 8 addendum: `finalize_attempt()` also captures an immutable
`AttemptQuestionSnapshot` per question, in the same transaction as
scoring, via `_create_question_snapshots()` — see
docs/QUESTION_VERSIONING_DESIGN.md and docs/PHASE_8_ARCHITECTURE.md for
why (in short: so a later edit to the live Question/Option can never
change what an already-finalized attempt's review page shows).
"""
import logging

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from academics.models import QuestionBankConfig
from academics.services import record_question_result

from .models import AttemptQuestion, Test, TestAttempt
from .stats_tasks import enqueue_question_stats_task

logger = logging.getLogger(__name__)


def compute_effective_session_status(session):
    """The exact transition rule ExamSession.refresh_status() applies,
    computed WITHOUT writing — the single place this rule lives (that
    model method now delegates here) so a read path (listing sessions) and
    a write path (refresh_status(), called from the start action) can
    never independently drift on what 'live'/'completed' means. Never
    moves a session out of 'cancelled'/'draft'/'completed' — those are
    either explicit admin actions or an already-correct terminal state."""
    if session.status in ('cancelled', 'draft', 'completed'):
        return session.status
    now = timezone.now()
    if now > session.end_datetime:
        return 'completed'
    if now >= session.start_datetime:
        return 'live'
    if session.registration_deadline and now >= session.registration_deadline:
        return 'scheduled'
    return 'registration_open' if session.registration_deadline else 'scheduled'


def effective_attempt_end(attempt):
    """MIN(attempt_start + test.duration_minutes, session.end_datetime) —
    the exact rule the Phase 6 spec requires, generalized to apply even
    with no session: every Test has a duration_minutes (already the
    number the frontend countdown timer has always displayed), so every
    attempt has at least a personal deadline; a session, when one exists,
    can only ever shorten it, never extend it.

    GT3-2: a Grand Test scheduled via the simpler Test.scheduled_end field
    (no real ExamSession — see grand_test_participation_status's own
    docstring) gets the identical ceiling a session-scheduled Grand Test
    already had, closing the 'late start extends the closing time' gap
    for that path (a late-starting student's personal duration_minutes
    could previously run well past the exam's own advertised end time,
    since scheduled_start/end were display-only before this).

    Daily Test schedule audit: extended to exam_type='daily' too, for the
    identical reason — a Daily Test's 24-hour scheduled_start/end window
    must cap a late-starting attempt exactly like Grand Test's does,
    per that feature's own requirement ("if the attempt would extend
    beyond scheduled_end, cap the attempt at scheduled_end"). A Mock/PYQ
    test's scheduled_start/end (used only for TestListSerializer.
    get_status's display badge) remains completely unaffected — this
    still does not newly enforce anything for those exam types."""
    from datetime import timedelta

    personal_end = attempt.start_time + timedelta(minutes=attempt.test.duration_minutes)
    if attempt.session_id and attempt.session.end_datetime:
        return min(personal_end, attempt.session.end_datetime)
    if attempt.test.exam_type in ('grand', 'daily') and attempt.test.scheduled_end:
        return min(personal_end, attempt.test.scheduled_end)
    return personal_end


def is_attempt_expired(attempt, now=None):
    """Pure, read-only — safe to call from a capability function (no DB
    write). True only for an attempt still nominally 'in_progress' whose
    effective deadline has passed; a already-submitted attempt is never
    'expired' (it's just submitted)."""
    if attempt.status != 'in_progress':
        return False
    now = now or timezone.now()
    return now >= effective_attempt_end(attempt)


def resolve_test_schedule_session(test):
    """Grand Test 3.0 / GT3-2 — the one, canonical way to find 'the'
    ExamSession that governs a Test's scheduled window, reused by both
    _start_attempt() (enforcement) and grand_test_participation_status()
    (read-only reporting) so the two can never independently disagree
    about which session is authoritative.

    A Test can in principle have more than one ExamSession (a reschedule
    creates a new one rather than mutating the old — see ExamSession's
    own docstring); this picks the most recently *starting* non-cancelled
    one. For Grand Test 3.0's actual usage pattern (one Test = one
    scheduled Grand Test = one session) there is only ever one candidate,
    so this ambiguity is a documented edge case, not the common path —
    see this feature's own limitations note.

    Returns None for a Test with no real schedule at all (the ordinary,
    unscheduled Daily/Mock/PYQ/legacy-Grand-Test case) — such a Test has
    no 'ended' concept and can therefore never be MISSED."""
    return test.sessions.exclude(status='cancelled').order_by('-start_datetime').first()


def grand_test_participation_status(test, user, session=None):
    """Grand Test 3.0 / GT3-2 — the derived (never stored) answer to
    'where does this student stand on this scheduled Grand Test right
    now.' One of:

        not_scheduled  — this Test has no real schedule at all, neither
                         an ExamSession nor Test.scheduled_start/end (an
                         ordinary, unscheduled Test — MISSED cannot apply;
                         nothing here changes for it)
        upcoming       — the schedule hasn't opened yet
        live           — schedule is open, student has no attempt yet
        in_progress    — student has an attempt, still 'in_progress'
        completed      — student has an attempt, 'submitted'
                         (auto_submitted or manual — both are equally
                         'completed' from a participation standpoint;
                         attempt.auto_submitted still distinguishes them)
        missed         — schedule has closed, student never started one

    TWO schedule sources, checked in this order, matching exactly what
    _start_attempt() enforces (see that function — the two must never
    diverge):
      1. A real ExamSession (resolve_test_schedule_session) — the richer,
         status-lifecycle-aware mechanism (draft/scheduled/live/completed/
         cancelled), used when a Test has actually been run through
         Reschedule / Exam Sessions.
      2. Test.scheduled_start/scheduled_end directly — the simpler pair
         of fields an admin can set on the exam builder without ever
         touching Exam Sessions at all. Confirmed (exam_versioning.py's
         own reschedule-seed logic) to be the more basic, standalone
         mechanism, not merely a display-only legacy leftover — GT3-2
         makes it load-bearing for the first time, alongside the richer
         session path, rather than replacing it.

    Deliberately entitlement-agnostic: whether the user actually HOLDS a
    valid billing.GrandTestAccess for this Test is a separate, existing
    question (billing.access.get_grand_test_access) — a caller combines
    both ('not entitled' is reported independently, never folded into
    this function's vocabulary, so the two concerns stay testable in
    isolation exactly as GT3.0's own architecture principle requires).

    No TestAttempt row is ever created, read-modified, or implied by
    calling this — a 'missed' result is derived purely from the absence
    of one, never represented by one."""
    if session is None:
        session = resolve_test_schedule_session(test)

    if session is not None:
        attempt = TestAttempt.objects.filter(test=test, session=session, user=user).only('status').first()
        if attempt is not None:
            return 'in_progress' if attempt.status == 'in_progress' else 'completed'
        effective = compute_effective_session_status(session)
        if effective in ('scheduled', 'registration_open'):
            return 'upcoming'
        if effective == 'live':
            return 'live'
        if effective == 'completed':
            return 'missed'
        return 'not_scheduled'  # draft (cancelled already excluded by resolve_test_schedule_session)

    # No real ExamSession exists — fall back to the Test's own
    # scheduled_start/scheduled_end (see docstring above).
    start, end = test.scheduled_start, test.scheduled_end
    if start is None or end is None:
        return 'not_scheduled'

    attempt = TestAttempt.objects.filter(test=test, session__isnull=True, user=user).only('status').first()
    if attempt is not None:
        return 'in_progress' if attempt.status == 'in_progress' else 'completed'

    now = timezone.now()
    if now < start:
        return 'upcoming'
    if now <= end:
        return 'live'
    return 'missed'


def grand_test_review_window(test, session=None):
    """Grand Test 3.0 / GT3-4 — (solutions_available_at, review_expires_at)
    for one Grand Test, the single shared derivation every review/solution
    authorization check reuses so none of them can independently disagree
    about when a review window opens or closes (the exact discipline
    grand_test_participation_status/effective_attempt_end already
    established for MISSED and the attempt deadline).

    solutions_available_at is exactly the moment this Test's own governing
    schedule closes — a real ExamSession's end_datetime if one exists,
    else Test.scheduled_end (the same two-source resolution
    grand_test_participation_status uses). review_expires_at is that same
    moment plus Test.review_duration_days, or None for a permanent review
    (the field's own null default — see its help_text).

    Returns (None, None) for a Test with no real schedule at all — nothing
    has 'closed' yet, so there is no window to compute."""
    if session is None:
        session = resolve_test_schedule_session(test)

    available_at = session.end_datetime if session is not None else test.scheduled_end
    if available_at is None:
        return None, None
    if test.review_duration_days is None:
        return available_at, None
    from datetime import timedelta

    return available_at, available_at + timedelta(days=test.review_duration_days)


def freeze_attempt_questions(attempt):
    """Phase 9 — called exactly once, immediately after TestAttempt.objects
    .create() in tests_app.views._start_attempt(), never again for that
    attempt. Applies Test.shuffle_questions (if set) and the free-preview
    slice (if the attempt started in preview-only mode) exactly once, then
    persists the result as AttemptQuestion rows — the single source of
    truth every later GET/answer/submit reads from instead of
    recomputing.

    This is the actual fix for the proven Daily Test incident: previously
    TestAttemptSerializer.get_questions() called random.shuffle() (and,
    for a preview-only student, re-sliced a freshly-reshuffled list) on
    every single request, so the same in-progress attempt could show a
    different question set/order on every reload.

    Does not consume any entitlement itself and does not gate access —
    _start_attempt() has already fully decided, by the time this runs,
    that the attempt is allowed to exist; this only fixes WHICH of the
    test's questions belong to it and in WHAT ORDER, once."""
    from billing.access import is_preview_only

    test = attempt.test
    qs = list(test.questions.all())
    if test.shuffle_questions:
        import random

        random.shuffle(qs)

    preview = is_preview_only(attempt.user, test)
    if preview:
        qs = qs[:test.free_preview_questions]

    AttemptQuestion.objects.bulk_create([
        AttemptQuestion(
            attempt=attempt, question=q, order=i, is_preview=preview,
            marks_snapshot=q.marks, negative_marks_snapshot=q.negative_marks,
        )
        for i, q in enumerate(qs)
    ])


def attempt_is_preview_only(attempt):
    """The authoritative, immutable-once-started answer to "is this
    attempt preview-only" — the frozen state from freeze_attempt_questions()
    if this attempt has one (every attempt created after this feature
    shipped), falling back to a live billing.access.is_preview_only()
    check only for an attempt that predates it (no AttemptQuestion rows
    exist at all — see freeze_attempt_questions' own docstring for why
    that population is never backfilled).

    Deliberately does NOT re-derive from the student's CURRENT
    entitlement state once an attempt is in progress: an exam attempt
    behaves like an immutable session (per this feature's own design
    principle) — a student who subscribes mid-attempt gets full access on
    their NEXT attempt, not a retroactively-expanded version of one
    already running privately-scoped to a smaller, already-shown question
    set. This changes nothing about entitlement rules themselves
    (billing.access.is_preview_only/has_daily_test_access are untouched)
    — only WHEN that decision gets locked in for a given attempt."""
    frozen = list(attempt.attempt_questions.values_list('is_preview', flat=True)[:1])
    if frozen:
        return bool(frozen[0])
    from billing.access import is_preview_only

    return is_preview_only(attempt.user, attempt.test)


def attempt_preview_question_ids(attempt):
    """The exact set of question IDs that belong to this attempt's frozen
    preview subset — read by SubmitAnswerView so a question the student
    was actually shown as preview can never be rejected as "not preview"
    by a separately, differently re-derived allowlist (the second half of
    the proven incident: get_questions() and SubmitAnswerView used to
    compute this independently, with get_questions() re-shuffling before
    slicing and SubmitAnswerView never shuffling at all, so the two could
    legitimately disagree on which questions counted as preview).

    Only meaningful for a preview-only attempt (see attempt_is_preview_only)
    — callers must check that first, matching the existing call-site
    pattern. Falls back to the exact pre-fix live-derived slice only for
    an attempt that predates AttemptQuestion."""
    frozen_ids = list(attempt.attempt_questions.values_list('question_id', flat=True))
    if frozen_ids:
        return {qid for qid in frozen_ids if qid is not None}
    return set(
        attempt.test.questions.all()[:attempt.test.free_preview_questions].values_list('id', flat=True)
    )


def _score_and_rank(attempt):
    """The exact scoring/ranking logic SubmitTestView used to run inline —
    extracted, unchanged in behavior, so a manual submit and a
    server-triggered auto-submit are byte-for-byte the same code path.
    Caller holds the row lock and the outer transaction; this only
    mutates `attempt` in memory and returns the stat deltas to enqueue.

    GT3-1 (Grand Test 3.0, Historical & Scoring Integrity):

    1. Marks/negative-marks are read from this attempt's own frozen
       AttemptQuestion.marks_snapshot/negative_marks_snapshot first,
       falling back to the live Question.marks/negative_marks only when
       the snapshot is null (a pre-GT3-1 attempt, or the pre-Phase-9
       legacy case of zero AttemptQuestion rows) — see AttemptQuestion's
       own docstring for why this matters specifically for a Grand Test.
    2. `accuracy` is now correct/(correct+incorrect) — i.e. over
       ATTEMPTED questions only. The previous `attempt.answers.count()`
       denominator over-counted: an Answer row is created not just by
       actually selecting an option but also by MarkForReviewView
       (marking a question for review with no option ever chosen still
       creates/touches an Answer row with selected_option=None) — so a
       student who marked several questions for review without
       answering them previously had their accuracy silently understated
       against a denominator that included those never-answered rows.
       Score itself was never affected (this loop already only credited
       marks when selected_option_id was actually set) — only the
       separately-displayed accuracy percentage was wrong."""
    score = 0.0
    correct = 0
    incorrect = 0
    stat_deltas = []
    config = QuestionBankConfig.load()
    marks_by_qid = {
        aq.question_id: aq for aq in attempt.attempt_questions.all() if aq.question_id is not None
    }
    for answer in attempt.answers.select_related('question', 'selected_option'):
        q = answer.question
        if answer.selected_option_id:
            frozen = marks_by_qid.get(q.id)
            marks = frozen.marks_snapshot if frozen and frozen.marks_snapshot is not None else q.marks
            negative_marks = (
                frozen.negative_marks_snapshot if frozen and frozen.negative_marks_snapshot is not None
                else q.negative_marks
            )
            if answer.is_correct:
                score += float(marks)
                correct += 1
            else:
                incorrect += 1
                if attempt.test.negative_marking:
                    score -= float(negative_marks)
            _, delta = record_question_result(
                attempt.user, q, answer.is_correct, source='test',
                selected_option=answer.selected_option, time_taken_seconds=answer.time_taken_seconds,
                defer_stats=True, config=config, atomic=False,
            )
            if delta:
                stat_deltas.append(delta)

    attempted = correct + incorrect
    attempt.score = round(score, 2)
    attempt.accuracy = round((correct / attempted) * 100, 2) if attempted else 0
    attempt.end_time = timezone.now()
    attempt.status = 'submitted'

    # GT3-7 §46 — ranking concurrency: reading ranking_pool and computing
    # this attempt's rank used to happen with no lock beyond the caller's
    # own TestAttempt row lock (finalize_attempt's select_for_update on
    # THIS attempt only) — so two different students finishing at
    # genuinely the same instant (a real scenario near a Grand Test's
    # 11:00 deadline, GT3-7 §41 Scenario C) could each read the ranking
    # pool before the other's row had committed as 'submitted', and each
    # compute a rank/percentile that doesn't account for the other —
    # e.g. two attempts with different scores could both compute
    # ahead=0 and both land on rank=1, even though one genuinely
    # outscored the other. Locking this Test row (already exists, one
    # per exam, so this only ever serializes OTHER students finishing
    # the SAME exam — never a global lock across unrelated exams)
    # for this short aggregate-then-write section makes rank assignment
    # for one exam strictly sequential, closing that race. Deadlock-safe:
    # the only caller (finalize_attempt) already holds this attempt's own
    # row lock first and never acquires a Test lock anywhere else in a
    # different order.
    Test.objects.select_for_update().get(pk=attempt.test_id)

    ranking_pool = TestAttempt.objects.filter(test=attempt.test, session=attempt.session, status='submitted')
    agg = ranking_pool.aggregate(total=Count('id'), ahead=Count('id', filter=Q(score__gt=attempt.score)))
    total = agg['total'] + 1  # +1: attempt itself hasn't been saved as 'submitted' yet, so isn't in ranking_pool
    ahead = agg['ahead']
    attempt.rank = ahead + 1
    attempt.percentile = round((total - attempt.rank) / total * 100, 2) if total > 1 else 100

    return stat_deltas


def _create_question_snapshots(attempt):
    """Phase 8 — one AttemptQuestionSnapshot per question in this attempt's
    Test, capturing exactly what each question/option looked like right
    now (finalization time) — text, options, correctness, explanation, and
    which option (by original id, not a live FK) this student picked. See
    docs/QUESTION_VERSIONING_DESIGN.md for the full design.

    Phase 9 fix: for an attempt with frozen AttemptQuestion rows (every
    attempt created after that feature shipped), THOSE rows — not the
    live, mutable TestQuestion set — are the authoritative source of which
    questions belong to this attempt and in what order. Without this, an
    admin editing a Test's question list (TestAdminSerializer.update()'s
    ordinary delete/recreate of TestQuestion rows — a normal, unguarded
    admin action, not a bug) between attempt-start and finalization could
    silently drop an already-answered, correctly-scored question from the
    student's own review page, even though nothing about their answer or
    score was actually lost (proven by direct reproduction; fixed here
    together with a regression test in tests_attempt_question_integrity.py).
    A frozen row whose `question` has since gone null (the live Question
    itself was force-deleted — a separate, pre-existing, already-guarded
    path, see AttemptQuestion's own docstring) has no live content left to
    snapshot and is skipped, matching get_questions()'s identical
    precedent for the same case.

    Legacy attempts with zero AttemptQuestion rows (created before this
    feature existed) fall back to the exact pre-Phase-9 behavior: the live
    TestQuestion set, ordered by TestQuestion.order — unchanged.

    GT3-1 fix: marks/negative_marks recorded on this permanent snapshot
    now come from the SAME frozen AttemptQuestion.marks_snapshot/
    negative_marks_snapshot _score_and_rank() actually scored against
    (falling back to live Question.marks/negative_marks only when the
    snapshot is null) — previously this read live Question.marks
    independently, at finalize time. For a quick quiz the two reads
    happen close enough together that this never showed a visible
    difference, but for a Grand Test — one scheduled window many
    students finalize across, minutes to hours apart — an admin editing
    a question's marks mid-window used to mean the review page's
    displayed marks and the score actually computed for a LATER-finishing
    student's identical attempt could disagree, even though each
    individual attempt's own snapshot was internally self-consistent.

    Caller holds the row lock and the outer transaction. Runs exactly once
    per attempt by construction — finalize_attempt()'s own status re-check
    means this function is only ever reached the one time an attempt
    actually transitions out of 'in_progress', never on the idempotent
    no-op path for an already-submitted attempt — so an attempt's existing
    snapshot rows, once created, are never touched by this function again;
    this change is purely forward-looking for future finalizations."""
    from media_library.serializers import resolve_image_data

    from .models import AttemptQuestionSnapshot, TestQuestion

    select_fields = (
        'question', 'question__subject', 'question__reference_book',
        'question__image_asset', 'question__explanation_image_asset',
    )
    frozen = list(
        attempt.attempt_questions.select_related(*select_fields)
        .prefetch_related('question__options').order_by('order')
    )
    if frozen:
        questions_in_order = [aq.question for aq in frozen if aq.question_id and aq.question is not None]
        # GT3-1: same frozen-marks lookup _score_and_rank() uses, so this
        # permanent review snapshot always agrees with the score that was
        # actually computed for THIS attempt — never the live value some
        # other, later-finalizing attempt of the same Grand Test might see.
        marks_by_qid = {aq.question_id: aq for aq in frozen if aq.question_id is not None}
    else:
        questions_in_order = [
            tq.question for tq in
            TestQuestion.objects.filter(test=attempt.test)
            .select_related(*select_fields).prefetch_related('question__options').order_by('order')
        ]
        marks_by_qid = {}

    answers_by_question = {a.question_id: a for a in attempt.answers.all()}

    snapshots = []
    for i, q in enumerate(questions_in_order):
        answer = answers_by_question.get(q.id)
        options_snapshot = [
            {
                'id': opt.id, 'text': opt.text, 'latex': opt.latex,
                'image_data': resolve_image_data(opt.image_asset, opt.image),
                'is_correct': opt.is_correct, 'explanation': opt.explanation, 'order': opt.order,
            }
            for opt in q.options.all()
        ]
        frozen_marks = marks_by_qid.get(q.id)
        marks = frozen_marks.marks_snapshot if frozen_marks and frozen_marks.marks_snapshot is not None else q.marks
        negative_marks = (
            frozen_marks.negative_marks_snapshot
            if frozen_marks and frozen_marks.negative_marks_snapshot is not None else q.negative_marks
        )
        snapshots.append(AttemptQuestionSnapshot(
            attempt=attempt, question=q, order=i,
            text=q.text, latex=q.latex, image_data=resolve_image_data(q.image_asset, q.image),
            explanation=q.explanation, explanation_latex=q.explanation_latex,
            explanation_image_data=resolve_image_data(q.explanation_image_asset, q.explanation_image),
            explanation_video_url=q.explanation_video_url, key_takeaway=q.key_takeaway,
            references=q.references, reference_book_name=q.reference_book.name if q.reference_book_id else '',
            reference_edition=q.reference_edition, reference_chapter=q.reference_chapter,
            reference_page=q.reference_page, reference_url=q.reference_url,
            subject_name=q.subject.name if q.subject_id else '',
            marks=marks, negative_marks=negative_marks,
            options_snapshot=options_snapshot,
            selected_option_original_id=answer.selected_option_id if answer else None,
        ))
    if snapshots:
        AttemptQuestionSnapshot.objects.bulk_create(snapshots)


def _enqueue_stats_safely(attempt_id, deltas):
    """Mirrors tests_app.views._enqueue_stats_safely exactly (same
    reasoning: a failed enqueue must never turn an already-committed
    finalization into a 500/failure for anyone)."""
    try:
        enqueue_question_stats_task(attempt_id, deltas)
    except Exception:  # noqa: BLE001 - never let a finalization that already succeeded surface as an error
        logger.exception('_enqueue_stats_safely: failed to enqueue stats task for attempt %s', attempt_id)


def finalize_attempt(attempt, auto_submitted=False):
    """Race-safe, idempotent finalization — the ONLY place a TestAttempt is
    ever scored, ranked, and marked 'submitted'. Re-fetches and locks the
    row; if it's already left 'in_progress' (finalized by a concurrent
    request, the background sweep, or the student's own manual submit
    racing this call), returns that already-final row unchanged rather
    than re-scoring. Always returns a TestAttempt with status='submitted'
    (or whatever terminal status a future caller finds it already in)."""
    with transaction.atomic():
        locked = TestAttempt.objects.select_for_update().get(pk=attempt.pk)
        if locked.status != 'in_progress':
            return locked  # idempotent no-op — already finalized elsewhere
        stat_deltas = _score_and_rank(locked)
        locked.auto_submitted = auto_submitted
        locked.save()
        # Phase 8: same transaction as scoring — either both the score and
        # the historical snapshot commit together, or neither does. Never
        # skipped for auto-submitted attempts (see docs/PHASE_8_
        # ARCHITECTURE.md's Phase 6 integration note).
        _create_question_snapshots(locked)

    if stat_deltas:
        transaction.on_commit(lambda: _enqueue_stats_safely(locked.id, stat_deltas))
    return locked


def ensure_finalized_if_expired(attempt):
    """The request-time backstop — call at the top of any view that reads
    or mutates an attempt a student owns. A no-op (one cheap in-memory
    check, no query) unless the attempt is actually expired, in which case
    it finalizes it (one extra query + the same work a manual submit
    already does) and returns the now-submitted row."""
    if is_attempt_expired(attempt):
        return finalize_attempt(attempt, auto_submitted=True)
    return attempt


def sweep_expired_attempts(*, dry_run=False):
    """Release Candidate — the ONE shared body for the best-effort
    expired-attempt sweep. Called by BOTH the `finalize_expired_attempts`
    management command AND the `/api/cron/finalize-expired-attempts/` HTTP
    endpoint (tests_app.views.FinalizeExpiredAttemptsView), so a Cloud
    Scheduler HTTP trigger and a manual/CLI run are byte-for-byte the same
    logic — never a second finalization implementation.

    Still not required for correctness (every request-time path already
    finalizes an expired attempt it touches — see this module's docstring)
    and still fully idempotent: finalize_attempt() is a no-op on anything
    not still 'in_progress', and each attempt is finalized under its own
    row lock, so overlapping sweeps or a sweep racing a student's own
    submit can never double-score. Returns a small counts dict for the
    caller to log/return.

    The cheap DB-side prefilter (status='in_progress') narrows the
    candidate set before the exact per-row MIN(personal deadline, session
    end) check, which needs Python."""
    now = timezone.now()
    candidates = (
        TestAttempt.objects.filter(status='in_progress')
        .select_related('test', 'session')
        .order_by('id')
    )
    checked = 0
    finalized_ids = []
    for attempt in candidates.iterator():
        checked += 1
        if not is_attempt_expired(attempt, now=now):
            continue
        if not dry_run:
            finalize_attempt(attempt, auto_submitted=True)
        finalized_ids.append(attempt.id)
    return {'checked': checked, 'finalized': len(finalized_ids), 'finalized_ids': finalized_ids, 'dry_run': dry_run}
