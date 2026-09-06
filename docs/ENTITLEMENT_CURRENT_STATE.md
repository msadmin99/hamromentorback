# Entitlement Current State — Phase 2 Inventory

Re-verified directly against current source (post-Phase-1) on 2026-09-02, per the Phase 2 spec's Step 1/2 instruction not to trust the audit over live code. Every access source below was re-read fresh; three refinements to the master audit's framing were found and are called out explicitly.

---

## 1. Course Enrollment

- **SOURCE:** `courses.Enrollment` — the platform's single academic-eligibility record.
- **MODEL:** `courses/models.py` — `user`, `course`, `package` (opt), `batch` (opt), `access_type` (`free`/`package`), `student_code`, `is_active`, `enrolled_at`, `expires_at`. `unique_together=('user','course')`.
- **API:** `courses/views.py` — `EnrollmentViewSet` (admin CRUD, `IsAdminRoleOrAbove`), `MyEnrollmentsView` (read-only, `IsAuthenticated`), `EnrollmentRequestViewSet.approve` (creates/updates via `update_or_create`).
- **SERVICE:** `courses/access.py` — `eligible_course_ids(user)`, `eligible_batch_ids(user)`. `billing/payment_service.py`'s `_ensure_enrollment(user, course)` is the write path every commercial-grant flow calls through.
- **ACCESS CHECK:** `Enrollment.objects.filter(user=user, is_active=True)` — consumed by `tests_app.access.can_access_test`/`visible_test_queryset`, `academics.access`, `videos_app`'s course-gated video list-scoping.
- **EXPIRY:** `expires_at` field exists on the model but **was not honored by `eligible_course_ids`/`eligible_batch_ids`** — confirmed still true as of this re-read (`courses/access.py`, unchanged since the master audit). **This is fixed in this phase** — see `ENTITLEMENT_DATA_MODEL.md` §Fix 1.
- **REVOCATION:** No dedicated revoke action. Plain admin `PATCH {"is_active": false}` via `EnrollmentViewSet`. `PruneExpiredPackagesView` (`courses/views.py`) **deletes** (does not deactivate) stale `access_type='package'` rows past `expires_at`, gated by a cron secret, triggered only if an external scheduler calls it (unconfirmed whether one does in production).
- **CURRENT PROBLEMS:** (1) expiry not enforced at the eligibility-check layer — fixed this phase. (2) No dedicated withdraw/transfer action. (3) `student_code` auto-generation has no collision retry despite `unique=True` — not addressed this phase (data-integrity, low blast radius, deferred).

## 2. Subscription

- **SOURCE:** `billing.Subscription` — one row per (user, course, product_type) activated access window.
- **MODEL:** `billing/models.py` — `user`, `plan` (opt FK to `SubscriptionPlan`), `course`, `product_type`, `starts_at`, `expires_at`, `mock_test_quota`/`mock_test_used`, `auto_renew`, `is_active`. `is_current` property (live-computed, correct).
- **API:** `billing/urls.py` → `SubscriptionPlanViewSet` (catalog), `MySubscriptionsView` (own), `SubscriptionAutoRenewView`; no direct public CRUD on `Subscription` itself (only created/extended server-side).
- **SERVICE:** `billing/payment_service.py` — `_extend_or_create_subscription(user, course, product_type, duration, plan, mock_test_quota)`, called from `_activate_product()` (real purchases) and `GrantAccessView.post` (admin manual grant / scholarship).
- **ACCESS CHECK:** `billing/access.py` — `_active_subscriptions()`, `has_qbank_access`, `has_video_access` (premium branch), `has_mock_test_access`, `has_daily_test_access`, `has_pyq_access`.
- **EXPIRY:** Correctly checked everywhere it's read (`Q(expires_at__isnull=True) | Q(expires_at__gte=now)`), confirmed unchanged and still correct on this re-read.
- **REVOCATION:** No dedicated cancel/revoke action for a *paid* Subscription — only `Scholarship.revoke()` deactivates a linked one (see §4). `is_active` is never flipped on natural expiry (only on explicit scholarship revocation) — matches the master audit's finding, still true.
- **CURRENT PROBLEMS:** (1) `has_mock_test_access`/`has_daily_test_access`/`has_pyq_access` don't scope by course at all (confirmed unchanged) — **deliberately NOT fixed this phase**, see `ENTITLEMENT_DATA_MODEL.md`'s explicit deferral rationale (fixing this changes real subscriber behavior; the Phase 2 spec's "existing valid subscriber access must continue to work" instruction argues against a behavior change here without a dedicated review). (2) The scholarship/paid shared-row merge risk (§4) — **fixed this phase**.

## 3. Combo Plan

- **SOURCE:** `billing.ComboPlan` — a catalog bundle, never itself an access record.
- **MODEL:** `billing/models.py` — `name`, `course`, `plans` (M2M to `SubscriptionPlan`), `discount_percent`. Price computed live, never stored (correct, confirmed unchanged).
- **API:** `ComboPlanViewSet`, `ComboQuoteView` (pricing preview).
- **SERVICE:** Purchasing a combo creates `PurchaseComboItem` rows (price-snapshotted at purchase time) → on approval, `_activate_product()`'s `kind == 'combo'` branch loops `purchase.combo_items` and calls `_extend_or_create_subscription()` once per bundled plan.
- **ACCESS CHECK:** None combo-specific — resolves entirely into N ordinary `Subscription` rows, then checked exactly like any other subscription.
- **EXPIRY / REVOCATION:** Inherited from the resulting `Subscription` rows — no separate combo-level expiry/revocation exists or is needed.
- **CURRENT PROBLEMS:** None found this phase beyond what's already correctly handled (price/composition snapshotting confirmed sound). No change made.

## 4. Scholarship

- **SOURCE:** `billing.Scholarship` — an admin-granted, zero-revenue Subscription, tracked separately for analytics exclusion.
- **MODEL:** `billing/models.py` — `user`, `course`, `product_type`, `plan` (opt), `subscription` (OneToOne to `billing.Subscription`), `reason`, `granted_by`, `granted_at`, `expires_at`, `is_active`.
- **API:** `ScholarshipViewSet` (`IsAdminRoleOrAbove`; `create()` explicitly disabled, redirects to `/grant-access/`), `.revoke` action.
- **SERVICE:** `GrantAccessView.post` (`billing/views.py`) — creates/extends a `Subscription` via `_extend_or_create_subscription()`, then (if `is_scholarship`) wraps it in a `Scholarship` row.
- **ACCESS CHECK:** None reads `Scholarship` directly — access is entirely mediated through the linked `Subscription` (confirmed unchanged: `billing/access.py` has zero references to `Scholarship`).
- **EXPIRY:** `Scholarship.expires_at`/`is_active` are pure record-keeping — never read by any access-check function (confirmed unchanged). The *actually enforced* expiry is the linked `Subscription.expires_at`.
- **REVOCATION:** `ScholarshipViewSet.revoke()` deactivates `Scholarship.is_active` and `scholarship.subscription.is_active` — but **re-confirmed this phase: never touches the `Enrollment` row** `_ensure_enrollment()` also created, so broad course-catalog visibility survives revocation indefinitely. **Not fixed this phase** — this specific piece (Enrollment-on-revoke) is a distinct, larger decision (does revoking a scholarship's *product* access also mean revoking *course visibility* entirely, even if the student has other reasons to see that course?) flagged as a business decision for a later phase, not blindly resolved here.
- **CRITICAL PROBLEM CONFIRMED AND FIXED THIS PHASE:** `_extend_or_create_subscription()` previously looked up *any* existing active `Subscription` for `(user, course, product_type)` regardless of origin — a scholarship grant could silently extend/attach to an already-paid subscription row (or vice versa), meaning revoking the scholarship would deactivate the same row backing the student's paid access. **Fixed** — see `ENTITLEMENT_DATA_MODEL.md`'s Fix 2.

## 5. Direct / Grand Test Purchase

- **SOURCE:** `billing.GrandTestAccess` — a distinct model, one row per `(user, test)`, password-based, created only from `Purchase(kind='grand_test')`.
- **MODEL:** `billing/models.py` — `purchase` (OneToOne), `user`, `test`, `password` (auto-generated, unique), `granted_at`, `email_sent_at`. No `expires_at` field at all (by design — a one-time grant, not a recurring subscription).
- **API:** Created via `PurchaseViewSet` → `payment_service.activate()` → `_activate_product()`'s `kind == 'grand_test'` branch.
- **ACCESS CHECK:** `billing.access.get_grand_test_access(user, test)`.
- **EXPIRY / REVOCATION:** None — matches its one-time, non-recurring nature. Not a gap; a deliberate design given Grand Tests are single scheduled events, not ongoing subscriptions.
- **CURRENT PROBLEMS:** None found. No change made.

## 6. Exam / Batch / Individual Assignment

- **SOURCE:** `tests_app.Test.assigned_students` (M2M to `User`), `Test.assigned_batches` (M2M to `courses.Batch`) — independent, per-exam overrides.
- **ACCESS CHECK:** `tests_app/access.py` — `can_access_test()`/`visible_test_queryset()`. Individual assignment checked before batch, batch before course; each is independent and bypasses the others (confirmed unchanged on this re-read).
- **EXPIRY / REVOCATION:** None — a plain M2M membership, removed by unassigning (admin PATCH on the `Test`).
- **CURRENT PROBLEMS:** None new. Confirmed this remains the correct, working precedent for individual/batch overrides — reused as-is by the new decision layer, not modified.

## 7. Password

- **SOURCE:** `Test.access_password` / `GrandTestAccess.password` / `ExamSession.password` — an **additional** layer, never a substitute for entitlement.
- **ACCESS CHECK:** `tests_app/views.py`'s `_start_attempt()` — checked only *after* academic + commercial entitlement both pass (confirmed unchanged, correct ordering).
- **CURRENT PROBLEMS:** None. Correctly separated already; the new decision layer preserves this ordering rather than reinventing it (see `ENTITLEMENT_DATA_MODEL.md`).

## 8. Admin Override

- **SOURCE:** `tests_app.access.can_access_test()`'s staff/creator bypass (`user.is_staff or test.created_by_id == user.id` → always allowed, ignores `is_draft` and all other checks).
- **CURRENT PROBLEMS:** None found. This is a legitimate, narrow, already-correct override — preserved as-is.

## 9. Free / Trial / Preview Access

- **SOURCE (existing, pre-Phase-2):** `Subject.is_free` (QBank), `Test.free_preview_questions` (Daily Test truncated preview via `is_preview_only()`), `Video.access_level in ('public', 'registered')`.
- **CURRENT PROBLEMS:** None — these are narrow, purpose-built free/preview mechanisms, not a general Free Starter policy. **No general "new student gets a starter allocation" mechanism exists anywhere in the codebase** — confirmed by exhaustive grep for `free_starter`/`starter_entitlement`/similar, zero hits. This is the genuine gap Phase 2 builds the foundation for (see `ENTITLEMENT_DATA_MODEL.md`).

## 10. Legacy / Other

- No other access mechanism was found on this re-read. `marketplace.CourseEnrollment` (a fully separate system for the unrelated `TeacherCourse` marketplace, confirmed still structurally independent from everything above) is out of scope for this phase — it is not part of the `courses`/`tests_app`/`billing`/`videos_app` access graph this phase is building the foundation for.

---

## Summary of what changes this phase vs. what's confirmed already correct

| Source | Re-verified status | Action this phase |
|---|---|---|
| Course Enrollment | Expiry ignored (confirmed bug, unchanged) | **Fix**: honor `expires_at` in `eligible_course_ids`/`eligible_batch_ids` |
| Subscription | Correct expiry handling; course-scoping gap on 3 of 5 product types (confirmed unchanged) | Course-scoping gap **deliberately not fixed** (behavior-change risk, deferred) |
| Combo | Confirmed sound | No change |
| Scholarship | Shared-row merge risk (confirmed unchanged) | **Fix**: scholarship and paid subscriptions never share a row |
| Scholarship→Enrollment on revoke | Confirmed still not touched by `revoke()` | **Not fixed this phase** (flagged as a business decision) |
| Grand Test Access | Confirmed sound | No change |
| Batch/Individual Assignment | Confirmed sound | No change (reused as-is by the new decision layer) |
| Password | Confirmed correctly ordered after entitlement | No change (preserved) |
| Admin Override | Confirmed sound | No change |
| Free Starter | **Did not exist** | **New**: `entitlements` app (models, service, provisioning, minimal API) — foundation only, not wired into live consumption endpoints yet
