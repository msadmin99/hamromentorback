# Phase 6 — Exam Session, Scheduling & Attempt Lifecycle — Completion Report

**Status:** Complete. Awaiting phase validation before Phase 7.

Companion document: `PHASE_6_ARCHITECTURE.md` (design detail this report
references rather than repeats).

---

## 1. Phase 6 specification summary

The kickoff prompt named `implementation-plan.md → Phase 6` as source of
truth. That document's actual Phase 6 was "Exam Builder" (a frontend
component-consolidation task) — a mismatch with the kickoff prompt's own,
extremely detailed content (session lifecycle, `MIN(attempt_start +
duration, session_end)`, auto-submit, Daily/Grand delivery), which instead
matched that same document's **Phase 7 — "Daily / Grand Delivery"** almost
verbatim (including the exact MIN-rule wording and the
`clone_test_as_new_version` re-release precedent). Applying the kickoff
prompt's own conflict-resolution procedure (identify → report → smallest-
safe interpretation → document): `implementation-plan.md` was renumbered
(Phase 6 ↔ Phase 7 swapped, with an explicit note left in place) at the
start of this phase, and this phase implemented the Daily/Grand Delivery
content under the Phase 6 label the kickoff prompt used. Flagged
immediately in-conversation before any code was written, not discovered
after the fact.

## 2. Pre-implementation audit findings

Re-read (not assumed from prior docs): `Test`, `TestQuestion`,
`ExamSession`, `TestAttempt`, `Answer` models; `_start_attempt`,
`SubmitAnswerView`, `MarkForReviewView`, `SubmitTestView`,
`AttemptDetailView`, `TestResultView`, `ExamSessionViewSet`; `entitlements.
services.can_continue_attempt/can_submit_attempt/can_review_attempt`;
`exam_versioning.py` (reschedule/re-release); background-job infrastructure
repo-wide. Confirmed, precisely:

- `TestAttempt.STATUS_CHOICES` is exactly `in_progress`/`submitted` — no
  timeout state of any kind.
- `_start_attempt` enforces session window (`start_datetime`/
  `end_datetime`/status) **only at start time**. None of
  `SubmitAnswerView`, `MarkForReviewView`, or `SubmitTestView` ever checked
  whether an attempt's deadline had passed — an `in_progress` attempt
  really could remain answerable indefinitely, exactly the audit's
  "single highest-priority stale-state finding."
- `can_continue_attempt`/`can_submit_attempt` (built in Phase 4) were never
  actually called from any live view — status-only, no timing awareness,
  and effectively dead code outside their own tests.
- No Celery/cron/scheduled-job infrastructure exists anywhere in this
  repository — the only async pattern is Cloud Tasks (HTTP-triggered,
  on-demand, never periodic). Confirmed by a repo-wide search, not assumed.
- `ExamSession.refresh_status()` is a lazy-reactive pattern (transitions
  state on-touch) already established in this codebase, called **only**
  from `_start_attempt` — a plain list/retrieve read never refreshes it,
  so the raw `status` column can be stale on read.
- `create_reschedule_session` (re-release) already, correctly, always
  creates a **new** `ExamSession` row — confirmed by reading it, not
  assumed — never mutates or reuses an existing session's window.
- `Test.scheduled_start`/`scheduled_end` are read by exactly one piece of
  live code: `adopt_test_into_template`'s one-time backfill on a legacy
  Test's first-ever reschedule. Not read by `_start_attempt` or any
  ongoing authorization path.
- Grand Test password-vs-entitlement ordering (`_start_attempt`'s
  sequential checks) was already correct — password never substitutes for
  entitlement. Confirmed unchanged, re-tested rather than re-implemented.

## 3. Existing scheduling architecture discovered

`ExamTemplate → Test (version) → ExamSession (scheduled opportunity) →
TestAttempt` — the four-entity hierarchy was already correctly separated,
never collapsed. `ExamSession` (not `Test`) is the real scheduled-delivery
entity; `Test`'s own schedule fields are legacy/version-level, confirmed
dead for live authorization. See `PHASE_6_ARCHITECTURE.md` §1.

## 4. Existing attempt lifecycle discovered

Exactly two states (`in_progress`/`submitted`), one-way transition via
`SubmitTestView`'s inline scoring/ranking, no timeout, no auto-submit
mechanism of any kind, no distinction between a manual and a would-be
system-triggered finalization.

## 5. Final session architecture

Unchanged entity structure. Two additive changes: (1) the transition rule
inside `ExamSession.refresh_status()` was extracted into
`tests_app.lifecycle.compute_effective_session_status()`, a pure function
now shared by the write path (`refresh_status()`) and a new read-only
`ExamSessionSerializer.effective_status` field, so list/retrieve reads are
always live-correct even when the raw `status` column is stale; (2) a new
`my_status` field surfaces the per-student view (`in_progress`/
`submitted`/`missed`/`cancelled`/`not_started`/`null`). See
`PHASE_6_ARCHITECTURE.md` §4.

## 6. Final attempt architecture

`TestAttempt.STATUS_CHOICES` unchanged (`in_progress`/`submitted`) — a
deliberate decision NOT to add the larger status enum
`implementation-plan.md`'s older text described, reasoned through
explicitly and documented in `PHASE_6_ARCHITECTURE.md` §5. One new field:
`auto_submitted` (boolean, default `False`) — distinguishes a
system-triggered finalization from a manual one with zero result-
implication difference and zero risk to the many existing
`status='submitted'` filters elsewhere in the codebase.

## 7. Deadline calculation

`tests_app.lifecycle.effective_attempt_end(attempt)` = `MIN(attempt.
start_time + test.duration_minutes, session.end_datetime)` when a session
exists, else just `attempt.start_time + test.duration_minutes` — every
attempt has a personal deadline regardless of whether it's scheduled.
Server-authoritative: computed fresh from stored timezone-aware
datetimes on every check, never trusts a client-supplied value, never
reads a browser clock. Verified against the spec's own exact Cases A/B/C
numbers (08:00–11:00 window, 180/60-minute durations, 08:00/09:30 starts)
in `tests_app/tests_phase6.py: EffectiveAttemptEndTests`.

## 8. Session state behavior

`upcoming`/`open`/`closed` semantics were already present as `ExamSession.
status`'s richer state set (`draft/scheduled/registration_open/live/
completed/cancelled`) — not re-invented. `draft`/`cancelled` never
auto-transition (explicit admin actions only); `completed` is sticky.
Deterministic from server time via `compute_effective_session_status()`,
now the single place this rule lives. See §5 above and
`PHASE_6_ARCHITECTURE.md` §4.

## 9. Attempt state behavior

`in_progress` → `submitted` remains the only real transition. Entry:
`_start_attempt` (unchanged). Exit: `finalize_attempt()` — either a manual
Submit (`auto_submitted=False`) or a server-triggered finalization
(`auto_submitted=True`), reached via the same code, same scoring, same
ranking. Allowed actions per state: `in_progress` — answer/mark-for-review
(until expiry)/submit(always)/continue(until expiry); `submitted` —
review only. Result implications: identical regardless of how
`'submitted'` was reached.

## 10. Auto-submit/finalization implementation

One function, two callers — request-time (primary) and a management-
command sweep (secondary, best-effort). Full detail in
`PHASE_6_ARCHITECTURE.md` §6. Race-safe (`select_for_update()` +
post-lock re-check) and idempotent, proven by a dedicated concurrency test
(`ConcurrentFinalizationTests`) mirroring the existing
`SubmitTestDoubleSubmissionRaceTests` pattern, plus a command-vs-request-
time race test.

## 11. Daily Test behavior

Window open/upcoming/closed (existing Phase 4 session checks, unchanged),
late-start-gets-less-time via the MIN rule (new), missed detection via
`my_status` (new), attempted-student-can-still-review (existing
`can_review_attempt`, now correctly reached after lazy finalization),
re-release creates a genuinely new opportunity while the old session and
its attempts stay byte-for-byte unchanged (confirmed by test, not just by
reading the code). See `PHASE_6_ARCHITECTURE.md` §8.

## 12. Grand Test behavior

Fixed live window (existing, unchanged), late entry correctly capped by
the MIN rule (new, tested against the spec's own numbers), password as an
additional layer never a substitute for entitlement (existing ordering,
confirmed unchanged and re-tested), ranking/percentile (existing logic,
relocated not rewritten), unattempted students get no post-event access
(existing check, re-tested). Registration/check-in beyond what already
existed was explicitly out of scope. See `PHASE_6_ARCHITECTURE.md` §9.

## 13. Legacy schedule compatibility

`Test.scheduled_start`/`scheduled_end` untouched — no migration, no
deprecation, no behavior change. Confirmed still read only by
`adopt_test_into_template`'s one-time backfill; every other reference is
this phase's own documentation of that fact.

## 14. Files/modules changed

**Backend:**
- `tests_app/lifecycle.py` — **new**: `effective_attempt_end()`,
  `is_attempt_expired()`, `compute_effective_session_status()`,
  `finalize_attempt()`, `ensure_finalized_if_expired()`, `_score_and_rank()`
  (the relocated scoring/ranking body), `_enqueue_stats_safely()`.
- `tests_app/models.py` — `TestAttempt.auto_submitted` field added;
  `ExamSession.refresh_status()` now delegates to `lifecycle.
  compute_effective_session_status()`.
- `tests_app/views.py` — `AttemptDetailView.get`, `TestResultView.get`,
  `_start_attempt`'s resume branch now call `ensure_finalized_if_expired`;
  `SubmitAnswerView.post`/`MarkForReviewView.post` reject+finalize an
  expired attempt; `SubmitTestView.post` reduced to a thin wrapper around
  `finalize_attempt()` (scoring/ranking logic relocated, not duplicated);
  now-dead `_enqueue_stats_safely` removed; unused imports
  (`QuestionBankConfig`, `record_question_result`, `enqueue_question_
  stats_task`) removed.
- `tests_app/serializers.py` — `TestAttemptSerializer` gains
  `effective_end_at`/`server_time`/`auto_submitted`; `TestResultSerializer`/
  `TestAttemptSummarySerializer` gain `auto_submitted`;
  `ExamSessionSerializer` gains `effective_status`/`my_status`.
- `entitlements/services.py` — `can_continue_attempt` denies once
  expired (read-only check); `can_submit_attempt` documented as
  deliberately NOT denying once expired.
- `tests_app/management/commands/finalize_expired_attempts.py` — **new**.
- `tests_app/migrations/0020_testattempt_auto_submitted.py` — **new**.
- `tests_app/tests.py` — one test fixture updated (a session window that
  was accidentally already-closed by construction, see §18), two mock
  patch targets updated to the new module location for the relocated
  scoring code.
- `tests_app/tests_phase6.py` — **new**, 44 tests.
- `docs/implementation-plan.md`, `docs/PHASE_6_ARCHITECTURE.md`,
  `docs/PHASE_6_COMPLETION_REPORT.md` — new/updated.

**Frontend:**
- `Frontend/src/app/tests/attempt/[attemptId]/page.js` — the exam-taking
  countdown now reads `effective_end_at` (server-computed, session-aware)
  instead of recomputing `start_time + duration_minutes` locally, which
  previously ignored a shorter session window entirely. Additive/display-
  only — the backend independently re-validates every action regardless.

No other file changed. No Student Dashboard, QBank, Subscription/Commerce
UI, or mobile changes — none were required.

## 15. APIs changed

No endpoint removed, renamed, or made backward-incompatible. Additive
response fields only: `TestAttemptSerializer` (`effective_end_at`,
`server_time`, `auto_submitted`), `TestResultSerializer`/
`TestAttemptSummarySerializer` (`auto_submitted`), `ExamSessionSerializer`
(`effective_status`, `my_status`). New behavior, not new shape:
`SubmitAnswerView`/`MarkForReviewView` now return `403 {code:
'exam_closed'}` for a request against an attempt whose deadline has
passed (previously such a request would have silently succeeded, or hit a
generic 404 once *manually* submitted but never on its own from time
alone). `SubmitTestView`'s "loser" of a true concurrent double-submit race
now gets `200` with the already-final result instead of `404` — a
deliberate, tested UX improvement (see §16), not a contract break (the
existing race test's own assertion already tolerated either outcome).

## 16. Database/migrations

One schema migration: `0020_testattempt_auto_submitted.py` — adds
`TestAttempt.auto_submitted` (`BooleanField(default=False)`). Verified via
`makemigrations --check --dry-run` → "No changes detected" both
immediately after generating it and again after the full test run.
Applied to the local dev DB (already-migrated) with zero errors; also
implicitly exercised on a fresh DB by the test runner (774/774 — see §19).
No production database was touched.

## 17. Security improvements

- Every attempt-mutating endpoint (`SubmitAnswerView`, `MarkForReviewView`)
  now independently, server-side, rejects an action past the effective
  deadline — closing the audit's "TestAttempt could remain in_progress
  indefinitely" gap. Verified specifically for the "scheduler delayed"
  scenario (Case G): the request path alone, with no background job ever
  invoked, still correctly rejects and finalizes.
- `can_continue_attempt` is now genuinely expiry-aware, closing the gap
  where the Phase 4 capability layer would have answered "allowed" for an
  attempt whose deadline had already passed.
- Confirmed (by test, not assumption): a student cannot answer, view, or
  submit another student's attempt (ownership-scoped 404s, unchanged);
  cannot influence `start_time`/`status`/`score`/`rank` via a spoofed
  payload on start (server-controlled fields only); Grand Test password
  remains required independently of window/entitlement; wrong password
  still never creates an attempt (unchanged from Phase 3, re-tested
  alongside the new timing logic).
- No new access-control bypass of the Phase 4 capability engine — every
  new check composes `is_attempt_expired`/`effective_attempt_end`, never
  reimplements academic or commercial entitlement.

## 18. Performance impact

- `effective_attempt_end`/`is_attempt_expired` are pure Python over
  already-loaded fields — zero additional queries at any call site that
  already had `attempt.test`/`attempt.session` in scope (all of them do,
  via existing `select_related`/direct FK access).
- `ensure_finalized_if_expired` adds exactly one extra query
  (`finalize_attempt`'s own `select_for_update().get()`) **only** when an
  attempt is actually expired — a rare path, not the common case, and only
  on already-mutating or resume-checking requests, never a hot per-second
  poll.
- `ExamSessionSerializer.my_status` adds one query per row (an attempt
  lookup, not annotated onto the queryset) — accepted for now since
  session listings are small/curated, not catalog-scale; explicitly
  flagged in `PHASE_6_ARCHITECTURE.md` §4 as a spot to revisit if that
  assumption stops holding, not silently declared cost-free.
- `finalize_expired_attempts` prefilters to `status='in_progress'` before
  doing any per-row Python check — bounded by how many attempts are
  actually in progress platform-wide at run time, not by total attempt
  history.
- No per-second server polling was introduced anywhere — the frontend
  countdown still ticks purely client-side; the server is only consulted
  on actual student actions (answer/mark/submit/resume/view-result), same
  request cadence as before this phase.
- No existing query-count-pinned test needed updating — all such tests in
  the full suite still pass unchanged (see §19).

## 19. Tests added and full-suite result

**44 new tests** in `tests_app/tests_phase6.py`: `EffectiveAttemptEndTests`
(7 — the MIN-rule unit cases, including the spec's own A/B/C numbers),
`TimingMatrixEndpointTests` (4 — mandatory Cases D/E/F/G via real HTTP
endpoints), `AutoSubmitFinalizationTests` (6), `CanContinueCanSubmitExpiry
Tests` (3), `SessionEffectiveStatusTests` (7), `DailyTestDeliveryTests`
(5), `GrandTestDeliveryTests` (4), `AttemptLifecycleSecurityTests` (4),
`FinalizeExpiredAttemptsCommandTests` (4), `ConcurrentFinalizationTests`
(1) — 44 total, plus one existing test fixture repaired (see §18) and two
existing mock-patch targets updated for the relocated scoring code (see
§14).

```
cd Backend && python manage.py test
```

**774/774 tests pass** (baseline after Phase 5: 730; this phase: +44 new,
zero regressions). One pre-existing test's fixture was repaired (a session
window that was, by construction, already closed by the time its HTTP
calls ran — an artifact of the fixture never having cared about real
timing before Phase 6 enforcement existed to notice; the ranking-pool
behavior it actually tests is unchanged and still passes). The `redis
down` / Cloud Tasks `DefaultCredentialsError` tracebacks visible in the
run's output are pre-existing, deliberately-injected failure-path tests
(unrelated to this phase, present in every prior phase's run) — not real
failures; the run's final result is `OK`.

## 20. Deferred issues and deployment readiness

**Deferred, explicitly:**
- Wiring `finalize_expired_attempts` to an actual schedule (Cloud
  Scheduler or equivalent) — the command exists, is tested, idempotent,
  and safe to run; connecting it to a real cadence is a deployment-time
  infra decision outside this repository's scope.
- Attempt-level `cancelled` status; session-cancellation cascading to
  in-progress attempts (a pre-existing gap, not introduced this phase);
  full Grand Test registration/check-in state machine — none required by
  this phase's actual spec.
- `solutions_visibility` enforcement — still Phase 8.
- The exam-taking result page's error handling for the new mid-answer
  `exam_closed` 403 shows the existing generic failure state rather than a
  dedicated "time's up" message — functionally correct (the answer is
  correctly rejected and the attempt correctly finalized either way) but
  not a polished UX moment; flagged, not silently left unmentioned.

**Deployment readiness:** Not deployed. No persistent/production database
was modified — every change is a local working-tree edit (this repository
is not a git repository) plus one local SQLite dev-DB migration apply,
used only to verify migration correctness. Deployment requires separate
explicit approval, as always.

---

Phase 6 complete. Stopping here per the mandatory stop, awaiting validation before Phase 7.
