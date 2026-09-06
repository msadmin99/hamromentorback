# Phase 2 — Entitlement Foundation — Completion Report

**Status:** Complete. Awaiting phase validation before Phase 3.

Companion documents: `ENTITLEMENT_CURRENT_STATE.md`, `ENTITLEMENT_DATA_MODEL.md`, `PHASE_2_IMPLEMENTATION_PLAN.md`.

---

## Summary

Built the entitlement/access decision foundation without duplicating any existing data. One genuinely new model set (`entitlements` app — Free Starter policy, per-student grants, an audit log) plus two small, explicitly Phase-2-spec-mandated fixes to existing behavior (`courses/access.py` now honors `expires_at`; scholarship and paid subscriptions can no longer share a row). Free Starter is provisioned idempotently at registration. A minimal, read-only API proves the foundation works end to end. Nothing about the Student Dashboard, Exam Management UI, Subscription/Combo/Payment/Marketplace UI, or any live consumption endpoint (`QuestionViewSet.answer`, `_start_attempt`, `SubmitTestView`) was touched.

## Files changed

**New — `entitlements` app:**
- `models.py` — `FreeStarterPolicy`, `FreeStarterEntitlement`, `EntitlementEventLog`
- `admin.py` — Django-admin registration (quota configuration surface for this phase; a dedicated Admin-panel screen is later-phase UI work)
- `provisioning.py` — `provision_free_starter()`, `consume_free_starter()`
- `services.py` — `AccessDecision`, `academic_eligibility()`, `commercial_entitlement()`, `can_view_qbank()`, `can_start_test()`
- `serializers.py`, `views.py`, `urls.py` — minimal read-only API
- `management/commands/backfill_free_starter_entitlements.py` — idempotent, dry-run-by-default backfill tool (not executed this phase)
- `migrations/0001_initial.py`, `tests.py` (48 tests)

**Modified:**
- `Backend/hamromentor/settings.py` — registered `entitlements` in `INSTALLED_APPS`
- `Backend/hamromentor/urls.py` — mounted `entitlements.urls`
- `Backend/courses/access.py` — Fix 1 (expiry)
- `Backend/billing/payment_service.py` — Fix 2 (`is_scholarship` parameter)
- `Backend/billing/views.py` — `GrantAccessView.post` passes `is_scholarship` through
- `Backend/accounts/serializers.py` — `RegisterSerializer.create()` calls `provision_free_starter()`

**Deviation from the implementation plan, documented:** Fix 1/Fix 2 regression tests were consolidated into `entitlements/tests.py` (alongside the new foundation's own tests) rather than split across `courses/tests.py`/`billing/tests.py` as originally planned — kept together since they're conceptually one Phase 2 test suite; nothing is untested, just co-located differently than first sketched.

## Migrations

One new migration: `entitlements/migrations/0001_initial.py` — pure `CREATE TABLE` for the three new models, zero data written. **Applied only to the ephemeral test database during test runs** — not yet applied to any persistent (dev or production) database, consistent with "no production deployment occurs during this phase." `makemigrations --check` confirms no other app has any pending, unmade schema change.

## APIs added

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/api/entitlements/mine/` | GET | `IsAuthenticated` | Own Free Starter entitlements (lazily provisions on first call if never provisioned) |
| `/api/entitlements/tests/<test_id>/access/` | GET | `IsAuthenticated` | Read-only `CanStart` decision for one Test |
| `/api/entitlements/subjects/<subject_id>/qbank-access/` | GET | `IsAuthenticated` | Read-only `CanView` decision for QBank practice on one Subject |

No existing endpoint's request/response shape changed.

## Access rules implemented

`can_start_test(user, test)` composes, in order: `tests_app.access.can_access_test` (unmodified — staff/creator bypass, draft check, individual/batch/course precedence) → free-exam short-circuit → Grand Test via `GrandTestAccess` (unmodified) → mock/qbank/daily/pyq via the matching `billing.access.has_*_access` function (unmodified) → Free Starter fallback (new) → deny. `can_view_qbank(user, subject)` composes: free-subject short-circuit → `billing.access.has_qbank_access` (unmodified, resolved to a concrete `commercial_entitlement` decision) → Free Starter fallback (new) → deny.

**Explicitly verified against real code, not assumed:** staff bypass applies only to the academic gate (`can_access_test`), never automatically to the commercial gate — matches `tests_app._start_attempt`'s actual, unmodified behavior exactly (re-read fresh this phase; an earlier draft of this test suite briefly encoded the wrong assumption and was corrected before this report — see `entitlements.tests.CanStartTestDecisionTests`).

## Entitlement rules implemented

- `FreeStarterEntitlement.effective_status` computes live validity (revoked → expired → exhausted → active), matching `Subscription.is_current`'s existing correct pattern — never trusts a stale `status` field alone (Phase 2 spec Step 9).
- `quantity`/`unlimited` snapshotted at provisioning time from whatever `FreeStarterPolicy` was active then — a later policy edit never rewrites an already-provisioned student (matches the platform's existing `PurchaseComboItem.price` snapshot precedent).
- **No policy rows are seeded.** The system grants nothing until an admin explicitly activates at least one `FreeStarterPolicy` row — verified by a dedicated test (`test_no_active_policy_means_nothing_is_granted`).
- **Fix 1** (`courses/access.py`): `eligible_course_ids`/`eligible_batch_ids` now filter `Q(expires_at__isnull=True) | Q(expires_at__gte=now)`, matching the pattern already correct elsewhere (`billing.access._active_subscriptions`). Verified this can only remove access from already-expired rows, never grant new access — no existing test or code path relied on the old permissive behavior.
- **Fix 2** (`billing/payment_service.py`): `_extend_or_create_subscription` now takes `is_scholarship`, scoping which existing row is eligible to extend by origin (`scholarship__isnull=not is_scholarship`) — a scholarship grant and a real purchase for the same `(user, course, product_type)` now always produce two independent `Subscription` rows instead of one merged row. Same-origin renewals (a second scholarship extending the first, or a second purchase extending an existing paid subscription) are unaffected — still correctly extend, not duplicate.

## Test results

```
cd Backend && python manage.py test
```

**630/630 tests pass** (baseline 582 post-Phase-1 + 48 new, zero failures, zero regressions).

New test breakdown (all in `entitlements/tests.py`):
- `FreeStarterProvisioningTests` (7) — no-policy no-op, per-policy provisioning, idempotency, snapshot-not-live-read, registration integration, registration-survives-provisioning-failure.
- `FreeStarterConsumptionTests` (7) — decrement, exhaustion, exhaustion logging, unlimited never exhausts, no-row-fails-cleanly, revoked/expired can't be consumed.
- `FreeStarterConcurrentConsumptionTests` (1) — genuine 10-thread race test, confirms exactly 5 of 10 concurrent consumers succeed against a quota of 5, never negative.
- `EligibleCourseIdsExpiryTests` (5) — Fix 1 regression: non-expired/future-expiry preserved, expired now denied (both at the helper level and through a real consumer, `can_access_test`), batch-id variant.
- `ScholarshipPaidSeparationTests` (6) — Fix 2 regression: cross-origin never merges (both directions), same-origin renewal still works, revoking a scholarship doesn't touch a separately-purchased subscription (exercised through the real `/api/grant-access/` and `/api/scholarships/{id}/revoke/` endpoints, not just the internal function), source-type distinguishability.
- `CanStartTestDecisionTests` (12) — anonymous denied, free exam allowed, unenrolled denied, draft denied for student, staff academic-bypass confirmed, staff still needs commercial entitlement confirmed, batch/individual assignment allowed, pro-mock free-starter fallback (available and exhausted), pro-mock with real subscription, grand test with/without access grant.
- `CanViewQbankDecisionTests` (4) — free subject (even unauthenticated), paid subject free-starter fallback, paid subject denied with nothing, paid subject with subscription.
- `EntitlementsApiTests` (9) — auth required on all three endpoints, own-data-only, lazy provisioning on first call, IDOR check (two different authenticated users against the same resource id get independently correct answers), 404 for nonexistent resource.

## Known limitations (explicitly deferred, not oversights)

- The new decision layer (`can_start_test`/`can_view_qbank`) is **not wired into any live consumption endpoint**. Deferred to a later phase once the exact Free Starter consumption rule per exam type is a settled product decision (see `PHASE_2_IMPLEMENTATION_PLAN.md`).
- The confirmed mock/daily/pyq course-scoping inconsistency in `billing.access` is **not fixed** — real subscriber-behavior change, deliberately out of scope per "existing valid subscriber access must continue to work."
- `ScholarshipViewSet.revoke()` still does not touch the `Enrollment` row `_ensure_enrollment()` created — flagged as a separate business decision (does revoking product access also mean revoking course-catalog visibility?), not resolved here.
- `can_purchase`/`can_register`/`can_continue`/`can_submit`/`can_review`/`can_view_solutions`/`can_view_rank`/`can_view_analytics` are not implemented — most require touching the live attempt/results flow, explicitly out of this phase's scope.
- The backfill command exists and is tested-ready but **not run** — whether/when to grant Free Starter to already-registered students is a product decision, not resolved here.
- No proactive background job deactivates expired `Enrollment`/`Subscription` rows — Step 9's mandate is satisfied at read time (matching how `Subscription.is_current` already works platform-wide); a proactive sweep is background-jobs scope, not entitlement-data-model scope.

## Rollback instructions

1. `python manage.py migrate entitlements zero` — drops all three new tables. Safe: nothing else references them (not wired into any existing endpoint's request path).
2. Revert `courses/access.py`'s filter addition (Fix 1) — one-line-scale change, no data to unwind.
3. Revert `billing/payment_service.py`'s `is_scholarship` parameter and `billing/views.py`'s call-site change (Fix 2) — no data migration to undo; any `Subscription` rows already created under the fixed behavior remain valid, correctly-shaped rows even if the code is rolled back.
4. Revert the `provision_free_starter()` call in `accounts/serializers.py` — future registrations simply stop getting Free Starter rows; no cleanup needed for already-provisioned students.
5. Unregister `entitlements` from `INSTALLED_APPS`/`urls.py`.

## Deployment

**Not deployed.** All changes are local working-tree edits; the new migration has not been applied to any persistent database. Per the phase-gate rule, deployment requires separate explicit approval, same as Phase 1.

## Awaiting

Explicit validation of this phase before Phase 3 (Freemium Starter Access — the student-facing UI/UX layer this foundation is built for) begins.
