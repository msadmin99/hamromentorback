# Phase 2 — Entitlement Foundation — Implementation Plan

Companion documents: `ENTITLEMENT_CURRENT_STATE.md` (Step 2 inventory), `ENTITLEMENT_DATA_MODEL.md` (Step 24 schema design). This document is the "exact implementation plan" (files/models/APIs/migrations/risks/rollback) requested before coding begins.

---

## What this phase builds

1. **A new `entitlements` Django app** — the one genuinely missing model (Free Starter) plus a decision-layer service that composes every *existing* access source into one uniform, explainable result. Not a replacement for `Enrollment`/`Subscription`/`Scholarship`/`GrandTestAccess` — those stay the systems of record.
2. **Two small, well-evidenced fixes to existing behavior**, both explicitly mandated by the Phase 2 spec's own Step 9 and Step 17: `courses/access.py` now honors `expires_at`; `billing/payment_service.py` never lets a scholarship and a paid subscription share the same row.
3. **Free Starter provisioning wired into registration**, idempotent, non-blocking.
4. **A minimal, read-only API surface** (`/api/entitlements/mine/`, `/api/entitlements/tests/<id>/access/`, `/api/entitlements/subjects/<id>/qbank-access/`) — enough to prove the foundation works end-to-end via HTTP, not a UI.

## What this phase deliberately does NOT build

- No change to the Student Dashboard, Exam Management UI, Subscription UI, Combo UI, Payment UI, or Marketplace UI.
- No wiring of the new decision layer into any *existing* live consumption endpoint (`QuestionViewSet.answer`, `_start_attempt`, `SubmitTestView`, etc.) — those remain exactly as they are today. The new `can_start_test`/`can_view_qbank` functions are read-only decision queries, provable correct by their own tests, ready for Phase 4 to wire into the live paths once the exact per-exam-type Free Starter consumption rule is a settled product decision (which specific Mock Test counts as "the" free one is not decided anywhere in the spec or the codebase — inventing an answer here would be guessing business policy).
- No fix to the confirmed mock/daily/pyq course-scoping inconsistency in `billing.access` — a genuine behavior change to real subscriber access, explicitly against this phase's "existing valid subscriber access must continue to work" instruction without a dedicated review.
- No change to `ScholarshipViewSet.revoke()`'s Enrollment-untouched behavior — a separate, larger business decision (does revoking product access also mean revoking course-catalog visibility?), not resolved here.
- No `Subscription`/`Enrollment` background expiry job — Step 9's "effective validity" requirement is satisfied at *read time* (the fix + the existing correct patterns), matching how `Subscription.is_current` already works platform-wide; a proactive deactivation sweep is a background-jobs concern, not this phase's data-model concern (the master audit's own recommended sequencing puts this alongside the other stale-state background-job fixes, not entitlement foundation work).

## Exact files

**New:**
- `Backend/entitlements/__init__.py`, `apps.py`, `models.py`, `admin.py`, `provisioning.py`, `services.py`, `serializers.py`, `views.py`, `urls.py`, `tests.py`
- `Backend/entitlements/management/__init__.py`, `management/commands/__init__.py`, `management/commands/backfill_free_starter_entitlements.py`
- `Backend/entitlements/migrations/__init__.py`, `migrations/0001_initial.py`

**Modified:**
- `Backend/hamromentor/settings.py` — register `entitlements` in `INSTALLED_APPS`.
- `Backend/hamromentor/urls.py` — mount `entitlements.urls`.
- `Backend/courses/access.py` — Fix 1 (expiry).
- `Backend/billing/payment_service.py` — Fix 2 (`is_scholarship` parameter).
- `Backend/billing/views.py` — `GrantAccessView.post` passes `is_scholarship` through.
- `Backend/accounts/serializers.py` — `RegisterSerializer.create()` calls `provision_free_starter(user)`.
- `Backend/courses/tests.py` — regression tests for Fix 1.
- `Backend/billing/tests.py` — regression tests for Fix 2.
- `Backend/accounts/tests.py` — regression test confirming registration provisions Free Starter idempotently.

## Exact models

See `ENTITLEMENT_DATA_MODEL.md` in full. Summary: `FreeStarterPolicy` (admin-configurable quota per resource_type, starts empty), `FreeStarterEntitlement` (per-student grant + usage, `unique_together=('user','resource_type')`), `EntitlementEventLog` (append-only audit trail).

## Exact APIs

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/api/entitlements/mine/` | GET | `IsAuthenticated` | Own Free Starter entitlements summary (list of resource_type/quantity/used/remaining/status) |
| `/api/entitlements/tests/<test_id>/access/` | GET | `IsAuthenticated` | `CanStart` decision for one Test — `{allowed, source_type, source_id, expires_at, remaining, reason}` |
| `/api/entitlements/subjects/<subject_id>/qbank-access/` | GET | `IsAuthenticated` | `CanView` decision for QBank practice on one Subject, same shape |

IDOR posture (Step 22): every function takes `request.user` only — no endpoint accepts or trusts a client-supplied `user_id`. `test_id`/`subject_id` are resource identifiers, not identity claims; the decision functions scope correctly by `request.user` regardless of which resource id is asked about, and a nonexistent/unauthorized resource id resolves through the normal `get_object_or_404` pattern already used everywhere else in this codebase.

## Migrations

One new migration, `entitlements/migrations/0001_initial.py` — pure `CREATE TABLE`, zero data written (see `ENTITLEMENT_DATA_MODEL.md`). No migration in any existing app.

## Risks

| Risk | Mitigation |
|---|---|
| Fix 1 (`expires_at`) unexpectedly removes access from a currently-active student | Re-verified no existing test or code path relies on the old (buggy) permissive behavior; can only affect rows that are already, definitionally, expired. Full regression run across all consumers (tests_app, academics, videos_app, billing). |
| Fix 2 changes `GrantAccessView`'s subscription-lookup query shape | New behavior only diverges from old behavior in the specific case a scholarship and a paid subscription would have collided — every other call path (plain purchases, non-scholarship admin grants) is unaffected (default `is_scholarship=False` preserves the exact prior query). |
| Free Starter provisioning fails and blocks registration | Deliberately called *after* `StudentProfile.objects.create()`, not inside the same atomic block, and internally exception-tolerant at the caller (registration must succeed even if provisioning has an issue) — see `services.py`'s lazy-provision fallback as a second chance for any student who somehow registered without it. |
| Concurrent Free Starter consumption creates a negative-remaining race | `select_for_update()` + explicit `used + amount > quantity` check inside `transaction.atomic()`, matching the codebase's own existing correct pattern; a genuine multi-thread concurrency test is included (not just a sequential unit test), following the exact `APITransactionTestCase` + `PRAGMA busy_timeout` pattern already established in `tests_app.tests.SubmitTestDoubleSubmissionRaceTests`. |
| New app misconfigured (not migrated, not in INSTALLED_APPS) | Verified via full test suite run before considering the phase complete. |

## Rollback

See `ENTITLEMENT_DATA_MODEL.md`'s Rollback strategy section — summary: `migrate entitlements zero` cleanly removes the new app (nothing else references it yet); Fix 1/Fix 2 are one-line-scale reverts with no data to unwind; the registration hook is a one-line removal.

## Test plan (Step 28)

- **Entitlement lifecycle:** creation (provisioning), consumption, exhaustion, expiry (effective_status), revocation, duplicate-provisioning prevention.
- **Access decisions:** allowed/denied for `can_start_test` across every source (course-enrolled+free exam, subscription, scholarship, grand-test purchase, batch assignment, individual assignment, staff override, free-starter fallback, unauthenticated); allowed/denied for `can_view_qbank`.
- **Free Starter:** first registration provisions correctly against active policies; a second registration attempt / duplicate call does not create a second row; quota consumption and exhaustion; **concurrent consumption cannot go negative** (real thread-based test).
- **Fix 1 regression:** enrolled-and-not-expired still grants access (unchanged); enrolled-but-expired now correctly denies (bug fixed) — exercised through `eligible_course_ids` directly and through at least one real consumer (`tests_app.access.can_access_test`).
- **Fix 2 regression:** scholarship-then-existing-paid-subscription does not merge; paid-purchase-then-existing-scholarship does not merge; revoking a scholarship's subscription does not affect a separately-purchased subscription for the same product+course; same-origin renewal still correctly extends (not duplicates).
- **Security:** `/api/entitlements/mine/` never returns another user's data; the two decision endpoints are scoped to `request.user` regardless of resource id; anonymous requests are rejected.
- **Full existing suite:** must remain green (baseline 582/582 post-Phase-1) — any new failure blocks completion per the phase-gate rule.
