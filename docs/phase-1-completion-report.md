# Phase 1 — P0 Security — Completion Report

**Status:** Complete. Awaiting phase validation before Phase 2 begins.

---

## Scope

Per the modernization spec, Phase 1 is scoped strictly to **authorization defects** (the three named findings plus "other confirmed P0 authorization defects" from the audit's own P0 list). Data-integrity and missing-feature P0 items (entitlement expiry enforcement, `TestAttempt` timeout, refund system, coupon race condition, `CRON_SECRET` hardening, `Question` option-edit history loss) are explicitly out of scope for this phase — they are sequenced into Phases 2, 7, 9, and 10 in `implementation-plan.md`, matching the spec's own phase list.

## Fixes applied

### 1. Unauthenticated exam-session participant PII leak

**File:** `Backend/tests_app/views.py`, `ExamSessionViewSet.attempts`.
**Before:** no `permission_classes` override — inherited the viewset's class-level `IsStaffOrReadOnly`, whose `SAFE_METHODS` bypass made this GET-only action return participant names, emails, scores, and ranks to **any caller, authenticated or not**, who knew or guessed a session ID.
**Fix:** explicit `permission_classes=[IsAdminUser]` override, matching the same convention already used by `.browse`/`.stats`/`.stats_by_program` elsewhere in the same file.
**Tests:** `ExamSessionAttemptsPermissionTests` (3 new tests: anonymous rejected, authenticated non-staff rejected, staff succeeds).

### 2. Same pattern, lower severity

**File:** `Backend/tests_app/views.py`, `ExamTemplateViewSet.sessions`.
**Before:** identical gap — no permission override, `IsStaffOrReadOnly` bypassed by `SAFE_METHODS`. Exposes schedule metadata (no participant PII) rather than student data.
**Fix:** same `[IsAdminUser]` override.
**Tests:** `ExamTemplateSessionsPermissionTests` (3 new tests).

### 3. Billing authorization gap

**File:** `Backend/billing/views.py`, `PurchaseViewSet.approve` / `.reject` / `.request_resubmission` / `.audit_log`.
**Before:** all four used DRF's bare `IsAdminUser` (checks only `is_staff`) — the only four sensitive billing endpoints in the file that didn't use this app's own `IsAdminRoleOrAbove` (used by every sibling endpoint: `CouponViewSet`, `ScholarshipViewSet`, `GrantAccessView`, `AnalyticsView`). An Editor or Teacher-role account, explicitly excluded from the `billing` feature everywhere else in the product, could approve/reject real payments and read the payment audit trail via direct API calls.
**Fix:** all four switched to `IsAdminRoleOrAbove`. Removed the now-unused `IsAdminUser` import from `billing/views.py`.
**Tests:** `PurchaseApprovalRoleGateTests` (5 new tests: Editor blocked on approve/request-resubmission/audit-log, Teacher blocked on reject, admin_role='admin' still succeeds).

### 4. `TestViewSet` — no role-level enforcement on delete/reschedule

**File:** `Backend/tests_app/views.py`, `TestViewSet.destroy` and `.reschedule`.
**Before:** plain `IsStaffOrReadOnly` — any staff account, including Editor/Teacher, could delete or reschedule any exam via direct API, despite the Admin UI already hiding these buttons for such roles (`hasFeature(user, "exam_delete"/"exam_schedule")` in `ExamTable.js`).
**Fix:** wired to the codebase's own pre-existing, previously-unenforced `EXAM_MANAGEMENT_FEATURES` design (`accounts/models.py`) via a new reusable `HasFeature(feature_key)` permission-class factory (`hamromentor/permissions.py`):
- `destroy` now requires the `exam_delete` feature (added via a `get_permissions()` override scoped to that one action only — every other action keeps its existing `IsStaffOrReadOnly` behavior unchanged).
- `.reschedule` now requires `exam_schedule`.
- `.duplicate` and the generic publish/archive `PATCH` (`is_draft` toggle, which has no dedicated endpoint — it goes through the same generic `update`/`partial_update` as any other field edit) were **deliberately left unchanged**. No `EXAM_MANAGEMENT_FEATURES` key specifically claims `.duplicate`, and gating a generic PATCH by inspecting its payload for an `is_draft` change is a materially bigger, riskier change than a scoped P0 authorization patch — documented here and in `phase-0-baseline.md` as an explicit deferral, not an oversight.
**Behavior-change note:** `admin_role='teacher'`'s fixed feature ceiling (`TEACHER_ALLOWED_FEATURES`) never included `exam_schedule`/`exam_delete` — this fix makes the backend match the *already-documented* design intent and the *already-existing* frontend behavior; no legitimate workflow that a teacher account could previously reach through the UI is affected.
**Tests:** `ExamDeleteFeatureGateTests` (3 new tests), `ExamRescheduleFeatureGateTests` (2 new tests).

### 5. `MediaAssetDetailView.get` — no ownership/staff check

**File:** `Backend/media_library/views.py`.
**Before:** `permission_classes = [IsAuthenticated]` at the class level with no additional check on `.get()` — any authenticated user could poll **any** `MediaAsset` by UUID, despite this class's own docstring claiming staff-only intent (which `.delete()` already correctly enforced).
**Fix:** scoped to **owner-or-staff**, not staff-only — `permissions_util.py`'s `STUDENT_ALLOWED_TYPES = {'student_avatar'}` confirms a plain student account is designed to be able to create (and must therefore be able to poll) their own avatar upload; a blanket staff-only restriction would have broken that designed capability. Denial returns 404 (not 403) to avoid confirming an asset's existence to a non-owner, consistent with IDOR-hardening practice.
**Tests:** `MediaAssetDetailViewGetPermissionTests` (4 new tests: anonymous rejected, other-student rejected, owner succeeds, staff succeeds).

### 6. `RolePermission` UI gap + unenforced ceiling

**Backend, `Backend/accounts/models.py`:** added `user_feature_list(user)`, the single shared computation for "what feature keys can this account use" — now the one function both `UserSerializer.get_permissions()` (frontend visibility) and the new `HasFeature` permission class (backend enforcement) call, closing the systemic gap the audit found: RolePermission was previously consulted **only** by the frontend, by **zero** backend permission classes. `user_feature_list` also enforces `EDITOR_ALLOWED_FEATURES` as a real ceiling at read time (a stored row can no longer grant an Editor more than the documented set, regardless of how it was written).

**Backend, `Backend/accounts/serializers.py`:** `UserSerializer.get_permissions()` now delegates to `user_feature_list` (removed the duplicated logic that could drift again). `RolePermissionSerializer` gained `validate()`: a `PATCH`/`POST` that would grant an Editor role a feature outside `EDITOR_ALLOWED_FEATURES` is now rejected at write time with a 400, not silently accepted.

**Frontend, `Admin/src/app/accounts/page.js`:** re-verified the actual bug (see `phase-0-baseline.md` §4.2 for the corrected diagnosis — it is a missing-checkboxes gap, not an active silent-deletion-on-every-save bug). Added the 5 previously-uncheckable feature keys (`billing`, `question_entry`, `exam_schedule`, `exam_archive`, `exam_delete`) to `FEATURE_LABELS`/`ADMIN_FEATURES`; added the 2 of those 5 that are actually inside the backend's own `EDITOR_ALLOWED_FEATURES` ceiling (`question_entry`, `exam_schedule`) to `EDITOR_FEATURES` — the exact split is read directly from `accounts/models.py`'s already-encoded policy, not guessed.

**Tests:** `RolePermissionEditorCeilingTests` (3 new tests: write-time rejection, write-time success, read-time clamp against a row that bypassed validation e.g. via direct DB write).

## What was explicitly NOT changed

- `TestViewSet.duplicate` and the generic publish/archive PATCH path (see item 4).
- `CRON_SECRET`'s insecure fallback default (deferred to Phase 10 — confirmed not reachable in the actual production deploy path per `phase-0-baseline.md`).
- Every P0 finding that is a data-integrity or missing-feature issue rather than an authorization defect (entitlement expiry, `TestAttempt` timeout, refund system, coupon race, `Question` option-edit history) — sequenced into Phases 2/7/9/10.

## Test results

```
cd Backend && python manage.py test
```

**582/582 tests pass** (baseline 559 + 23 new regression tests, zero failures, zero regressions).

Breakdown of new tests by app:
- `tests_app`: 11 (`ExamSessionAttemptsPermissionTests` ×3, `ExamTemplateSessionsPermissionTests` ×3, `ExamDeleteFeatureGateTests` ×3, `ExamRescheduleFeatureGateTests` ×2)
- `billing`: 5 (`PurchaseApprovalRoleGateTests`)
- `media_library`: 4 (`MediaAssetDetailViewGetPermissionTests`)
- `accounts`: 3 (`RolePermissionEditorCeilingTests`)

Admin frontend: `npm run build` completes cleanly (all 32 routes compile, no errors) after the `accounts/page.js` edit.

## Files changed

- `Backend/tests_app/views.py`
- `Backend/tests_app/tests.py`
- `Backend/billing/views.py`
- `Backend/billing/tests.py`
- `Backend/media_library/views.py`
- `Backend/media_library/tests.py`
- `Backend/accounts/models.py`
- `Backend/accounts/serializers.py`
- `Backend/accounts/tests.py`
- `Backend/hamromentor/permissions.py`
- `Admin/src/app/accounts/page.js`

## Deployment

**Not deployed.** No `gcloud builds submit`/staging/production action was taken — these are local working-tree changes only, per the modernization spec's Step 6/7/8 sequencing ("Implement PHASE 1 only" → "Run tests and report results" → "Wait for phase validation before continuing"). Deployment (staging first, per this engagement's established pattern) requires separate explicit approval.

## Awaiting

Explicit validation of this phase before Phase 2 (Entitlement Foundation) begins, per the phase-gate rule.
