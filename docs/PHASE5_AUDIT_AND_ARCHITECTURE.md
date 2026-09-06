# Phase 5 — Pre-Implementation Audit & Architecture

Companion to `implementation-plan.md` (Phase 5 source of truth) and
`PHASE_5_COMPLETION_REPORT.md`.

## 1. Audit findings (Step 2/3 — re-verified against live code, not prior docs)

**Backend has zero exam-type-aware default logic.** `Test` model field
defaults (`tests_app/models.py`) are flat and uniform regardless of
`exam_type`: `is_draft=True`, `duration_minutes=60`, `questions_per_page=1`,
`negative_marking=True`, `shuffle_questions=True`, `shuffle_options=True`,
`max_attempts=1`, `solutions_visibility='auto'`, `is_pro=False`,
`free_preview_questions=0`. `TestAdminSerializer` (`tests_app/serializers.py`)
— confirmed still the single shared contract for both `TestViewSet`
(Create/Edit) and `ImportBatchCreateTestView` (Import) — contains **no**
`if exam_type == ...` branching anywhere. There is no "five policy tables"
logic anywhere server-side today; the exam-category descriptions in the
Phase 5 prompt (Practice/Mock/Daily/Grand/PYQ) do not correspond to any
existing differentiated code path.

**Both admin frontends always submit a complete, fully-populated payload**
(confirmed by reading `save()` in `Admin/src/app/exam-management/page.js`
and the `POST /import-batches/<id>/create-test/` call in
`Admin/src/app/questions/import/page.js`) — every field is explicitly set
from local component state before the request, every time. This means the
`Test` model's own field defaults are **effectively dead code** for both
flows today: 100% of what a new exam "starts out looking like" is
determined by two independently hand-coded JS objects:

- `Admin/src/app/exam-management/page.js: emptyForm(examType)` (Create/Edit wizard)
- `Admin/src/components/import/TestConfigStep.js: defaultConfig(batch)` (Import & Create Test)

**Confirmed drift #1 (behavioral, real):** `is_draft` — `emptyForm()` →
`true` (draft), `defaultConfig()` → `false` (publish immediately). Both are
user-editable via a UI control before submit (`TestConfigStep.js` has a
draft/publish radio pair) — this is a **default-only** drift, not a missing
feature.

**Confirmed drift #2 (structural, expected/deferred):** `emptyForm()`
additionally carries `subject`, `assigned_students`, `assigned_batches`,
and a UI-only `program` field that `defaultConfig()` doesn't have — the
Import flow has no assignment UI. `implementation-plan.md`'s Phase 5 text
does not mention unifying assignment; this is deferred, not fixed (see
Completion Report §19).

**No other duplicate default source exists.** Repo-wide search for
hardcoded exam-field defaults (`duration_minutes:`, `questions_per_page:`,
`max_attempts:`) found only these same two files (a third match in
`Admin/src/app/videos/page.js` is an unrelated video-duration field).
`TestViewSet.duplicate()`/reschedule (`clone_test_as_new_version`) copy an
**existing Test's actual stored values**, never default-template values —
out of scope, unaffected by this phase.

**`is_draft`/`questions_per_page` re-verification:** `questions_per_page`
default is uniformly `1` in the model and in both JS files today — no
discrepancy currently observed in code (the previously-noted "1 vs 10" gap
remains a data-instance artifact, not a code defect; unchanged this phase).
`is_draft`'s drift is real and resolved below.

## 2. "Before Phase 5" architecture summary (Step 4)

```
Create Exam Wizard          Import & Create Test
(emptyForm(), JS-hardcoded) (defaultConfig(), JS-hardcoded)
        |                            |
        |   [[ NO SHARED DEFAULT SOURCE — pure coincidence where they agree ]]
        |                            |
        v                            v
        POST /tests/          POST /import-batches/<id>/create-test/
        TestAdminSerializer   TestAdminSerializer  <- same backend class,
                                                        zero policy logic
                    Test.objects.create(**validated_data)
```

Server-side model defaults exist but are never actually reached by either
production entry point, since both always send every field explicitly.

## 3. Chosen architecture (Step 5)

**New DB-backed model, `ExamTypePolicy`** — one row per `exam_type` (5
rows), admin-editable via bare Django admin. Rejected alternatives:

- **Backend Python constants** — rejected because the spec's own mandatory
  Policy Immutability Test requires *changing* a category's policy as a
  live operation ("change Mock policy to config B") without a redeploy;
  a DB row supports this directly and testably, a constants module does
  not.
- **New Admin-panel (Next.js) UI screen** — rejected as unnecessary scope;
  bare Django admin is the established Phase 2/3 precedent
  (`FreeStarterPolicy`) for exactly this category of decision ("admin-
  configurable business policy, no new custom UI needed").
- **Reusing/extending an existing model** — none fits; `Test` itself is
  instance data, not a template, and no existing model represents
  "defaults per exam_type."

This is the smallest model that gives one source of truth + admin
override + safe defaults + backward compatibility + testability, matching
the Phase 2 precedent's own reasoning.

**Policy fields** (only fields explicitly named in the Phase 5 test list
and confirmed to exist as real, configurable `Test` fields):
`default_duration_minutes`, `default_questions_per_page`,
`default_negative_marking`, `default_shuffle_questions`,
`default_shuffle_options`, `default_max_attempts`,
`default_solutions_visibility`, `default_is_draft`, `default_is_pro`,
`default_free_preview_questions`, `default_price`.

Deliberately **excluded** from the policy (remain pure per-instance data,
never templated): `title`, `description`, `difficulty`, `subject`,
`courses`, `assigned_students`, `assigned_batches`, `academic_year`,
`university`, `access_password` (a shared default password across every
exam of a category would be a security anti-pattern), `is_new` (cosmetic,
no per-category meaning, unchanged in either existing JS object). No
"total marks" field exists on `Test` at all — marks are derived from
selected questions (`get_total_marks`, a `SerializerMethodField`) — so the
prompt's explicit "do not hard-code 200 marks" warning is satisfied by
construction: no marks field is added.

**Seed values:** since the audit found **no existing differentiated
per-category behavior anywhere** to preserve, seeding five categories with
invented, differentiated numeric defaults (e.g. a specific negative-marking
fraction per category) would be guessing business policy — explicitly
forbidden. The one deliberate seed decision is `default_is_draft = True`
for all five categories, resolving drift #1 in favor of the safer,
already-dominant choice (matches the `Test` model's own default, matches
`emptyForm()`, matches the model's own help_text calling `True` "the
default for every new exam", and matches this platform's established
fail-closed-by-default precedent for `Test.courses`). Every other field is
seeded at today's actual shared value (`duration_minutes=60`,
`questions_per_page=1`, `negative_marking=True`, `shuffle_questions=True`,
`shuffle_options=True`, `max_attempts=1`,
`solutions_visibility='auto'`, `is_pro=False`,
`free_preview_questions=0`, `price=None`) — a genuine **zero-behavior-change**
launch for every field except the one documented, deliberate, tested
`is_draft` resolution. Admins can differentiate categories after launch
via Django admin — that is the actual value this phase delivers, not an
invented set of category-specific numbers.

**Consumption model:** `Policy → defaults applied at Test-creation time →
stored on the Test row`. `ExamTypePolicy` is never referenced again after
a `Test` is created — no FK from `Test` to `ExamTypePolicy`, no read of
the policy table on any read/list/attempt/result path. Changing a policy
row only affects the next `Test` created after the change.

**Backend authority:** `TestAdminSerializer.create()` (create only, never
`update()`) fills in the policy default for any policy-controlled field
**absent from the incoming payload**, before falling through to the raw
model field default. Today's two known frontends always send every field
explicitly, so this is currently dormant for them (their own values win,
which is correct — an admin's explicit per-exam choice must never be
silently overridden) — but it makes the backend genuinely authoritative
for any other/future caller, per the spec's "never trust client-supplied
default values" instruction, rather than that guarantee living only in
frontend fetch logic.

**Frontend integration:** `emptyForm()` and `defaultConfig()` both fetch
the canonical template from a new read endpoint instead of hardcoding
values, keeping a small local object only as an offline/loading-state
fallback (clearly commented as fallback-only).

**New endpoint:** `GET /tests/exam_type_policies/` (staff-only,
`@action(detail=False)` on `TestViewSet` — the same house pattern already
used for `universities`/`years`/`browse`/`stats`) → `{mock: {...}, daily:
{...}, ...}`. Read-only; the write path is Django admin only, per the
Phase 2 precedent (no new write API needed).
