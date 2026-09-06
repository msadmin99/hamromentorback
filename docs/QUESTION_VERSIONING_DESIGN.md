# Question Versioning / Historical Integrity — Design Document

Written before any migration, per `implementation-plan.md`'s Phase 8
requirement. Covers the audit, the versioning-vs-snapshot-vs-hybrid
analysis, and the chosen architecture. `PHASE_8_ARCHITECTURE.md` is the
as-built reference; this document is the design review that preceded it.

---

## 1. The two confirmed problems (re-verified against current code, not assumed)

**Problem 1 — in-place edits change historical display.**
`QuestionAdminSerializer.update()` (`academics/serializers.py:281-293`)
mutates `Question` fields in place (`setattr` + `instance.save()`) with no
history of any kind. `TestResultSerializer.get_questions()`
(`tests_app/serializers.py`) builds its review payload from
`obj.test.questions.select_related(...)` — a **live** query against the
current `Question`/`Option` rows. A finalized attempt's `TestAttempt.score`
is safely frozen (see §2), but the *displayed* question text, options,
correctness marking, and explanation are not — editing a question after
students have attempted it immediately and permanently changes what every
past attempt's review page shows, even though the frozen score no longer
matches what's displayed.

**Problem 2 — option edits null the historical selection.**
`QuestionAdminSerializer.update()` line 290: `instance.options.all()
.delete()` then recreates fresh `Option` rows for **any** edit that
touches the options list (including a one-character text fix to a single
option — there is no partial-update path). `Answer.selected_option`
(`tests_app/models.py:394`) and `QuestionAttempt.selected_option`
(`academics/models.py:337`) are both `on_delete=SET_NULL` — every option
edit nulls the historical "what did the student pick" reference on every
`Answer`/`QuestionAttempt` row for that question, platform-wide.

## 2. What's already safe (confirmed by re-reading the code, not assumed)

- **`TestAttempt.score`/`rank`/`percentile`/`accuracy`** are computed once
  by `finalize_attempt()` (Phase 6) and never recomputed. `Answer.
  is_correct` is a plain `BooleanField`, set once when the student answers
  (`SubmitAnswerView`) from `selected_option.is_correct` **at that moment**
  — never re-derived at finalization or later. The numeric score is
  already historically accurate as of answer-time; nothing in Phase 8
  touches scoring.
- **Question deletion is already blocked** when historically referenced —
  `QuestionViewSet.destroy()` (`academics/views.py:492-515`) refuses to
  delete a question with `QuestionAttempt` history or used in a Test with
  student attempts. No bulk-delete path bypasses this (confirmed by
  repo-wide search — this is the only question-delete endpoint).
- **Import "replace" is already conservative** — `question_is_referenced()`
  (`academics/import_engine.py:47-50`, checked from
  `_create_questions_for_test`, `academics/import_views.py:533-537`) blocks
  deleting the old question if it's used in *any* Test or has *any*
  `Answer` at all (a stricter check than the delete endpoint's own),
  falling through to creating a new question instead. Skip/keep_both never
  touch the original question.
- **`TestQuestion.order`** has no dedicated reorder endpoint — the only
  write path is `TestAdminSerializer.update()`'s question-list replace
  (`tests_app/serializers.py:313-318`), which deletes and recreates every
  `TestQuestion` row for the Test whenever `question_ids` is included in
  an edit payload.

## 3. The real, previously-unaddressed gap this surfaces

`TestAdminSerializer.update()`'s question-list replace (§2) means editing
*which questions a Test contains* (adding, removing, or reordering) after
students have already attempted it silently changes what `test.questions
.all()` returns for **every past attempt's review**, not just future ones
— a removed question vanishes from old students' review pages despite
having contributed to their score; a newly-added question would appear as
an "unanswered" item those students never actually saw. This was not
explicitly named in the original two evidenced problems but follows
directly from the same live-read architecture, and any fix for Problem 1
must also cover it (the question *set*, not just each question's
*content*, must be historically fixed).

## 4. Versioning vs. snapshot vs. hybrid

**A. Immutable Question/Option versioning** (`Question → QuestionVersion →
OptionVersion`, `TestQuestion` points at a specific version). Rejected.
The footprint is platform-wide, not exam-review-scoped: QBank browsing/
search, `QuestionViewSet`, import/duplicate-detection, `QuestionBankConfig`
thresholds, Smart Practice, `chapter_breakdown`/`topic_mastery`,
`Question.total_attempts`/`correct_attempts` live stat updates — every one
of these would need to become version-aware, for a problem that is
actually scoped to "what does a *finalized attempt's review* show."
Migration and query complexity are both large; authoring workflow impact
is real (every edit becomes "which version am I looking at"). This is not
the smallest architecture for the actual requirement.

**B. Immutable attempt snapshot** (capture the needed content once,
scoped to the attempt). Chosen. Touches only the exam-attempt/review path:
one new model, one write point (`finalize_attempt()`, already the single
place scoring happens), one read point (the result serializer). QBank,
admin editing, import, search, and stats continue working against live
`Question`/`Option` exactly as today — zero changes required there,
directly satisfying "preserve existing models where possible" and
"additive historical layer."

**C. Hybrid** (versioned *identity* + snapshotted *display content*) was
considered and reduces to B in practice: the identity that actually needs
to survive editing/deletion is the *original option's primary key* (to
know which one was selected), which a plain, non-FK integer field already
provides without needing a whole parallel versioning system. No additional
mechanism earns its complexity here.

**Trade-off summary for B (chosen):**
- *Duplication*: one row per (attempt, question), each carrying a copy of
  display-relevant content — bounded (roughly the same order of magnitude
  as `Answer`, which already exists at that cardinality), not unbounded.
- *Migration complexity*: additive only — one new table, no changes to
  existing tables, no data backfill (see §7).
- *Query complexity*: the review serializer gains one branch (snapshot
  exists → read snapshot; else → existing live-read, unchanged) instead of
  a version-resolution join everywhere.
- *Storage cost*: proportional to (finalized attempts × questions per
  test) — the same order as `Answer`, already accepted at that scale.
- *Historical correctness*: complete for every field actually snapshotted
  (§5); explicitly does not (and cannot) reconstruct pre-migration
  attempts (§7).
- *Authoring workflow*: **zero impact** — `QuestionAdminSerializer`,
  the Question Bank UI, and import are entirely unchanged. An admin still
  edits a question in place, exactly as today; the snapshot exists purely
  as a side effect of a *later* attempt's own finalization already having
  happened *before* that edit.
- *Import impact*: none — `create_question_from_row`/`question_is_
  referenced` are unchanged.
- *Grading impact*: none — `finalize_attempt()`'s scoring math is
  unchanged; snapshot creation is purely additive within the same
  transaction.
- *Result rendering impact*: the result serializer gains a snapshot-or-
  live-fallback branch (§8) — additive, existing response shape preserved.
- *Backward compatibility*: full — see §7.

## 5. Exactly what is snapshotted, and why

Per question, at the moment of finalization:

| Field | Why |
|---|---|
| `order` | The question's position in *this* attempt's test, independent of later `TestQuestion` reordering (§2/§3). |
| `text`, `latex`, `image_data` (resolved `{url,variants,width,height}`, not a raw FK) | What the student actually read. `image_data` is pre-resolved so display never depends on `image_asset` surviving. |
| `explanation`, `explanation_latex`, `explanation_image_data`, `explanation_video_url`, `key_takeaway`, `references`, `reference_book_name`/`edition`/`chapter`/`page`/`url` | The solution content Phase 7's `CanViewSolutions` gates access to — Phase 8 doesn't change *who* can see it, only *what* is shown once allowed (per the kickoff prompt's own "Phase 8 changes WHAT historical content is returned, not WHO is allowed to access it"). |
| `marks`, `negative_marks` | Informational display only — the *actual* score was already computed from whatever these were at finalization time (§2) and is never recomputed from this snapshot. |
| `subject_name` | Display label already shown in review. |
| `options_snapshot` (JSON list: `{id, text, image_data, latex, is_correct, explanation, order}` per option) | The full historical option set — content *and* correctness — as it existed at finalization. A JSON field, not a child table: options are only ever read as a unit alongside their question, never queried independently, so a table+FK graph would add real complexity for no benefit here. |
| `selected_option_original_id` (plain integer, **not** an FK) | The one field that actually solves Problem 2 — a plain integer can never be nulled by another table's `on_delete`, unlike `Answer.selected_option`. Matched against `options_snapshot[i]['id']` at read time to know which option (by original identity) was picked, independent of whether the live `Option` row still exists. |

**Deliberately not snapshotted** (per "do not snapshot irrelevant data
merely for completeness"):
- `Question.tags`, `instructor_difficulty`/`actual_difficulty`,
  `question_type`, SEO fields, `public_id`/`slug` — authoring/catalog
  metadata, never shown on a result/review page.
- Any `Test`-level field (`duration_minutes`, `negative_marking`,
  `solutions_visibility`, `shuffle_*`, `max_attempts`, `title`) — the
  *scoring-relevant* ones (`negative_marking`, `marks`) are already safe
  because the score itself is frozen (§2), not because the config is
  snapshotted; `solutions_visibility` is a deliberate **live** policy gate
  per Phase 7's own architecture (an admin can release solutions after the
  fact — that must keep working, so it must stay live, not frozen);
  `title`/`shuffle_*`/`max_attempts` are cosmetic or not surfaced on the
  result view at all today. Re-litigated explicitly, not silently skipped.
- `QuestionAttempt`/`QuestionEvent` (the QBank mastery/mistake-bank
  tables) — a deliberately different, self-correcting, live-tracking
  subsystem (documented in their own model docstrings as holding "the
  latest state," not a historical exam record). Out of Phase 8's scope,
  which is specifically finalized *Test attempt* review, not QBank
  practice history.

## 6. Immutability rule

`AttemptQuestionSnapshot` rows are created exactly once, inside
`finalize_attempt()`'s existing transaction, and **never updated
afterward** — no update path is ever written for this model (enforced by
simply never calling `.save()`/`.update()` on an existing row anywhere in
the codebase, the same "immutable by omission" pattern already used for
`QuestionEvent`). `finalize_attempt()`'s own idempotency guard (Phase 6:
`if locked.status != 'in_progress': return locked`) already prevents a
second finalization attempt from running the snapshot step twice.

## 7. Backward compatibility — no backfill, explicit fallback

**Decision: do not backfill snapshots for attempts finalized before this
migration.** Reasoning: the only content available to backfill *from* is
today's **current** `Question`/`Option` state — which, for any question
already edited since the original attempt, is exactly the same
already-drifted content the existing (pre-Phase-8) live-read already
shows. A backfill would not recover the *true* original content (already
irrecoverably lost for any question edited before this migration ships)
— it would only freeze "whatever is live at migration time" a little
earlier than "whatever is live at read time," a marginal, misleading
gain dressed up as real historical accuracy. Per the plan's own explicit
instruction — "do NOT fabricate historical content" — manufacturing a
snapshot that looks authoritative but isn't would be worse than being
honest about the limitation.

**What actually happens**: `AttemptQuestionSnapshot` is a purely additive
table (one migration, no data migration). Every attempt finalized *after*
this phase ships gets real, accurate snapshots. Every attempt finalized
*before* has none. The result serializer (§8) checks
`attempt.question_snapshots.exists()` and falls back to the exact
pre-Phase-8 live-read code path, completely unchanged, for old attempts —
so their review behavior is neither better nor worse than it already was,
and no old attempt is ever hidden, broken, or shown fabricated data.

This is a genuine, documented limitation: **pre-Phase-8 historical
attempts do not have reconstructable point-in-time content** if their
questions have since been edited. This was already true before this
phase (nothing regresses) and is now explicitly documented rather than
silently assumed away.

## 8. Read path

`TestResultSerializer.get_questions()` gains one branch at the top:
`snapshots = list(obj.question_snapshots.order_by('order'))`; if
non-empty, render via a new `AttemptQuestionSnapshotResultSerializer`
(same Phase 7 `show_solutions`/`solutions_locked` gating, same output
field names as `QuestionResultSerializer` so the frontend needs no
changes for the fields it already reads); if empty, fall through to the
existing `QuestionResultSerializer`/live-query code, byte-for-byte
unchanged. Phase 7's enforcement architecture (gating inside the
serializer, not per-view) is preserved exactly — Phase 8 adds a content
source, not a second access-control system.

## 9. What Phase 8 does not build

No `QuestionVersion`/`OptionVersion` model. No change to
`QuestionAdminSerializer`, the Question Bank UI, or the import pipeline's
create/replace/skip/keep_both semantics. No backfill migration. No change
to scoring, ranking, or analytics aggregation. No new permission system —
Phase 4's `CanReview`/`CanViewSolutions`/`CanViewRank`/`CanViewAnalytics`
remain the only access-control layer; snapshots are read-only data behind
those existing gates.
