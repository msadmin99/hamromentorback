# Phase 5 — Exam Type Policies & Unified Exam Configuration — Completion Report

**Status:** Complete. Awaiting phase validation before Phase 6.

Companion document: `PHASE5_AUDIT_AND_ARCHITECTURE.md` (full pre-implementation
audit + chosen architecture, referenced throughout instead of repeated here).

---

## 1. What `implementation-plan.md` Phase 5 specified

"Exam Type Policies — default policy templates for Practice/Mock/Daily/Grand/
Past Year, admin-overridable. A `ExamTypePolicy` (or similar) default-value
set per `exam_type`, consumed by both `CreateExamWizardShell`/`TestConfigStep`
… and the Import-and-create-exam flow. Encodes exactly the five policy
tables the spec lays out as data, not scattered conditionals. Tests
required: regression on the existing `TestAdminSerializer`/`TestConfigStep`
default-value tests plus new tests confirming each exam_type's template is
actually applied and is admin-overridable." Treated as the sole source of
truth throughout, per the governing prompt's own instruction.

## 2. Pre-implementation audit findings

Full detail in `PHASE5_AUDIT_AND_ARCHITECTURE.md` §1. Headline finding: the
backend (`Test` model, `TestAdminSerializer`, `TestViewSet`,
`ImportBatchCreateTestView`) had **zero exam-type-aware default logic
anywhere** — no "five policy tables," not even scattered conditionals to
unify. 100% of what a new exam "started out looking like" was two
independently hand-coded frontend JS objects
(`emptyForm()`/`defaultConfig()`), which both frontends always submitted in
full, making the backend's own model-level field defaults dead code in
practice for both flows.

## 3. Duplicate/default sources discovered

Exactly two: `Admin/src/app/exam-management/page.js: emptyForm()` (Create/
Edit) and `Admin/src/components/import/TestConfigStep.js: defaultConfig()`
(Import). Repo-wide search found no third source. `TestViewSet.duplicate()`/
reschedule copy an existing Test's actual stored values, not template
defaults — unaffected, out of scope.

## 4. Final architecture chosen

`Policy → defaults applied once at Test-creation time → stored on the Test
row`. No FK from `Test` to the policy; a policy change never touches an
already-created Test. Full reasoning (including rejected alternatives —
Python constants, a new Admin-panel UI screen) in
`PHASE5_AUDIT_AND_ARCHITECTURE.md` §3.

## 5. Policy representation

New model `ExamTypePolicy` (`tests_app/models.py`) — one row per
`exam_type` (5 rows total, `exam_type` as primary key), admin-editable via
bare Django admin only (`tests_app/admin.py: ExamTypePolicyAdmin`), matching
the Phase 2 `FreeStarterPolicy` precedent. No new Admin-panel (Next.js) UI
screen was built — judged unnecessary scope. Sole read path:
`tests_app/policy.py: get_exam_type_defaults()` /
`get_all_exam_type_defaults()`.

## 6. Exam categories supported

All five: `qbank` (Practice/Question Bank), `daily`, `mock`, `grand`, `pyq`
— exactly `Test.EXAM_TYPE_CHOICES`, reused directly (`ExamTypePolicy.
EXAM_TYPE_CHOICES = Test.EXAM_TYPE_CHOICES`), no separate enum invented.

## 7. Admin override behavior

Two independent, both real and tested:
- **Category-level**: editing an `ExamTypePolicy` row (Django admin, or the
  ORM) changes what the *next* exam of that category receives —
  `ExamCreationAppliesPolicyDefaultsTests.test_admin_can_customize_a_
  category_and_new_exams_pick_it_up`.
- **Instance-level**: an admin's explicit field value in the Create/Edit
  payload always wins over the category policy, at creation and forever
  after (a later policy change never reaches back into that specific
  exam) — `ExamCreationExplicitValueOverridesPolicyTests`,
  `ExamPolicyOverrideSurvivesLaterPolicyChangeTests` (the spec's mandatory
  Override Test, exact steps).

## 8. Create/Edit/Import integration

- **Create/Edit** (`TestViewSet` → `TestAdminSerializer`): `create()` now
  fills any policy-controlled field absent from the payload with that
  category's policy default before creating the row; `update()` is
  untouched — never reads policy, per the immutability requirement.
- **Import** (`ImportBatchCreateTestView` → same `TestAdminSerializer`):
  automatically covered, since it already shares the identical serializer/
  `create()` path — no separate wiring needed.
- **Frontend**: `emptyForm()` and `defaultConfig()` both now fetch
  `GET /tests/exam_type_policies/` and merge the canonical values over
  their own (now fallback-only, network-failure-only) local defaults, so
  the two admin UIs are no longer two independently-drifting sources.

## 9. Backend files changed

- `tests_app/models.py` — added `ExamTypePolicy`.
- `tests_app/policy.py` — **new** — `POLICY_CONTROLLED_FIELDS`,
  `get_exam_type_defaults()`, `get_all_exam_type_defaults()`.
- `tests_app/serializers.py` — `TestAdminSerializer.create()` applies
  policy defaults for omitted fields; `update()` unchanged.
- `tests_app/views.py` — new `TestViewSet.exam_type_policies` read-only
  action.
- `tests_app/admin.py` — registered `ExamTypePolicyAdmin`.
- `tests_app/migrations/0018_examtypepolicy.py`,
  `0019_seed_examtypepolicy.py` — schema + seed (5 rows).
- `tests_app/tests.py`, `academics/tests.py` — new tests (§15).
- `docs/PHASE5_AUDIT_AND_ARCHITECTURE.md`, `docs/PHASE_5_COMPLETION_REPORT.md` — new.

## 10. Frontend files changed

- `Admin/src/app/exam-management/page.js` — `emptyForm()` accepts an
  optional policy overlay; fetches `/tests/exam_type_policies/` once on
  mount; both `setForm(emptyForm(...))` call sites now pass the fetched
  policy for the relevant `exam_type`.
- `Admin/src/components/import/TestConfigStep.js` — `defaultConfig()`'s
  `is_draft` fallback flipped `false → true` (the canonical resolution);
  a mount effect fetches the same endpoint and merges it into the initial
  form state, skipped entirely when resuming an already-configured draft
  (`initialConfig` present).

No other frontend surface touched (no Student Dashboard, Exam Dashboard,
QBank UI, Subscription/Commerce UI, or mobile changes — none were in scope).

## 11. APIs changed

One new endpoint: `GET /api/tests/exam_type_policies/` (staff-only,
`IsAdminUser`) → `{qbank: {...}, daily: {...}, mock: {...}, grand: {...},
pyq: {...}}`, each value containing exactly the 11
`POLICY_CONTROLLED_FIELDS`. No existing endpoint's URL, request shape, or
response shape changed. `POST /tests/` and `POST /import-batches/<id>/
create-test/` behavior is unchanged for every caller that (like both real
frontends) sends every field explicitly; only a caller that omits a
policy-controlled field sees new behavior (it now receives the category's
policy default instead of the raw model default — for every field except
`is_draft`, on the initial seed, these are numerically identical anyway).

## 12. DB changes / migrations

One schema migration (`0018_examtypepolicy` — new table, 12 columns
including PK) + one data migration (`0019_seed_examtypepolicy` — inserts
exactly 5 rows, `RunPython` with a real reverse operation that deletes
those 5 rows and nothing else). Verified: applies cleanly against the
existing, already-migrated local dev DB (`db.sqlite3`) with zero errors;
also implicitly verified against a fresh DB, since the Django test runner
builds one from scratch and applies every migration before running the
suite (730/730 passed). `makemigrations --check --dry-run` → "No changes
detected", confirmed both before and after the full suite run. No
production database was touched — this repository is not connected to
Cloud SQL from this environment.

## 13. Security changes

No new attack surface beyond one new read-only, staff-gated endpoint
(`IsAdminUser`, verified with an explicit 403-for-student and
401/403-for-anonymous test). No new write endpoint — the only way to
change a policy is Django admin (already staff-only by construction) or a
direct ORM/database action. `TestAdminSerializer.create()`'s new logic
only ever *fills in a gap* in the payload; it never reads a client-supplied
value for a field it wasn't explicitly given, and never overrides an
explicit value — confirmed by
`ExamCreationExplicitValueOverridesPolicyTests`. No change to Phase 4's
access engine, no new bypass of `can_access_test`/`billing.access`
functions — this phase never touches the attempt/result/access path at
all.

## 14. Performance impact

`TestAdminSerializer.create()` gains exactly one additional query (a
single indexed primary-key lookup, `ExamTypePolicy.objects.get(pk=exam_type)`)
per Test creation — an admin/staff action, never a hot student-facing path,
and never invoked more than once per create request. No new query on any
list/detail/attempt/result endpoint; `ExamTypePolicy` is read in exactly
two places (`create()`, the new endpoint), both confirmed via
`tests_app/policy.py`'s module docstring and a repo-wide grep for its
importers. No existing query-count-pinned test needed updating — there was
none covering `POST /tests/`, and the full suite's other query-count
assertions (Test list/detail, performance aggregation, etc.) were
unaffected, confirmed by all of them still passing.

## 15. Tests added

**30 new tests**, all passing:

- `tests_app/tests.py` (25): `ExamTypePolicyModelAndServiceTests` (5),
  `ExamTypePoliciesEndpointTests` (4 — staff/student/anonymous/live-edit),
  `ExamCreationAppliesPolicyDefaultsTests` (6 — one per exam category + the
  admin-customization case), `ExamCreationExplicitValueOverridesPolicyTests`
  (3), `ExamTypePolicyImmutabilityTests` (1 — the mandatory Policy
  Immutability Test, exact spec steps), `ExamPolicyOverrideSurvivesLater
  PolicyChangeTests` (1 — the mandatory Override Test, exact spec steps),
  `ExamEditNeverAppliesPolicyDefaultsTests` (1), `IsDraftDriftResolution
  Tests` (2), `ExamTypePolicyBackwardCompatibilityTests` (2, including an
  embedded `makemigrations --check` sanity test).
- `academics/tests.py` (5): `Phase5ConfigurationConsistencyTests` — the
  mandatory Configuration Consistency Test, one per exam category, each
  creating an exam via the real Create endpoint and the real Import &
  Create Test endpoint with equivalent minimal input and asserting every
  policy-controlled field matches.

## 16. Full test-suite result

```
cd Backend && python manage.py test
```

**730/730 tests pass** (baseline after Phase 4: 700; this phase: +30 new,
zero regressions, zero pre-existing tests modified). The `redis down` /
Cloud Tasks `DefaultCredentialsError` tracebacks visible in the run's
output are pre-existing, deliberately-injected failure-path tests
(resilience/fallback coverage unrelated to this phase) — not real
failures; the run's final result is `OK`.

## 17. Query-count changes

None. See §14 — no pinned query-count assertion exists for the one path
that gained a query (`POST /tests/`), and every existing pinned assertion
elsewhere in the suite still passes unchanged.

## 18. Compatibility risks

None identified. Every existing Test row is completely unaffected —
`ExamTypePolicy` has no relationship to `Test` at the database level, and
is never read on any read path. The one deliberate, documented behavior
change (`is_draft`'s canonical default, both frontends and the backend
fallback, now resolve to `True`/draft for every category) only affects
brand-new exams created after this deploy via a flow that happens to omit
`is_draft` from its payload — today, no such flow exists (both real
frontends always send it explicitly) — so this is a zero-observed-impact
change at ship time, with the corrected behavior tested and ready for any
future caller.

## 19. Deferred issues (explicitly, not silently)

- **Assignment fields** (`assigned_students`/`assigned_batches`) are not
  part of the policy — `implementation-plan.md`'s Phase 5 text doesn't
  require it, and the Import flow has no assignment UI at all. Left as-is,
  matching the governing prompt's own "if not required in Phase 5, document
  as deferred" instruction.
- **Seed values carry no per-category differentiation** — every field
  except `is_draft` is seeded identically across all five categories,
  because the audit found no existing differentiated behavior to preserve
  and inventing new business numbers (e.g. category-specific negative-
  marking fractions) would be guessing product policy. Differentiating
  categories is now a pure Django-admin operation, not a code change — this
  is the actual capability delivered, not a set of pre-baked numbers.
  Product should populate these once real category-specific values are
  decided.
- Every item the governing prompt explicitly excluded from Phase 5 (full
  scheduling overhaul, auto-submit, Daily/Grand registration redesigns,
  question/result versioning, refund-to-entitlement revocation redesign,
  subscription/combo redesign, dashboard redesign) — untouched, not started.
- Pre-existing, still-open items carried forward unchanged from Phase 4
  (`solutions_visibility` enforcement, `can_access_test`/
  `visible_test_queryset` unification, `_start_attempt`'s separate code
  path) — none were touched or needed for Phase 5 to function.

## 20. Deployment readiness

**Not deployed.** No persistent/production database was modified — all
changes are local working-tree edits (this repository is not a git
repository, consistent with every prior phase) plus one local SQLite dev-DB
migration apply, used only to verify migration correctness. Deployment
(backend + frontend + running the migration against the real Cloud SQL
instance) requires separate explicit approval, as always.

---

Phase 5 complete. Stopping here per the mandatory stop, awaiting validation before Phase 6.
