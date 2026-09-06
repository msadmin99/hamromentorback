# Phase 8 — Historical Integrity, Question Versioning & Result Snapshot Architecture — Completion Report

**Status:** Complete. Awaiting phase validation before Phase 9.

Companion documents: `QUESTION_VERSIONING_DESIGN.md` (pre-migration design
review, written first per `implementation-plan.md`'s explicit requirement),
`PHASE_8_ARCHITECTURE.md` (as-built reference).

---

## 1. Current implementation-plan.md Phase 8 specification

Third consecutive phase-numbering mismatch: `implementation-plan.md`'s
Phase 8 was "Exam Builder" (a frontend consolidation task); this phase's
kickoff prompt's content — Question Versioning, attempt/delivery
snapshots, historical correctness — matched that document's Phase 9
("Historical Integrity") verbatim, including the same two evidenced
problems. Applying the same conflict-resolution procedure used for Phases
6 and 7 (now an established pattern across three consecutive phases):
`implementation-plan.md` was renumbered (Phase 8 ↔ Phase 9 swapped) at the
start of this phase, flagged immediately before any code was written. The
actual Phase 8 specification (post-renumbering) requires: a pre-migration
design document (`QUESTION_VERSIONING_DESIGN.md`, written first,
evaluating snapshot-on-attempt vs. true question versioning vs. hybrid —
explicitly not pre-decided by the plan) solving exactly two evidenced
problems (live-content-changes-historical-display; option delete/recreate
nulls historical selections), with a migration strategy that handles
existing historical rows via graceful degradation (live-read fallback)
rather than fabricated backfill.

## 2. Pre-implementation audit findings

Full detail in `QUESTION_VERSIONING_DESIGN.md` §1-3. Headline: the two
originally-evidenced problems were both confirmed, with exact code
references (`academics/serializers.py:281-293` for the option delete/
recreate; `tests_app/serializers.py`'s `get_questions()` for the live
read). A **third**, previously-unnamed but same-root-cause gap was found
during this phase's own audit: `TestAdminSerializer.update()`'s question-
list replace also deletes/recreates every `TestQuestion` row, meaning
editing which questions a Test contains after it's been attempted
silently changes old attempts' review pages too — not just each
question's content, but the question *set and order* itself.

## 3. Historical-integrity problems confirmed

1. Question/option content edits after finalization changed historical
   display (confirmed, `academics/serializers.py` + `tests_app/
   serializers.py`).
2. Option delete/recreate nulls `Answer.selected_option`/`QuestionAttempt.
   selected_option` (confirmed, `SET_NULL` FKs, `tests_app/models.py:394`,
   `academics/models.py:337`).
3. `TestQuestion` list replace changes historical question set/order
   (newly confirmed this phase, same root cause as #1).

## 4. Existing mutable-data risks

See `QUESTION_VERSIONING_DESIGN.md` §2-3 for the complete answers to all
twelve "Current Historical-Integrity Questions" the kickoff prompt posed,
each with a code reference. Also confirmed **already safe** (re-verified,
not assumed): `TestAttempt.score`/`rank`/`percentile` frozen at
finalization and never recomputed; `Answer.is_correct` frozen at answer-
time; Question deletion already blocked when historically referenced
(`QuestionViewSet.destroy()`); import "replace" already conservative
(`question_is_referenced()`).

## 5. Versioning vs. snapshot analysis

Full trade-off table in `QUESTION_VERSIONING_DESIGN.md` §4. Summary:
immutable `Question`/`Option` versioning was rejected as platform-wide in
footprint (QBank, search, import, stats, Smart Practice would all need to
become version-aware) for a problem actually scoped to exam-attempt
review. Immutable attempt-scoped snapshot was chosen — touches only the
finalization write point and the result read point, zero changes to
authoring/import/QBank/stats.

## 6. Final architecture chosen

One new model, `tests_app.models.AttemptQuestionSnapshot` — one immutable
row per `(attempt, question)`, created once inside `finalize_attempt()`'s
existing transaction (Phase 6), read by a new `AttemptQuestionSnapshot
ResultSerializer` reused inside `TestResultSerializer.get_questions()`
with a live-read fallback for pre-Phase-8 attempts. Full diagram and
reasoning in `PHASE_8_ARCHITECTURE.md` §1-6.

## 7. Data-model changes

`AttemptQuestionSnapshot`: `attempt` (FK, CASCADE), `question` (FK,
**SET_NULL**, nullable — traceability only, every display field is
duplicated so this going null changes nothing shown), `order`, `text`,
`latex`, `image_data` (JSON, resolved), explanation fields (`explanation`,
`explanation_latex`, `explanation_image_data`, `explanation_video_url`,
`key_takeaway`), reference fields, `subject_name`, `marks`,
`negative_marks` (display only), `options_snapshot` (JSON list),
`selected_option_original_id` (**plain integer, not an FK** — the field
that actually fixes the SET_NULL problem), `created_at`. One migration
(`0022_attempt_question_snapshot.py`), schema-only, no data migration.
Full field-by-field rationale in `QUESTION_VERSIONING_DESIGN.md` §5.

## 8. Historical capture process

`tests_app/lifecycle.py: _create_question_snapshots(attempt)`, called from
`finalize_attempt()` immediately after `_score_and_rank()`, same
transaction. Reads `TestQuestion` (the authoritative question list at
this exact moment) joined with `Question`/`Option` and cross-referenced
with `attempt.answers.all()`, `bulk_create()`s one snapshot row per
question. Runs identically for manual submit, request-time auto-
finalization, and the `finalize_expired_attempts` command (all three
already funnel through `finalize_attempt()`).

## 9. Question editing behavior

Unchanged — `QuestionAdminSerializer` was not modified. An edit
immediately affects the live `Question` (used by every *future* attempt);
already-finalized attempts are unaffected, confirmed by test
(`QuestionEditHistoricalIntegrityTests`).

## 10. Option editing behavior

Unchanged (`QuestionAdminSerializer.update()`'s delete+recreate was not
modified) — but its historical consequence is now fully absorbed: the
live `Answer.selected_option` still goes null (confirmed, unchanged), but
the review page no longer depends on it, reading `selected_option_
original_id` from the snapshot instead (confirmed by test,
`OptionReplacementIntegrityTests`, which explicitly asserts the live FK
*does* go null while the review stays completely correct).

## 11. Correct-answer behavior

An old attempt continues to display (and was already correctly scoring
against) whichever option was correct *at finalization time*, even after
the live `Option.is_correct` changes — confirmed by test
(`test_correct_answer_change_after_finalize_does_not_alter_historical_
grading_or_display`). A new attempt created after the change uses the new
answer key, both for scoring and display (confirmed by test).

## 12. Explanation/solution behavior

Frozen at finalization, read subject to the exact same Phase 7 `CanView
Solutions`/`solutions_locked` gating as before — Phase 8 changed the
content *source*, not who can see it. Confirmed by test.

## 13. Import behavior

Unchanged. `create_question_from_row`/`question_is_referenced`/skip/
replace/keep_both were not modified. "Replace" on a historically-
referenced question still creates a new question rather than touching the
old one (pre-existing, re-confirmed by test,
`ImportReplacementHistoricalIntegrityTests`).

## 14. Deletion behavior

Unchanged and re-confirmed: `QuestionViewSet.destroy()` still blocks
deleting a historically-referenced question. Additionally verified (model-
level, bypassing the guard on purpose) that even a force-deleted Question
leaves its snapshots completely intact and correctly displayable — the
defensive `SET_NULL` design working as intended
(`test_snapshot_survives_even_if_the_live_question_row_is_gone`).

## 15. Test configuration behavior

`Test.negative_marking`/`Question.marks`/`negative_marks` changes after
finalization do not alter the already-stored score (already true before
this phase; explicitly re-tested,
`TestConfigurationHistoricalIntegrityTests`). `Test`-level config fields
were deliberately not snapshotted — see `QUESTION_VERSIONING_DESIGN.md`
§5 for why each candidate field was or wasn't included.

## 16. Migration strategy

Additive only: one `CreateModel` migration, zero data migration, zero
change to any existing table. No backfill for pre-Phase-8 attempts —
deliberate, documented decision (`QUESTION_VERSIONING_DESIGN.md` §7):
backfilling from *current* content wouldn't recover *true* original
content for anything already edited, and risks looking more authoritative
than it is. The result serializer's live-read fallback (unchanged
original code) handles pre-Phase-8 attempts exactly as before this phase.

## 17. Migration result

Applied cleanly to the local, already-migrated dev DB. Reversibility
verified directly (not just claimed): rolled the migration back one step
and reapplied it, both succeeded cleanly. `makemigrations --check --dry-
run` → "No changes detected", confirmed both immediately after generating
the migration and again after the full test run. Also implicitly
exercised on a fresh DB via the test runner (every full-suite run builds
one from scratch). No production database was touched.

## 18. API changes

No endpoint removed, renamed, or made incompatible. `TestResultSerializer`'s
`questions` array keeps the exact same per-question field names for a
snapshot-backed attempt as it always had for a live-read one (confirmed
by test — every existing frontend consumer needs zero changes for fields
it already reads). No new endpoint was added (no `AttemptQuestionSnapshot`
serializer/ViewSet exposed directly — it's read only as a nested part of
the existing result payload).

## 19. Frontend changes

Minimal, defensive only: `Frontend/src/app/tests/result/[attemptId]/
page.js` — the per-question React `key` now falls back to the array index
when `q.id` is null (a snapshot whose live Question has since been
deleted — a real but rare edge case, since deletion is normally blocked
while historical attempts exist), and the "Report this question" button
is hidden when `q.id` is null (nothing live to report against). No other
frontend file changed; no Student Dashboard, Question Bank UI, Exam
Builder, Subscription UI, or mobile changes.

## 20. Security improvements

No new write surface (`AttemptQuestionSnapshot` has no serializer field
accepting client input, no ViewSet, no endpoint) and no new IDOR surface
(read only via an already ownership-checked `TestAttempt`, never by a
directly-addressable snapshot id). Phase 4's `CanReview`/`CanViewSolutions`
remain the sole access-control layer — confirmed unchanged by test.

## 21. Performance impact

Write side: `finalize_attempt()` gains a small, bounded, constant number
of additional queries (one `TestQuestion`/`Question`/options fetch, one
`bulk_create`) — a low-frequency, once-per-attempt action, not a hot
path. Read side: a snapshot-backed result actually issues *fewer* queries
than the original live-read path (one denormalized query replaces a
question-fetch-plus-options-prefetch pair). No query-count-pinned test
exists for either endpoint (confirmed by search) — no baseline needed
updating.

## 22. Number of new tests

**16 new tests** in `tests_app/tests_phase8.py`: `QuestionEditHistorical
IntegrityTests` (6), `OptionReplacementIntegrityTests` (1),
`QuestionDeletionProtectionTests` (2), `TestConfigurationHistorical
IntegrityTests` (1), `QuestionOrderHistoricalIntegrityTests` (1),
`ImportReplacementHistoricalIntegrityTests` (1),
`AutoSubmittedHistoricalIntegrityTests` (1),
`BackwardCompatibilityFallbackTests` (2, including an embedded
`makemigrations --check` sanity test), `ConcurrentFinalizationSnapshot
Tests` (1).

## 23. Full test-suite result

```
cd Backend && python manage.py test
```

**829/829 tests pass** (baseline after Phase 7: 813; this phase: +16 new,
zero regressions, zero pre-existing tests modified). Confirmed directly
from the run's own final output (`Ran 829 tests in 358.005s` / `OK`), not
inferred. One operational note, not a test-correctness issue: the
background shell process continued idling at 0% CPU for an extended
period *after* printing that final "OK" and destroying the test database
— almost certainly a mocked gRPC client thread (the same `Cloud
TasksClient`/`DefaultCredentialsError` mock exercised throughout this and
every prior phase's runs) not exiting cleanly, not a hang in the actual
test execution, which completed in the same ~6 minutes typical of the
full suite in Phases 6-7. The process was terminated after its result was
already captured; the 829/829/OK result above is read directly from the
run's own log output.

## 24. Query-count/performance changes

None pinned/tracked — see §21. No existing scalability test needed
updating.

## 25. Compatibility risks

None identified for existing attempts (§7/§16 — schema-only migration,
live-read fallback preserves pre-Phase-8 behavior exactly). The one new
runtime behavior — snapshot creation adds a few queries to
`finalize_attempt()` — was exercised extensively by the full Phase 1-7
regression suite (every test that finalizes an attempt now also creates
snapshots) with zero failures, giving real, not just theoretical,
confidence it doesn't destabilize anything downstream (ranking, stats
enqueueing, etc.).

## 26. Historical limitations

Stated plainly, per `PHASE_8_ARCHITECTURE.md` §11: pre-Phase-8 attempts
have no point-in-time content if their questions were edited before this
migration shipped (unavoidable, documented, not fabricated around). Peer
aggregate stats (`students_correct_percent` etc.) are deliberately not
frozen — a live, ever-changing number by nature. Image/media *files*
aren't duplicated, only resolved URL/variant references at snapshot time.
`QuestionAttempt`/`QuestionEvent` (QBank mastery tracking) retain the same
pre-existing `SET_NULL` exposure — a different subsystem, explicitly out
of this phase's scope.

## 27. Deferred work

`QuestionVersion`/`OptionVersion` (full versioning architecture) —
evaluated and explicitly rejected, not merely postponed (see §6/`QUESTION_
VERSIONING_DESIGN.md` §4). No change to `QuestionAdminSerializer`, the
Question Bank UI, or import pipeline semantics. No backfill migration. No
change to scoring, ranking, or analytics aggregation algorithms. Exam
Builder (the actual current Phase 9 per this phase's own renumbering) —
explicitly not started, per the mandatory stop.

## 28. Deployment readiness

**Not deployed.** No persistent/production database was modified — every
change is a local working-tree edit (this repository is not a git
repository) plus local SQLite dev-DB migration applies (including a
verified rollback/reapply cycle), used only to confirm migration
correctness. Deployment requires separate explicit approval, as always.

---

**Summary of explicitly requested facts:**
- Baseline test count: 813
- Final test count: 829
- New tests: 16
- Number of migrations: 1 (`0022_attempt_question_snapshot.py`, schema-only)
- `makemigrations --check` passed: yes (confirmed twice)
- Production DB touched: no
- Anything deployed: no
- Flaky tests: none observed or introduced this phase
- Changed test fixtures: none from prior phases (`tests_phase6.py`/
  `tests_phase7.py` fixtures unmodified; the one pre-existing fixture
  repair happened in Phase 6, not this phase)
- Changed query-count baselines: none (no pinned assertion existed for
  the affected endpoints)

Phase 8 complete. Stopping here per the mandatory stop, awaiting validation before Phase 9.
