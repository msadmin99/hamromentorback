# Phase 7 — Results, Solutions, Ranking & Analytics Access: Architecture

Companion document: `PHASE_7_COMPLETION_REPORT.md`.

---

## 0. Phase-numbering note

Same situation as Phase 6: `implementation-plan.md`'s Phase 7 was "Exam
Builder"; this phase's kickoff content matched that document's Phase 8
("Results / Solutions") almost verbatim. Renumbered (Phase 7 ↔ Phase 8
swapped) at the start of this phase, per the same documented
conflict-resolution procedure used for Phase 6. See the completion
report §1.

## 1. Result lifecycle map (Step 1 of the mandated sequence)

```
TestAttempt (Phase 6: in_progress -> submitted, manual or auto)
    |
    v
CanReview (ownership + status='submitted')  <- unchanged this phase
    |
    +--> score / rank / percentile / accuracy / status / auto_submitted
    |    always visible once CanReview passes — NEW this phase: exposed
    |    together with two additive capability flags (can_view_solutions,
    |    can_view_rank) so the frontend doesn't have to guess.
    |
    v
CanViewSolutions (NEW: ownership + submitted + solutions_visibility policy)
    |
    +--> per-question explanation / is_correct / correct-option marking /
         key_takeaway / reference detail / aggregate correctness stats —
         gated. selected_option_id (the student's OWN pick) is NEVER
         gated by this — that's CanReview's territory.

CanViewRank: unchanged (== CanReview) — no rank-specific policy field
exists in the current architecture, and none was required.

CanViewAnalytics: unchanged (self-only) — now actually called from the
four analytics views instead of only being implicitly true by construction.
```

**What's calculated once vs. every request vs. derived live:**
- `score`, `rank`, `percentile`, `accuracy`, `end_time`, `status`,
  `auto_submitted` — calculated ONCE, at finalization (`tests_app.
  lifecycle.finalize_attempt()`, Phase 6), stored on `TestAttempt`. Never
  recomputed on a result request. **Unchanged this phase** — Phase 7 does
  not touch scoring.
- `is_correct` (per question, per option) — derived live from the stored
  `Answer.selected_option`/`Answer.is_correct` (itself frozen at answer
  time) compared against the *current* `Option.is_correct` — this is the
  one place a live-content dependency already existed pre-Phase-7 (see §9,
  Historical Integrity). Phase 7 does not change this; it only decides
  *whether* to show it.
- `can_view_solutions`, `can_view_rank`, `solutions_locked` — computed
  fresh on every request (cheap, no query beyond what's already loaded).
- Whether solutions are actually released (`solutions_visibility='manual'`)
  — stored: `Test.solutions_released_at`/`by` or `ExamSession.
  solutions_released_at`/`by`, written only by the two new release actions.
- Whether solutions are released under `'auto'` — derived live from
  `tests_app.lifecycle.compute_effective_session_status()` (Phase 6, no
  new field).

## 2. Pre-implementation audit findings (Step 2)

- `can_view_solutions` was byte-for-byte identical to `can_review_attempt`
  — `Test.solutions_visibility` was read nowhere in the codebase's
  authorization path at all (confirmed by grep, not assumed). This was
  already documented as a known gap in the function's own Phase 4
  docstring and `ACCESS_DECISION_MATRIX.md` discrepancy #2.
- **Real bypass, found and fixed**: `AttemptDetailView` and `TestResultView`
  both gated their solution-revealing `TestResultSerializer` response on
  `can_review_attempt` alone — never on `can_view_solutions`. Since the
  two were identical before this phase, this was invisible; the moment
  `can_view_solutions` became genuinely stricter, both endpoints would
  have kept showing full solutions regardless, a real alternate-endpoint
  bypass. Fixed by moving the gating *inside* `TestResultSerializer`/
  `QuestionResultSerializer` itself (§4) rather than at either view's
  branch point, so both endpoints inherit the same enforcement by
  construction — no way for them to drift again.
- `can_view_rank`/`can_view_analytics` existed (Phase 4) but were never
  actually called from any live view — status-quo-correct by construction
  (no rank-visibility policy field exists; analytics views never accept a
  target-user parameter) but not literally enforced in code. Now wired in
  for `can_view_analytics` (§7); `can_view_rank` left exactly as CanReview
  since no additional policy exists to enforce for it.
- `QuestionForAttemptSerializer` (used while an attempt is `in_progress`)
  confirmed clean — uses the public `OptionSerializer` (no `is_correct`),
  never `OptionAdminSerializer`. `QuestionResultSerializer` (the
  solution-revealing one) has exactly two callers: `TestResultSerializer.
  get_questions` (Test Mode review, now gated) and `QuestionViewSet.
  answer()` (QBank's own immediate-explanation feature — a different
  subsystem, unrelated to any `Test.solutions_visibility`, deliberately
  left untouched — see §4).
- `StudentPerformanceOverviewView`'s response cache confirmed already
  correctly per-user-keyed (`request.user.id` is part of the cache key,
  making cross-user leakage impossible by construction) — no caching
  change needed.
- Ranking scope (per-session, not global) confirmed already correct —
  built in Phase 6's `finalize_attempt`, re-verified here, not re-derived.

## 3. Solution representation decision

Rejected: a Test-level or global boolean flip ("hide solutions"). Chosen:
`Test.solutions_visibility` (`'auto'`/`'manual'`, pre-existing field, never
enforced before this phase) is now the actual, enforced policy:

- **`'auto'`**: matches the field's own help text — "Automatically, once
  the exam window ends." Session-scoped attempt: released once
  `compute_effective_session_status(session) == 'completed'` (Phase 6,
  reused, not re-derived). Session-less (anytime) attempt: no window
  exists to end, so released immediately once reviewable — this is
  exactly the platform's pre-Phase-7 behavior for every non-scheduled
  Test, so `'auto'` (the model's default) is a **zero-behavior-change**
  policy for every Mock/PYQ/QBank Test that has never been scheduled
  through a session.
- **`'manual'`**: locked until an admin explicitly releases it. Two new
  nullable field pairs — `solutions_released_at`/`_by` on both `Test`
  (session-less attempts) and `ExamSession` (session-scoped attempts,
  independently per session, so releasing a re-released Daily Test's
  Session #1 can never leak into a still-open Session #2). Two new
  actions: `POST /tests/{id}/release_solutions/`, `POST /exam-sessions/
  {id}/release_solutions/` — feature-gated (`exam_release_solutions`,
  admin-only by default, same `HasFeature` mechanism as `exam_schedule`/
  `exam_archive`/`exam_delete`), idempotent, writes an
  `AdminEditAuditLog` entry via the existing `core.edit_audit.
  record_admin_edit()` helper (no new audit-log model).

No `QuestionVersion`/`TestQuestionSnapshot`/`AnswerSnapshot` — explicitly
not built, per the kickoff prompt's own instruction; see §9.

## 4. Enforcement architecture — inside the serializer, not at the view

`entitlements.services.can_view_solutions(user, attempt)` is the single
decision function (§3's rules). `TestResultSerializer.get_questions()`
computes it once (memoized per serializer instance — `_show_solutions()`)
and passes `show_solutions` into `QuestionResultSerializer`'s context.
`QuestionResultSerializer.to_representation()` strips the solution-
revealing keys (question-level: `is_correct`, `explanation` and its
variants, `key_takeaway`, reference detail, aggregate stats;
per-option: `is_correct`, `explanation`, `pick_count`, `pick_percentage`)
when `show_solutions` is `False`, and always sets a `solutions_locked`
boolean so the frontend never has to infer the state from field absence.
Defaults to `show_solutions=True` when the context key is absent — the
*only* other caller, `QuestionViewSet.answer()` (QBank), never sets it,
so it is completely unaffected by this phase, unchanged.

Because both `AttemptDetailView` and `TestResultView` construct the exact
same `TestResultSerializer` class, this is enforced identically on both —
closing §2's bypass by construction rather than by keeping two view-level
checks in sync by hand.

`selected_option_id` is a separate `SerializerMethodField`, never touched
by the strip — a locked review still shows what the student picked, never
whether it was right. The `?filter=wrong`/`?filter=correct` query
parameter is forced to `'all'` while locked (a soft leak otherwise — which
questions were missed narrows down the answer for a small option set).

`TestResultSerializer` also exposes `can_view_solutions`/`can_view_rank`
as top-level booleans — additive, so the frontend can render locked/
unlocked state without inferring it from field presence.

## 5. Auto-submitted attempts

Treated as fully completed for every result purpose — `finalize_attempt()`
(Phase 6) already produces byte-for-byte the same `status='submitted'`
row regardless of `auto_submitted`'s value; nothing in `can_review_attempt`/
`can_view_solutions`/`can_view_rank` branches on it. Verified by test
(`AutoSubmittedAndMissedResultTests.test_auto_submitted_attempt_is_fully_
reviewable`), not just asserted.

## 6. Missed attempts

Never represented as a `TestAttempt` row (no row is ever created for a
student who never started — matches Phase 6's own `not_started` reasoning:
absence-of-row, not a status value). `ExamSessionSerializer.my_status`
(Phase 6) already surfaces `'missed'` for this case; Phase 7 adds nothing
new here beyond confirming, by test, that a missed student cannot conjure
a result via another student's attempt id (ordinary ownership-scoped 404).

## 7. Analytics wiring

`entitlements.services.can_view_analytics` is now actually called (via a
shared `tests_app.views._deny_if_cannot_view_own_analytics()` helper) from
all four analytics views (`StudentPerformanceOverviewView`,
`SubjectPerformanceDetailView`, `ExamTypeStatsView`,
`PerformanceCalendarView`). Every one of them is self-scoped by
construction (never accepts a target-user id), so this is always a no-op
allow in current usage — the point is that the invariant is now real,
tested code rather than a claim about query shape, and stays correct if
any of these views is ever extended to accept a target parameter.
`AttemptComparativeView` was left as-is — its existing `TestAttempt.
objects.filter(pk=attempt_id, user=request.user)` ownership check is
already a strictly tighter, more specific guard than the generic self-check
would add.

## 8. Ranking

No change to the ranking algorithm itself (Phase 6's `finalize_attempt`,
untouched). Re-verified, not re-derived: rank/percentile are computed once
at finalization, scoped to `(test, session)` so a re-scheduled exam's
sessions never merge pools, ties share rank ("competition ranking"),
auto-submitted attempts count identically to manual ones (same finalize
path). `can_view_rank` continues to require ownership + submitted; no
rank-specific visibility policy field exists in this architecture, and
none was invented.

## 9. Historical integrity — what this phase does and does not solve

**Does not solve**: `is_correct` (both `Answer.is_correct`, frozen at
answer time, and the live re-comparison `QuestionResultSerializer`
performs against the *current* `Option.is_correct` for display) still
depends on live `Question`/`Option` content exactly as before this phase.
Editing a question's correct answer after students have taken it still
changes what those old results *display* as correct, while the frozen
`TestAttempt.score` does not change — the exact, already-documented gap
`implementation-plan.md`'s Phase 9 (Historical Integrity) exists to solve.
**Explicitly not addressed here** — no `QuestionVersion`/snapshot model was
introduced.

**Does protect, this phase**: `finalize_attempt()`'s stored fields
(`score`, `rank`, `percentile`, `accuracy`) are never recomputed by any
Phase 7 code — every new function added this phase is either read-only
(`can_view_solutions`, `can_view_rank`, `can_view_analytics`) or writes
only the two new `solutions_released_at`/`_by` field pairs, never touching
scoring. A result request can change *what's visible*, never *what was
scored*.

## 10. What was NOT built (explicitly deferred)

- Session-scoped "Release Solutions" button on the Admin Sessions &
  Participants detail page — the backend endpoint exists and is fully
  tested; only that specific admin UI trigger is deferred (the Exam Table
  row action covers session-less exams only, by design — see its own
  comment). Reachable via direct API call in the meantime.
- Question versioning / historical snapshots (Phase 9, per
  `implementation-plan.md`, and per this phase's own explicit exclusion).
- Any change to the scoring/grading algorithm.
- A rank-visibility policy field (nothing in the current architecture or
  this phase's spec calls for one — `CanViewRank` remains exactly
  `CanReview`).
