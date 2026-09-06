# Phase 9 — Commercial Integrity — Completion Report

**Status:** Complete. Awaiting phase validation before Phase 10.

Companion document: `PHASE_9_ARCHITECTURE.md`.

---

## 1. Current implementation-plan.md Phase 9 specification

After the numbering normalization described in §2, **Phase 9 = Commercial
Integrity**: *"Subscription/Combo/Payment/Coupon/Refund/Scholarship fixes,
with historical entitlements protected."* Its four concrete items:

1. **Refund system** — *"currently does not exist at all, confirmed by the
   audit"*: a new `Purchase.status` value or a related `Refund` model, plus
   an explicit refund→entitlement policy (*"must not revoke unrelated
   entitlements"*), with a business decision required on exactly what a
   refund revokes.
2. **Coupon race condition fix** — row-lock the `Coupon` at validation and
   redemption, and re-check `max_uses`/`max_uses_per_user` at `activate()`
   time, not only at `Purchase` creation.
3. **Historical purchase/combo protection** — already correctly
   snapshotted; preserve as-is, no change needed.
4. **`CRON_SECRET` hardening** — remove the insecure default outside
   `DEBUG`; production Secret Manager wiring stays authoritative.

All four are addressed below.

## 2. Phase-numbering discrepancy — and the permanent fix

At kickoff, `implementation-plan.md`'s Phase 9 was **"Exam Builder"**;
this phase's content matched its **Phase 10** ("Commercial Integrity").
That was the *fourth consecutive* conflict. I stopped before touching any
code, reported it, and asked how to proceed rather than performing another
adjacent swap — because the swap was the thing causing the recurrence:
Exam Builder had sat in the sequence since Phase 6 and was pushed down one
slot every time (6 → 7 → 8 → 9), regenerating the identical off-by-one for
the next kickoff.

On instruction, the sequence was **normalized once, permanently**:

| Before | Feature | After |
|---|---|---|
| 1–8 | Security … Historical Integrity (complete) | **1–8, unchanged** |
| 9 | Exam Builder | **Deferred / out-of-sequence** (no number) |
| 10 | Commercial Integrity | **9** — this phase |
| 11 | Student UX | **10** |
| 12 | Analytics / Performance / Accessibility | **11** |

Exam Builder is documented in `implementation-plan.md` under
**DEFERRED / OUT-OF-SEQUENCE**, scope unchanged, with a note recording why
it is parked and that scheduling it is a product decision. Completed
phases keep their numbers, scope and reports. Stale in-document
cross-references ("Phase 10" for refund/coupon work, "Phase 2/10 data")
were corrected with an inline note. The plan header's status line, which
still claimed "Phases 2–12 planned, not yet started", now reflects
reality. Phase number now equals execution order, so this class of
conflict cannot recur.

## 3. Pre-implementation audit findings

Read-only, before any change. Confirmed **already correct** (verified in
code, not assumed):

- **Phase 1 financial authorization intact** — `approve`, `reject`,
  `request-resubmission`, `audit-log`, `GrantAccessView`,
  `ScholarshipViewSet`/`revoke`, `CouponViewSet`, `AnalyticsView` are all
  `IsAdminRoleOrAbove`, not bare `IsStaff`/`IsAdminUser`.
- **Phase 2 scholarship/paid separation intact** —
  `_extend_or_create_subscription` scopes by origin
  (`scholarship__isnull=not is_scholarship`), so scholarship and paid rows
  are structurally distinct and revoking one cannot touch the other.
- **Every Purchase transition already locked/guarded/audited/notified** —
  `select_for_update()` in `transaction.atomic()`, precondition re-checked
  under the lock, `PaymentAuditLog` row, student notification.
- **IDOR protected** — `PurchaseViewSet.get_queryset()` filters to
  `user=request.user` for non-staff.
- **Client cannot fake payment success** — students can only reach
  `pending`; prices are recomputed server-side (`compute_price`).
- **Historical purchase/combo amounts already snapshotted** — unchanged.

Confirmed **broken**, and fixed here:

- **Refunds did not exist.** Repo-wide search: zero occurrences of
  "refund" anywhere. And the linkage one would need was missing —
  `GrandTestAccess.purchase` was the only traceable grant; a
  `Subscription` had no record of which Purchase paid for it.
- **Coupon caps were unenforceable.** Limits were checked only at Purchase
  *creation*, while `usage_count` incremented at *approval* — separated by
  a human review step. N students could each create an order against a
  `max_uses=1` coupon while `usage_count` was 0, and all would later be
  approved. Over-redemption with *zero* concurrency, plus a TOCTOU race.
- **`CRON_SECRET` fell back to a source-visible default**
  (`'dev-cron-secret-change-me'`) whenever the env var was missing.

## 4–5. Existing commerce / entitlement architecture

Diagrammed in `PHASE_9_ARCHITECTURE.md` §1. In short: plan/test/course →
`Purchase` → manual admin verification → `payment_service.activate()` →
materialized access (`Subscription`, `GrandTestAccess`,
`marketplace.CourseEnrollment`, plus a shared `courses.Enrollment`) →
`billing.access.has_*_access` → Phase 4 `AccessDecision` → capability.
Entitlement sources are additive and resolved by "does **any** valid
source grant this" — no precedence order, before or after this phase.

## 6. Payment lifecycle

`unpaid → pending → approved | rejected | resubmission_requested`, plus
`expired`/`cancelled`, and now `refunded` (only from `approved`).
**There is no payment gateway and no webhook** — verification is a human
admin reviewing an uploaded screenshot (`ManualQRProvider` is the only
provider implementation). See §20 for what that means for security claims.

## 7–11. Subscription / combo / scholarship / enrollment / coupon lifecycles

Documented in `PHASE_9_ARCHITECTURE.md` §1–§6. Behaviour confirmed
unchanged by this phase except where listed in §12–§14:

- **Subscription**: extension accumulates from the current expiry
  (June 30 + 30d = July 30) — the established rule, not invented here.
  Renewals extend the same row; cross-origin (scholarship vs paid) always
  creates a separate row.
- **Combo**: access is *materialized* — activation creates/extends one
  `Subscription` per bundled plan (not dynamically inherited).
- **Scholarship**: `grant` via `GrantAccessView`, `revoke` deactivates the
  scholarship and its own linked subscription only.
- **Enrollment**: `expires_at`/`is_active` respected (Phase 2).
- **Coupon**: `usage_count` counter + `Purchase.coupon` FK; no separate
  redemption model.

## 12. Refund lifecycle (new)

`Purchase.status='refunded'` + `refunded_at`/`refunded_by`/
`refund_reason`, driven only by `payment_service.refund()` (locked,
guarded, audited — same shape as every existing transition) and exposed at
`POST /api/purchases/{id}/refund/`, gated `IsAdminRoleOrAbove`, reason
required.

Reversal is driven by the new **`PurchaseEntitlementGrant`** ledger — one
row per access record a purchase actually granted, written at activation,
read only by refund. The rule:

- Sole live grant on the record **and** this purchase created it →
  deactivate the record.
- Otherwise → subtract **only this grant's contributed duration** from the
  current expiry; the record stays active covering less time.
- Lifetime grant shared with another purchase → left unchanged, and the
  audit metadata says so rather than guessing.

**A test caught a real bug in my first implementation here.** The obvious
rule ("deactivate what this purchase created") destroys another purchase's
paid time whenever several purchases accumulate onto one subscription row
— a combo creates it, a later direct purchase extends it. The
cross-source tests failed immediately, and the rule above is the fix.

**Business decisions made explicitly** (both documented, both one-line
reversible): partial refunds are **not** supported; a refund does **not**
return the coupon's global `usage_count` slot (keeping `max_uses` a
ceiling a buy/refund cycle cannot farm past).

## 13. Entitlement source-resolution behaviour

Unchanged and deliberately so — additive union, no precedence. A refund
can only remove one specific purchase's own contribution; every other
source answers identically afterward. Traceability is now real rather than
inferred: `PurchaseEntitlementGrant` records which purchase produced which
access record, and the refund audit entry names exactly what was reversed.

## 14. Final architecture implemented

See `PHASE_9_ARCHITECTURE.md`. Three changes: the refund mechanism +
grant ledger (§12), redemption-time coupon enforcement, and `CRON_SECRET`
hardening. No new access engine, no new precedence, no commerce model
replaced, no universal "UserAccess" table.

## 15–19. Phase integration

- **Phase 4** — still authoritative; commerce writes entitlement state,
  never a capability. The single access-layer edit is
  `get_grand_test_access()` now filtering `revoked_at`, i.e. the existing
  one resolution point telling the truth about a revoked grant.
- **Phase 3 (Free Starter)** — untouched; refund never consumes, restores
  or inspects quota.
- **Phase 6 (sessions/timing)** — untouched.
- **Phase 7 (results)** — untouched.
- **Phase 8 (snapshots)** — untouched, and explicitly tested: refunding
  after an exam leaves the attempt, score, answers and snapshots intact.

## 20. Security changes

- Coupon caps are now enforced where redemption actually happens, under a
  `select_for_update()` row lock — the (max_uses + 1)th approval always
  loses, verified under real concurrent threads.
- `CRON_SECRET` raises `ImproperlyConfigured` at startup outside `DEBUG`
  if unset, and both `_check_cron_secret` helpers fail closed on an empty
  configured secret, so a blank value can never match a blank header.
- The refund endpoint is `IsAdminRoleOrAbove` — verified by test that an
  `is_staff` **editor** is refused, alongside student and anonymous.
- **No webhook security is claimed, because there are no webhooks.** If a
  gateway is added later, signature verification and callback idempotency
  are that phase's work; `payment_providers.py` is the seam and
  `payment_service` remains the only thing touching the database.

**Flagged, not fixed** (out of scope — content-processing, not commerce):
`MEDIA_PROCESSING_SECRET`, `IMPORT_PROCESSING_SECRET`,
`DEDUP_PROCESSING_SECRET` and `STATS_PROCESSING_SECRET` share the identical
insecure-default pattern, and `DEDUP_PROCESSING_SECRET` is absent from
`cloudbuild.yaml`'s secret list entirely — meaning it currently runs on its
public default in production. Same two-line fix applies to each.

## 21. Performance changes

Activation adds one `SELECT … FOR UPDATE` on the Coupon (only when a
coupon is attached) and one small INSERT per granted access record.
Refund is O(grants on that purchase), an admin action. **Nothing was added
to any student hot path** — question-answer, exam-start and result paths
are untouched; entitlement reads still go through the same
`_active_subscriptions()` query as before. No new N+1: the refund loop
`select_related`s its three possible targets.

## 22. Files/modules changed

**Backend** — `billing/models.py` (refund fields, `revoked_at`,
`PurchaseEntitlementGrant`, audit action choice), `billing/payment_service.py`
(grant recording, coupon re-check, `refund()`, `_reverse_grant()`/
`_reverse_access_record()`/`_grant_contribution()`), `billing/views.py`
(refund action, fail-closed cron check), `billing/serializers.py`
(read-only refund fields), `billing/access.py` (`get_grand_test_access`
filters revoked), `courses/views.py` (fail-closed cron check),
`hamromentor/settings.py` (`CRON_SECRET` hardening),
`billing/migrations/0013_phase9_refund_and_grants.py`,
`billing/tests_phase9.py` (new).

**Docs** — `docs/implementation-plan.md` (normalization),
`docs/PHASE_9_ARCHITECTURE.md`, `docs/PHASE_9_COMPLETION_REPORT.md`.

**Frontend (Admin)** — `src/app/payments/page.js`: a "Refund Payment"
action on approved purchases (reason prompt + explicit confirmation
stating that other sources are unaffected), a refunded-state line, and a
"Refunded" status tab. No student-facing frontend change; no dashboard,
navigation or mobile work.

## 23. APIs changed

One new endpoint: `POST /api/purchases/{id}/refund/` (admin-role gated,
`{"reason": "..."}` required). Additive read-only response fields on
`PurchaseSerializer`: `refunded_at`, `refund_reason`. No existing endpoint
changed shape or behaviour. One new `Purchase.status` value (`refunded`)
and one new `PaymentAuditLog.action` value (`refunded`) — additive
enumerations.

## 24. Database migrations

**One**: `billing/0013_phase9_refund_and_grants.py` — adds
`Purchase.refunded_at`/`refunded_by`/`refund_reason`,
`GrandTestAccess.revoked_at`, the `PurchaseEntitlementGrant` table, and
alters two choice lists. All new fields are nullable/defaulted, so every
existing row is valid as-is; no data migration, no backfill needed
(historical purchases simply have no grant ledger — they predate it, and
`refund()` on such a purchase reverses nothing and says so in its audit
metadata rather than guessing, which is the honest behaviour). Applied
cleanly to the local dev DB; `makemigrations --check` clean.

## 25. Tests added

**41 new tests** in `billing/tests_phase9.py`: `RefundRevocationTests` (9),
`RefundNonInterferenceTests` (7 — the mandatory revocation matrix),
`EntitlementGrantTraceabilityTests` (4), `CouponRedemptionLimitTests` (5),
`CouponConcurrencyTests` (1, real threads), `PaymentIdempotencyTests` (2),
`CommerceAuthorizationTests` (8 — role matrix + IDOR + payload
manipulation), `RefundHistoricalIntegrityTests` (1),
`CronSecretHardeningTests` (3).

## 26. Full test-suite result

```
cd Backend && python manage.py test
```

**870/870 tests pass** (baseline after Phase 8: 829; this phase: +41 new,
zero regressions, zero pre-existing tests modified). Confirmed from the
run's own output (`Ran 870 tests in 358.556s` / `OK`). One small
correctness edit was made after that run started (skipping an
already-revoked grant instead of re-stamping it); the `billing` suite
(105 tests, covering all 41 new ones plus the pre-existing billing tests)
was re-run afterward and passes, and `makemigrations --check` re-confirmed
clean.

## 27. Query-count/performance impact

No query-count-pinned test covers the billing endpoints (verified by
search), so no baseline needed updating. Student hot paths are untouched
(§21).

## 28. Compatibility risks

- **Analytics figures move when a refund is issued.** `billing/analytics.py`
  counts revenue, paying users and plan breakdowns via `status='approved'`,
  so a refunded purchase drops out automatically. That is correct
  accounting and required no analytics change, but it does mean historical
  revenue totals decrease retroactively when refunds are issued — worth
  knowing before the first refund is processed in production.
- **Referral rewards are not clawed back** on refund (`_maybe_reward_referrer`
  credits the referrer's wallet at first approval). Out of scope; flagged.
- **A refunded purchase releases its per-user coupon allowance** (that check
  counts `status='approved'`) while the global `usage_count` slot stays
  consumed. These compose safely — the global cap still can't be exceeded —
  and the combination is documented rather than silently differing.
- Existing purchases carry no grant ledger (§24); refunding one is a safe
  no-op on entitlements, honestly reported in its audit entry.

## 29. Deferred work

Partial refunds; subscription cancellation semantics (no endpoint exists
today beyond `auto_renew`; "cancel now vs. access until end date" is an
unmade business decision); referral claw-back; the four sibling secrets in
§20; payment-gateway/webhook integration; Exam Builder (now explicitly
out-of-sequence, §2).

## 30. Deployment readiness

**Not deployed.** No production database was touched — all changes are
local working-tree edits plus a local SQLite dev-DB migration apply.
Deployment requires separate approval, as always. Note for whoever
deploys: the `CRON_SECRET` change means a non-`DEBUG` environment without
that env var will now **fail to start** instead of silently accepting a
public default. Production already injects it from Secret Manager
(verified in `cloudbuild.yaml`), but any other environment must set it.

---

**Summary of explicitly requested facts:**
- Baseline test count: 829
- Final test count: 870
- New tests: 41
- Migrations: 1 (`billing/0013_phase9_refund_and_grants.py`)
- `makemigrations --check` passed: yes
- Production DB touched: no
- Anything deployed: no
- Flaky tests: none observed or introduced
- Changed fixtures: none from prior phases
- Changed query-count baselines: none
- Remaining commerce risks: no gateway/webhook integration exists (so no
  automated payment verification, by design today); partial refunds
  unsupported; referral rewards not clawed back on refund; four
  non-commerce processing secrets still on insecure defaults, one of them
  (`DEDUP_PROCESSING_SECRET`) not wired in production at all.

Phase 9 complete. Stopping here per the mandatory stop, awaiting validation before Phase 10.
