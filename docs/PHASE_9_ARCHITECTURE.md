# Phase 9 — Commercial Integrity: Architecture

Companion document: `PHASE_9_COMPLETION_REPORT.md`.

---

## 0. Phase numbering — normalized, permanently

Phases 6, 7 and 8 each opened with a numbering conflict and were resolved
by swapping two adjacent phases. That never fixed the cause: **"Exam
Builder"** sat in the sequence at Phase 6 and every kickoff prompt from
Phase 6 on described the next *backend-integrity* phase, so Exam Builder
was pushed down one slot each time (6 → 7 → 8 → 9) and regenerated the
same off-by-one. Four conflicts, one root cause.

Normalized at the start of this phase, on explicit instruction: Exam
Builder is **out of the numbered sequence** (documented as
DEFERRED / OUT-OF-SEQUENCE in `implementation-plan.md`, scope unchanged,
to be scheduled deliberately) and the remaining substantive phases are
contiguous, so **phase number = execution order**:

| Before | Feature | After |
|---|---|---|
| 1–8 | Security … Historical Integrity (complete) | 1–8, unchanged |
| 9 | Exam Builder | deferred, no number |
| 10 | **Commercial Integrity** | **9 (this phase)** |
| 11 | Student UX | 10 |
| 12 | Analytics / Performance / Accessibility | 11 |

Completed phases keep their numbers, scope, and reports exactly as
executed.

## 1. The commerce data flow, as it actually is

```
SubscriptionPlan / ComboPlan / Test(grand) / TeacherCourse
        │
        ▼  PurchaseViewSet.create()  — server recomputes price; coupon/referral
Purchase (status='unpaid')             discount applied server-side, never trusted
        │                              from the client
        ▼  submit-payment  (student uploads proof + reference)
Purchase (status='pending')
        │
        ▼  ManualQRProvider.approve → payment_service.activate()
        │      · row-locked, precondition re-checked under the lock
        │      · coupon caps re-checked under a Coupon row lock  ← NEW
        │      · _activate_product() materializes access
        │      · PurchaseEntitlementGrant written per access record  ← NEW
        │      · coupon usage_count += 1 (atomic F())
        ▼
Subscription / GrandTestAccess / marketplace.CourseEnrollment
  (+ courses.Enrollment, shared per user+course, via _ensure_enrollment)
        │
        ▼  billing.access.has_*_access / get_grand_test_access
Phase 4 AccessDecision  →  CanStart / CanView / …
```

**There is no payment gateway and no webhook.** Verification is a human
admin reviewing an uploaded screenshot (`billing/payment_providers.py`,
`ManualQRProvider` is the only implementation). Every claim in this phase
about payment security is about *that* flow — see §8.

Phase 9 adds one reverse arrow to this picture and nothing else:

```
payment_service.refund()  →  PurchaseEntitlementGrant  →  reverse exactly
                                                          those records
```

## 2. What the audit found already correct (re-verified, not assumed)

- **Financial authorization (Phase 1) intact.** `approve`, `reject`,
  `request-resubmission`, `audit-log`, `GrantAccessView`,
  `ScholarshipViewSet` (incl. `revoke`), `CouponViewSet`, `AnalyticsView`
  are all `IsAdminRoleOrAbove` — not bare `IsStaff`/`IsAdminUser`.
- **Scholarship/paid separation (Phase 2) intact.**
  `_extend_or_create_subscription` scopes its "existing row" lookup by
  origin (`scholarship__isnull=not is_scholarship`), so a scholarship and
  a paid subscription for the same (user, course, product) are always
  *different rows*. Revoking one structurally cannot touch the other.
- **Every Purchase transition is already locked, guarded, audited,
  notified** — `select_for_update()` inside `transaction.atomic()`, the
  precondition re-checked *under* the lock, a `PaymentAuditLog` row, and a
  student notification. Phase 9 followed this existing shape exactly
  rather than inventing a second pattern.
- **IDOR on purchases** — `PurchaseViewSet.get_queryset()` filters to
  `user=request.user` for non-staff, so another student's purchase 404s.
- **Client cannot declare a payment successful** — a student can only
  reach `pending`; only an admin action reaches `approved`. Prices are
  recomputed server-side at creation (`compute_price`), never taken from
  the request.
- **Purchase/combo historical amounts** already snapshotted
  (`Purchase.original_amount`/`final_amount`, `PurchaseComboItem.price`) —
  the plan says preserve as-is; nothing changed.

## 3. Gap 1 — refunds did not exist

Confirmed by repo-wide search: **zero** occurrences of "refund" anywhere
in the codebase. No status, no model, no endpoint. Worse, the linkage a
refund would need was missing too: `GrandTestAccess.purchase` was the only
traceable grant; a `Subscription` carried no record of which Purchase paid
for it, so nothing could tell what to undo.

### The design

`Purchase.status` gains `'refunded'` (+ `refunded_at`/`refunded_by`/
`refund_reason`) rather than a separate `Refund` model: with no gateway, a
refund here is one administrative decision about one order, not a
multi-step transaction with its own lifecycle. **Partial refunds are not
supported** — deliberately: that needs a business decision about what a
*partial* entitlement reversal even means, and inventing one silently
would be worse than not having it.

`PurchaseEntitlementGrant` is the new, narrow ledger that makes reversal
possible: one row per access record a purchase actually granted, written
by `_activate_product()`, read only by `refund()`. It records
`was_created` (did this purchase create the record or extend it),
`previous_expires_at` and `granted_expires_at`. It is **not** a general
"UserAccess" table — nothing ever grants access from it; every access
decision still reads the real Subscription/GrandTestAccess/Enrollment rows
through the Phase 4 engine.

### The reversal rule (and the bug the tests caught)

The first implementation used the obvious rule: *deactivate what this
purchase created, roll back what it extended*. The mandatory cross-source
tests immediately caught that this is wrong, and wrong in exactly the way
the spec forbids. Several purchases can accumulate onto **one**
subscription row — a combo creates it, a later direct purchase extends it.
Deactivating the row because this purchase happened to be the one that
created it destroys the other purchase's separately-paid-for time.

The shipped rule:

- **This is the only live grant on the record, and it created it** → the
  record exists solely because of this purchase → deactivate it.
- **Otherwise** (another purchase's live grant also points at this record)
  → subtract only *this grant's own contributed duration* from the current
  expiry. The record stays active; it just covers less time. Every other
  purchase's contribution survives untouched.
- **Lifetime/unbounded grant shared with another purchase** → left
  unchanged, and the audit metadata says so. There is no arithmetic that
  removes one purchase's share of "forever" without taking someone else's
  access with it, so it is reported rather than guessed at.

### What a refund deliberately does NOT touch

- Any other purchase's grants, any scholarship, any combo the student also
  bought, Free Starter quota, or another student's anything.
- **`courses.Enrollment`** — one shared row per (user, course) maintained
  by `_ensure_enrollment` for *every* source. Deactivating it would break
  access the student still legitimately holds via another purchase or a
  scholarship. It governs catalog visibility, not paid entitlement: every
  `has_*_access()` check requires a live `Subscription`, which a refund
  *does* reverse. Tested explicitly.
- **Exam history** — finalized attempts, scores, ranks, and Phase 8
  `AttemptQuestionSnapshot` rows. Refunding money does not rewrite what a
  student did. Tested explicitly.

`GrandTestAccess` needed a `revoked_at` flag because that entitlement is
presence-based (`get_grand_test_access` just looks the row up); deleting
the row would destroy the issued password and the audit trail, so it is
flagged and filtered out at the single resolution point instead. Buying
again after a refund clears the flag.

## 4. Gap 2 — coupon over-redemption

`Coupon.max_uses`/`max_uses_per_user` were checked **only at Purchase
creation** (`billing/views.py: compute_price`), while `usage_count` was
incremented at **approval**. Those are separated by a human review step,
so the creation-time check could not cap anything: N students could each
create an order against a `max_uses=1` coupon while `usage_count` was
still 0, and every one of them would later be approved. That is a
systematic over-redemption *with no concurrency at all*, with a classic
TOCTOU race on top of it.

Fixed where redemption actually happens — inside `activate()`, in the
existing transaction:

```python
coupon = Coupon.objects.select_for_update().get(pk=purchase.coupon_id)
# re-check max_uses and max_uses_per_user under the lock
...
Coupon.objects.filter(pk=...).update(usage_count=F('usage_count') + 1)
```

The row lock serializes concurrent approvals of the same coupon, so the
check and the increment are one atomic unit and the (max_uses + 1)th
approval always loses. An over-limit approval raises `PaymentError`, which
the endpoint turns into a 400 and leaves the purchase `pending` — an admin
sees a clear reason rather than a silent grant. Creation-time checks are
kept as the fast, friendly pre-check they always were.

**A refund does not return the coupon slot.** That is a deliberate,
documented business decision, not an oversight: it keeps `max_uses` a hard
ceiling that a buy/refund cycle cannot be used to farm past. If the
business would rather return the slot, it is a one-line change in
`payment_service.refund()` — made deliberately.

## 5. Gap 3 — CRON_SECRET insecure default

`CRON_SECRET` fell back to a hardcoded, source-visible
`'dev-cron-secret-change-me'` whenever the env var was missing, and two
`/api/cron/*` endpoints accept it. A deployment that failed to inject the
secret would silently accept that known string from anyone.

Production does wire it (`CRON_SECRET=cron-secret:latest` in
`cloudbuild.yaml`, verified), so the fix is to make the failure loud
instead of silent: outside `DEBUG` the setting now raises
`ImproperlyConfigured` at startup if unset, and both `_check_cron_secret`
helpers fail closed on an empty configured secret so a blank value can
never match a blank header.

**Flagged, not fixed** (out of this phase's scope): four sibling secrets
(`MEDIA_PROCESSING_SECRET`, `IMPORT_PROCESSING_SECRET`,
`DEDUP_PROCESSING_SECRET`, `STATS_PROCESSING_SECRET`) have the identical
insecure-default pattern, and `DEDUP_PROCESSING_SECRET` is not in
`cloudbuild.yaml`'s secret list at all — meaning it currently runs on its
public default in production. They gate content-processing endpoints, not
commercial ones, so fixing them here would be unrelated work; the same
two-line pattern applies to each when they are scheduled.

## 6. Entitlement composition — unchanged, and that is the point

Multiple sources remain additive, resolved by asking "does **any** valid
source grant this?" — `billing.access._active_subscriptions()` +
`get_grand_test_access()` + Free Starter + academic eligibility, composed
by the Phase 4 engine. Phase 9 introduced **no precedence order** and no
new access engine. The only thing it can do is remove *one specific
purchase's own* contribution; every other source answers the same question
the same way afterward. That is what the cross-source and revocation test
matrices verify.

## 7. Phase integration

- **Phase 3 (Free Starter)** — untouched. A refund never consumes,
  restores, or inspects quota; Free Starter remains one independent source.
- **Phase 4 (Access engine)** — still authoritative and unchanged. Commerce
  writes entitlement state; it never sets a capability. The one access-layer
  edit is `get_grand_test_access()` filtering `revoked_at`, i.e. the
  existing single resolution point telling the truth about a revoked grant.
- **Phase 6 (sessions/timing)** — untouched. A valid subscription still
  cannot open a closed exam window.
- **Phase 7 (results)** — untouched. Review/solution access follows the
  existing policy; nothing in refund touches it.
- **Phase 8 (snapshots)** — untouched, and explicitly tested: refunding
  after an exam leaves the attempt, score, answers, and snapshots intact.

## 8. Honest limits of this phase

- **No webhook security was implemented or verified, because there are no
  webhooks.** Payment verification is manual admin review. If a gateway is
  added later, `payment_providers.py` is the seam for it and
  `payment_service.activate()/reject()` stays the only thing that touches
  the database — signature verification and callback idempotency would be
  that phase's work, not something this phase can claim.
- **Partial refunds are not supported** (§3).
- **Subscription cancellation** has no explicit endpoint today (only
  `auto_renew` toggling); this phase did not invent one, since the plan
  does not call for it and the "cancel now vs. access until end date"
  semantics are an unmade business decision.
- **The four sibling secrets** in §5 remain on insecure defaults.
