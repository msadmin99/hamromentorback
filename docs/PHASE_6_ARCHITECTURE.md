# Phase 6 — Exam Session, Scheduling & Attempt Lifecycle: Architecture

Companion document: `PHASE_6_COMPLETION_REPORT.md`. This document covers
the lifecycle model, deadline calculation, state semantics, and
finalization design this phase implements.

---

## 0. A note on the phase number

`implementation-plan.md`'s Phase 6 was "Exam Builder" (a frontend
component-consolidation task); this phase's actual content — session
lifecycle, `MIN(attempt_start + duration, session_end)`, auto-submit,
Daily/Grand delivery — matched what that same document called **Phase 7**
almost verbatim. Per the kickoff prompt's own conflict-resolution
procedure, the plan document was renumbered (Phase 6 ↔ Phase 7 swapped) at
the start of this phase, so `implementation-plan.md` now agrees with what
was actually built. See the completion report §1 for the full account.

## 1. The four-entity hierarchy — unchanged, clarified

```
ExamTemplate   stable exam identity (groups versions/sessions)
    ↓
Test           exam version/configuration (question set, duration, rules)
    ↓
ExamSession    one scheduled opportunity/event (optional — see §2)
    ↓
TestAttempt    one student's participation in one opportunity
```

None of these four models were collapsed or duplicated. `Test.
scheduled_start`/`scheduled_end` remain exactly what the pre-Phase-6 audit
already correctly identified them as: legacy, version-level fields, never
read by `_start_attempt`'s live authorization path (confirmed by a
repository-wide grep — the only production code that ever reads them is
`exam_versioning.adopt_test_into_template`, which backfills a session's
initial window from them the *first* time a legacy Test is rescheduled, a
one-time migration-shaped operation, not an ongoing scheduling source).
Left untouched — no migration, no deprecation — since removing them would
be a real backward-compatibility risk for zero behavioral gain this phase
needs.

## 2. Scheduled session vs. anytime test

A `Test` with no `ExamSession` (the common case for Practice/QBank, Mock,
PYQ) is "anytime access" — `_start_attempt`'s session-window checks simply
never run (`if session:` guards every one of them). Every attempt still
gets a **personal deadline** regardless: `effective_attempt_end()`
generalizes the spec's MIN rule to `attempt_start + test.duration_minutes`
when there's no session, since every `Test` already has a
`duration_minutes` (the same number the frontend countdown has always
displayed) — this phase's real, new behavior is that the server now
*enforces* what the UI already promised, for every exam type, not just
scheduled ones. A session, when one exists, can only ever shorten that
ceiling, never lengthen it.

## 3. Deadline calculation — `tests_app/lifecycle.py: effective_attempt_end()`

```python
effective_attempt_end(attempt) = min(
    attempt.start_time + attempt.test.duration_minutes,
    attempt.session.end_datetime,   # only if attempt.session_id is set
)
```

Pure, read-only, no query beyond what's already loaded on `attempt`
(`select_related('test', 'session')` at every call site that iterates
attempts). `is_attempt_expired(attempt, now=None)` wraps it: an attempt is
only ever "expired" while `status == 'in_progress'` — a submitted attempt
is just submitted, never "expired," regardless of how old it is.

## 4. Session state — `ExamSession.status` vs. `effective_status`

The raw `status` column (`draft/scheduled/registration_open/live/
completed/cancelled`) is a **write-path** value — the last state an
explicit write (an admin action, or `ExamSessionViewSet.start` calling
`session.refresh_status()`) actually persisted. It can be stale on a plain
read if nothing has written to that session since its window changed —
the pre-Phase-6 audit's own "session state refresh was partly lazy"
finding.

`tests_app.lifecycle.compute_effective_session_status()` is the same
transition rule, computed **without writing** — `ExamSession.
refresh_status()` now delegates to it (so the write path and the read path
can never independently drift on what "live"/"completed" means), and
`ExamSessionSerializer.effective_status` exposes it on every list/retrieve
read, live-correct even when the raw column is stale. `draft` and
`cancelled` never auto-transition (explicit admin actions only); `completed`
is sticky once reached (a session's window closing is a one-way transition
in this model — no "un-completing").

`ExamSessionSerializer.my_status` (additive) answers "what does the
current user see for this session": `'in_progress'`/`'submitted'` (their
own attempt's real status), `'missed'` (session `effective_status ==
'completed'`, no attempt), `'cancelled'` (session cancelled, no attempt),
`'not_started'` (session still open/upcoming, no attempt), or `null`
(anonymous). One extra query per row (not annotated onto the queryset) —
accepted for now since session lists are small/curated, not catalog-scale;
flagged in the completion report as a spot to revisit if that stops being
true.

## 5. Attempt state — deliberately NOT a new status enum

`TestAttempt.STATUS_CHOICES` stays exactly `in_progress`/`submitted`. A
new `auto_submitted` boolean (default `False`) was added instead of the
larger `not_started/in_progress/submitted/auto_submitted/expired/
abandoned/cancelled` enum `implementation-plan.md`'s original (Phase-1-era)
text listed as "the spec requires." This is a deliberate, documented
divergence — reasoned through explicitly, not an oversight:

- **`not_started`**: never a DB row exists before a student starts — there
  is no "attempt record exists but hasn't started" concept in this
  architecture, and nothing in this phase's actual spec requires
  pre-created placeholder rows. Represented by "no `TestAttempt` row",
  never a status value.
- **`auto_submitted`**: same result implications as a manual submit —
  scored, ranked, reviewable, identical `status='submitted'` — the *only*
  thing that differs is *who/what* triggered it. Adding a second status
  value here would mean updating every existing `status='submitted'`
  filter across the codebase (ranking pool, `can_review_attempt`, every
  serializer, `performance.py`) to treat two values as equivalent — real
  surface area, real drift risk, for zero behavioral difference. A boolean
  marker gets the same information (for UI copy: "time ran out" vs. "you
  submitted this") with zero of that risk.
- **`expired`**: considered as a distinct "never finalize a no-answer
  abandonment" status, rejected for consistency — a manually-submitted
  zero-answer attempt already scores 0 and enters the ranking pool today
  (existing, validated behavior); auto-submission reuses the *exact same*
  `finalize_attempt()` code path, so a zero-answer auto-submission behaves
  identically, on purpose. No separate treatment, no separate status.
- **`abandoned`**: no product requirement or mandatory test case describes
  a state distinct from "auto-submitted due to expiry." Not built.
- **`cancelled`** (attempt-level, e.g. an admin voiding one specific
  attempt): no existing code path creates this and it's outside this
  phase's mandatory scope. Not built, not silently assumed — flagged as
  deferred.

"Every status must have entry condition, exit condition, allowed actions,
result implications" — applied here as the actual filter for what
qualified as a real new state, not a reason to add a nicer-looking enum
that duplicates information already available another way.

## 6. Finalization — one function, two callers

`tests_app/lifecycle.py: finalize_attempt(attempt, auto_submitted=False)`
is the **only** place a `TestAttempt` is ever scored, ranked, and marked
`'submitted'` — the exact scoring/ranking body that used to live inline in
`SubmitTestView.post` (unchanged in behavior, only relocated), so a manual
submit and a server-triggered auto-submit are byte-for-byte the same code.
Race-safe (`select_for_update()` + a post-lock `status != 'in_progress'`
re-check) and idempotent (a no-op on anything already finalized).

Reached two ways, matching the "request-time checks AND background jobs
complement each other, neither alone is sufficient" requirement:

1. **Request-time (primary, always-correct)**: `ensure_finalized_if_
   expired()` is called at the top of `AttemptDetailView.get`,
   `TestResultView.get`, and `_start_attempt`'s resume branch (the same
   lazy-reactive pattern this codebase already used for `ExamSession.
   refresh_status()`). `SubmitAnswerView.post` and `MarkForReviewView.post`
   check `is_attempt_expired()` directly and, if true, finalize and
   **reject the specific late action** with a 403 (`code: 'exam_closed'`)
   — "student must no longer be allowed to answer" past the deadline.
   `SubmitTestView.post` always finalizes via `finalize_attempt()`
   regardless of expiry (see §7).
2. **Background sweep (secondary, best-effort)**: `python manage.py
   finalize_expired_attempts` finds every expired `in_progress` attempt
   platform-wide and finalizes it — for the one case request-time checks
   can't cover: an attempt nobody ever revisits again. **Not relied on for
   correctness** — every request path above is already correct without it
   ever running. Idempotent, safe to run repeatedly or concurrently with
   live traffic (own row lock per attempt). Not wired to a specific
   scheduler by this phase (see completion report §20, deployment
   readiness) — deliberately, since introducing a new scheduler
   infrastructure (Celery, cron) wasn't judged necessary when this
   codebase's existing async pattern (Cloud Tasks, HTTP-triggered) plus a
   plain management command already cover the requirement without adding
   one.

## 7. CanContinue vs. CanSubmit — the one deliberate asymmetry

- **`can_continue_attempt`**: denies once `is_attempt_expired()` is true,
  even before any request has actually written the finalized row — a pure
  read-only check, so a student can never keep answering just by not
  triggering a write.
- **`can_submit_attempt`**: stays allowed while `status == 'in_progress'`
  regardless of expiry. A late Submit click still finalizes — using
  whatever was legitimately answered *before* `SubmitAnswerView` started
  rejecting new answers at the deadline. This is what turns an
  about-to-be-abandoned attempt into a properly scored one instead of a
  rejected request; it does not reopen answering (that's `CanContinue`'s
  job, and it already said no).
- **`can_review_attempt`**: unchanged — requires `status == 'submitted'`.
  Since every review-path view (`AttemptDetailView`, `TestResultView`)
  lazily finalizes first, this capability is always evaluated against an
  already-correct row; no expiry-awareness needed inside the function
  itself.

## 8. Daily Test delivery

Window open/upcoming/closed comes for free from `_start_attempt`'s
existing (Phase 4) session-window checks — unchanged this phase. What's
new: the personal-duration-vs-session-end MIN rule (§3), the request-time
finalization backstop (§6), and `my_status` surfacing `'missed'` (§4).
**Re-release**: `exam_versioning.create_reschedule_session()` was already
confirmed — by reading it, not assuming — to always create a **new**
`ExamSession` row (never mutates/reuses the old one for a new window);
`DailyTestDeliveryTests.test_re_release_creates_a_new_session_old_session_
and_attempt_untouched` in `tests_phase6.py` proves the old session's
timestamps and an old attempt's score/status/session FK are untouched
after a re-release, end to end. No code change was needed here — only
regression coverage for an already-correct precedent.

## 9. Grand Test delivery

Fixed start/end (existing `_start_attempt` session checks), late entry
correctly capped by the MIN rule (§3, directly tested with the spec's own
08:00–11:00 / 180-minute / 09:30-join numbers), password as an additional
layer never a substitute for entitlement (existing `_start_attempt`
ordering — academic+commercial entitlement checked before password —
confirmed unchanged, re-tested), ranking/percentile (existing
`finalize_attempt`, formerly `SubmitTestView` inline, confirmed unchanged
behavior via the relocated-not-rewritten scoring logic), unattempted
students getting no post-event access (existing `can_access_test` +
`_start_attempt`'s window check, re-tested). Registration/check-in beyond
what already exists was explicitly out of scope and not built.

## 10. What was NOT built (explicitly deferred, not silently skipped)

- A specific scheduler wiring (Cloud Scheduler cron hitting
  `finalize_expired_attempts`) — the command exists and is tested; wiring
  it to a real schedule is a deployment-time infra decision.
- Attempt-level `cancelled` status / admin-cancels-a-single-attempt.
- Session-cancellation cascading to its in-progress attempts (an admin
  cancelling a *live* session with attempts already in progress) — no
  existing behavior handled this before Phase 6 either; not addressed,
  flagged as a real, pre-existing gap outside this phase's mandatory scope.
- Full Grand Test registration/check-in state machine (entitled →
  registered → checked-in → attempt) — explicitly out of scope per the
  kickoff prompt.
- `solutions_visibility` enforcement (still Phase 8 territory, untouched).
