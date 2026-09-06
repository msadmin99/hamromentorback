# Phase 8 — Historical Integrity, Question Versioning & Result Snapshot Architecture

Companion documents: `QUESTION_VERSIONING_DESIGN.md` (the pre-migration
design review — audit, versioning-vs-snapshot analysis, and the reasoning
behind every field choice; this document is the as-built reference and
doesn't repeat that reasoning), `PHASE_8_COMPLETION_REPORT.md`.

---

## 0. Phase-numbering note

Third consecutive renumbering: `implementation-plan.md`'s Phase 8 was
"Exam Builder"; this phase's kickoff content matched that document's
Phase 9 ("Historical Integrity") verbatim, down to the same two evidenced
problems. Renumbered (Phase 8 ↔ Phase 9 swapped) at the start of this
phase, per the same conflict-resolution procedure used for Phases 6 and
7. See the completion report §1.

## 1. The architecture, end to end

```
Question (live, editable, unchanged by this phase)
Option (live, editable, unchanged by this phase)
    │
    │  finalize_attempt() — Phase 6, extended this phase
    ▼
AttemptQuestionSnapshot  ← one immutable row per (attempt, question),
    │                       created once, at finalization, in the same
    │                       transaction as scoring. Never updated again.
    ▼
TestResultSerializer.get_questions()
    │  — snapshots exist?  →  AttemptQuestionSnapshotResultSerializer
    └  — none (pre-Phase-8) →  QuestionResultSerializer (original, live-read,
                                unchanged fallback)
    ▼
AttemptDetailView / TestResultView  (Phase 4/7 CanReview/CanViewSolutions
                                      gating, completely unchanged)
```

`Question`/`Option`/`TestQuestion`/`Answer`/`QuestionAttempt` — every
existing model — are **schema-unchanged** by this phase. One new model,
one new write point (already the single place scoring happens), one new
read branch (in the one serializer method that already built this
payload).

## 2. What audit confirmed was already safe (re-verified, not assumed)

- `TestAttempt.score`/`rank`/`percentile`/`accuracy` are computed once by
  `finalize_attempt()` and never recomputed — already historically stable.
- `Answer.is_correct` is a plain boolean, frozen at answer-time (`selected_
  option.is_correct` at the moment the student picked it), never
  re-derived at finalization or later.
- Question deletion is already blocked when historically referenced
  (`QuestionViewSet.destroy()`), and import's "replace" duplicate action
  is already even more conservative (`question_is_referenced()`) —
  neither needed a Phase 8 change.

## 3. What was genuinely broken, confirmed with code references

1. `TestResultSerializer.get_questions()` read `obj.test.questions...` —
   **live** — so an edit to `Question.text`/`explanation`/`Option.text`/
   `Option.is_correct` after an attempt was finalized changed what that
   old attempt's review page showed, forever, immediately.
2. `QuestionAdminSerializer.update()` (`academics/serializers.py:290`)
   hard-deletes and recreates every `Option` row for **any** edit that
   touches options — `Answer.selected_option`/`QuestionAttempt.
   selected_option` (`SET_NULL`) go null on every such edit, destroying
   "what did the student pick" for every historical `Answer` on that
   question, platform-wide.
3. (Found during this phase's own audit, not in the original two
   problems) `TestAdminSerializer.update()`'s question-list replace
   (`tests_app/serializers.py`) deletes and recreates every `TestQuestion`
   row for a Test whenever its question list is edited — meaning a
   question removed/added/reordered after students already attempted the
   Test silently changed what those old attempts' review pages showed,
   not just each question's *content* but the *set and order* of
   questions itself.

## 4. Chosen architecture: immutable attempt-scoped snapshot

**Not** `Question`/`Option` versioning (rejected — platform-wide
footprint for an exam-review-scoped problem; see `QUESTION_VERSIONING_
DESIGN.md` §4 for the full trade-off analysis). One new model,
`tests_app.models.AttemptQuestionSnapshot` — one row per `(attempt,
question)`, created once inside `finalize_attempt()`'s existing
transaction, never updated afterward (immutable by omission — no code
path anywhere calls `.save()`/`.update()` on an existing row).

**Fields snapshotted, and why** — full table with rationale in
`QUESTION_VERSIONING_DESIGN.md` §5. In short: question text/latex/image,
solution content (explanation and its variants, key takeaway,
references), marks/negative_marks (display only — the real score is
already frozen elsewhere), `order` (this attempt's position, immune to
later `TestQuestion` reordering), `options_snapshot` (a JSON list — text,
image, correctness, explanation per option, since options are only ever
read as a unit alongside their question), and
`selected_option_original_id` — a **plain integer, not an FK** — the one
field that actually fixes problem 2, since a plain integer can never be
nulled by another table's `on_delete`.

**Deliberately not snapshotted**: `Test`-level config (the *scoring*
effect of `negative_marking`/`marks` is already frozen via the stored
score; `solutions_visibility` is a deliberate **live** Phase 7 policy
gate, not a content fact — freezing it would break "admin releases
solutions later"); `QuestionAttempt`/`QuestionEvent` (a different,
deliberately live/self-correcting QBank-mastery subsystem, not exam
review).

## 5. Write path

`tests_app/lifecycle.py: _create_question_snapshots(attempt)`, called
from `finalize_attempt()` immediately after `_score_and_rank()`, inside
the same `transaction.atomic()` block — either both the score and the
snapshot set commit together, or neither does. Reads `TestQuestion.
objects.filter(test=attempt.test).select_related(...).prefetch_related
('question__options')` (the authoritative question list *at this exact
moment*) cross-referenced with `attempt.answers.all()` (what was actually
picked, if anything) and `bulk_create()`s one snapshot row per question.
Runs for **every** finalization path uniformly — manual submit, request-
time auto-finalization, and the `finalize_expired_attempts` management
command — because all three already funnel through this one function
(Phase 6's own "one function, every caller" design paying off again).
Auto-submitted attempts get snapshots exactly like manual ones; nothing
in this function branches on `auto_submitted`.

## 6. Read path

`TestResultSerializer.get_questions()` gains one branch at the top:
`obj.question_snapshots.select_related('question').order_by('order')` —
if non-empty, render via the new `AttemptQuestionSnapshotResultSerializer`
(same output field names as `QuestionResultSerializer`, so the frontend
needed no changes for fields it already reads); if empty (every attempt
finalized before this phase shipped), fall through to the **exact,
byte-for-byte original** live-read code, unchanged. The wrong/correct
`?filter=` parameter and the `show_solutions`/`solutions_locked` gating
(Phase 7) work identically on both paths — `AttemptQuestionSnapshotResult
Serializer` reuses `QuestionResultSerializer._SOLUTION_QUESTION_FIELDS`/
`_SOLUTION_OPTION_FIELDS` directly rather than duplicating that list, so
the two can never independently drift on what counts as "solution
content."

Peer aggregate stats (`stats_available`/`students_correct_percent`/
`total_responses` — "what % of all students got this right") are
deliberately still read live off the `question` FK when it still exists
— these are inherently live, ever-changing aggregates, not a point-in-
time fact the way correctness/explanation are (see `QUESTION_VERSIONING_
DESIGN.md` §5). Unavailable (not fabricated) once the live `Question` is
gone.

## 7. Backward compatibility — the explicit, honest limitation

**No backfill migration.** `AttemptQuestionSnapshot`'s migration is
schema-only — no data migration touches a single existing row. Every
attempt finalized before this phase shipped has zero snapshot rows and
falls through to the unchanged live-read path — neither better nor worse
than its pre-Phase-8 behavior. This was a deliberate choice (`QUESTION_
VERSIONING_DESIGN.md` §7): backfilling from *current* content wouldn't
recover the *true* original content for anything already edited, and
would risk looking more authoritative than it actually is. **Genuine,
documented limitation**: a pre-Phase-8 attempt's historical content is
not reconstructable if its questions have since been edited — exactly as
true before this phase, now explicit instead of silently assumed away.

## 8. Import / authoring workflow impact

None. `QuestionAdminSerializer`, the Question Bank UI, and every import
path (`create_question_from_row`, skip/replace/keep_both) are completely
unchanged — confirmed by test (`ImportReplacementHistoricalIntegrityTests`)
that "replace" on a historically-referenced question still creates a new
question rather than touching the old one, exactly as before.

## 9. Security

No new write surface — `AttemptQuestionSnapshot` has no serializer field
that accepts client input anywhere, no ViewSet, no endpoint. Read only via
`attempt.question_snapshots`, itself only reachable through an already
ownership-checked `TestAttempt` (`get_object_or_404(..., user=request.
user)`), so there is no snapshot-id-based IDOR surface — nothing exposes
a raw snapshot id or accepts one as a request parameter. Phase 4's
`CanReview`/`CanViewSolutions` remain the only access-control layer;
Phase 8 changed *what content* those gates return, never *who* they
admit.

## 10. Performance

Write side: `finalize_attempt()` gains a small, bounded, constant number
of additional queries (the `TestQuestion`+`Question`+options fetch, one
`bulk_create`) — a low-frequency action (once per attempt), not a hot
path. Read side: for a snapshot-backed attempt, `get_questions()` now
issues *fewer* queries than the original live-read path (one denormalized
snapshot query replaces a question `select_related`+options `prefetch_
related` pair), since options are already inlined as JSON rather than
needing a separate prefetch. No query-count-pinned test exists for either
endpoint (confirmed by search), so there was no baseline to update.

## 11. Known limitations (stated plainly)

- Pre-Phase-8 attempts have no point-in-time content (§7) — an accepted,
  documented, unavoidable limitation, not a regression.
- Peer aggregate stats (§6) are not frozen — a deliberate choice, not an
  oversight.
- Image/media *files* are not duplicated, only their resolved URL/variant
  data at snapshot time (`resolve_image_data`'s output) — if the
  underlying file or `MediaAsset` is later deleted independently, the
  snapshot's image reference would break too. No evidence in the audit
  that image files are ever deleted the destructive way `Option` rows are;
  full asset versioning was judged out of scope for this phase.
- `QuestionAttempt`/`QuestionEvent` (QBank mastery tracking) still have
  the same `selected_option` `SET_NULL` exposure as before this phase —
  explicitly out of scope (a different subsystem, not exam-attempt
  review; see `QUESTION_VERSIONING_DESIGN.md` §5).
