# Phase 10 — Student UX Architecture

> Scope: make the student-facing UI render the **backend's** access
> decisions instead of inventing its own. No new authorization, no new
> enforcement, no Exam Builder (deferred, out of the numbered sequence).

---

## 1. The problem Phase 10 exists to fix

Phase 4 built a capability engine (`entitlements/services.py`: ten
capabilities, `AccessDecision`, seventeen `REASON_*` codes). Phase 10's
audit found it was **completely unconsumed by the student app** — zero
calls to any `entitlements/` endpoint anywhere in `Frontend/src`.

In its place, `ExamCard.js` carried this line:

```js
const locked = test.is_pro && card_status !== "completed";
```

Lockedness inferred **from price alone**. Every student who legitimately
held access by a route other than "already completed it" — an active
subscription, a scholarship, a direct Grand Test purchase, an individual
assignment, a Free Starter grant — was shown a padlock on an exam the
backend would have happily started. The card said "locked"; `POST
/tests/{id}/start/` said "yes". The UI was wrong, not the server.

This is the drift Phase 10 removes: **one decision, made once, on the
server, rendered verbatim by the client.**

---

## 2. What was built

### 2.1 `tests_app/card_access.py` — the card contract

A batched projection of `can_start_test()` producing, per card:

| field | meaning |
|---|---|
| `state` | one of `continue` / `review` / `start` / `upcoming` / `closed` / `attempts_exhausted` / `locked` |
| `can_start`, `can_continue`, `can_review` | booleans derived from `state` |
| `reason_code` | a Phase 4 `REASON_*` constant when denied |
| `upgrade_available` | is there something purchasable that fixes this |
| `source` | which entitlement grants it (`SOURCE_*`) |
| `attempts_left`, `latest_attempt_id`, `in_progress_attempt_id` | what the primary button needs to link to |

A single `state` rather than a pile of independent badges, so the UI
renders one primary affordance instead of choosing between several.

### 2.2 Why a projection and not a second engine

The canonical decision remains `entitlements.services.can_start_test()`.
This module exists only because a catalog page renders ~20 cards and the
canonical function costs 6–9 queries each.

Three things keep it from becoming a divergent second implementation:

1. **Identical branch order.** `resolve_card_access()` evaluates the same
   branches in the same order: in-progress attempt → session window →
   review/attempts → `is_pro` → grand/product access → Free Starter.
2. **An agreement test.** `CardAccessMatchesCanStartTestTests` asserts
   card-by-card agreement with `can_start_test()` across the entitlement
   matrix. A change to one that isn't mirrored in the other fails the
   suite instead of silently drifting.
3. **A bounded query budget.** `CardAccessApiTests` pins the query count
   and asserts it does **not grow with the number of cards**.

Two things it deliberately does **not** re-derive:

- **Academic eligibility** — every list endpoint already runs
  `visible_test_queryset(user, qs)`, so a Test that reaches a card has
  passed that gate by construction.
- **Enforcement** — nothing here grants anything. The authoritative check
  still runs at `_start_attempt` / `SubmitTestView` / `TestResultView` on
  the actual action. This decides what a *button* looks like.

### 2.3 `StudentEntitlementSnapshot` — bounded queries

Every commercial fact needed for a whole page, fetched once: product
access memoized **per product type** (exact, not approximate —
`has_mock_test_access` / `has_daily_test_access` / `has_pyq_access`
resolve purely from `(user, product_type)` and only touch `test` for the
`is_pro` short-circuit already handled before the call); live Grand Test
grants in one query filtered on `revoked_at__isnull=True` (Phase 9);
Free Starter availability memoized per bucket.

Built per request, cached on serializer context, **never across
requests** — a purchase approved seconds ago shows on the next load.

### 2.4 `tests_app/preview.py` — admin "preview as student"

Per the plan: *read-only, must not create real rows, implement as a
request-scoped override of the capability functions' input user context,
not a real impersonation session.*

So it is **not impersonation**: no session switch, no token, no
`request.user` reassignment. It answers one question — "which user should
this GET's visibility and capability questions be asked about" — and
every other part of the request stays the admin's.

- GET-only, admin-role staff only, staff targets rejected.
- A non-admin passing `?preview_as=` is ignored entirely, not errored:
  silently rendering your own view is the safe failure.
- The one write a normal entitlement check performs — Phase 3's lazy
  provisioning of a missing `FreeStarterEntitlement` — is **suppressed**
  in preview mode (`StudentEntitlementSnapshot(read_only=True)`), so
  previewing a student cannot create rows against their account.
- Start / answer / submit / purchase never consult this module, so an
  admin cannot consume a student's attempt or quota by previewing.

### 2.5 `Frontend/src/lib/accessState.js` — the single presentation mapping

One module maps the server's `access` block to what the card shows:
`cardPresentation`, `primaryHref`, `isLocked`, `denialCopy` (per-reason
copy for eleven reason codes), `sourceLabel`, `quotaFor`.

Its rule: **the frontend never decides, only renders.** `isLocked` reads
`state === 'locked'`; it does not look at `is_pro`. The regression test
that pins this is literally *"a pro test the backend says is startable is
NOT locked."*

### 2.6 Plan features moved server-side

`plans/page.js` held four hardcoded feature arrays (`QBANK_FEATURES`,
`PYQ_FEATURES`, `MOCK_FEATURES`, `DAILY_FEATURES`) that could drift from
what a plan actually granted. Replaced by `SubscriptionPlan.features`
(admin-editable JSON) exposed as `display_features`, which falls back
server-side to a line derived from the plan's **real** duration/quota
fields — so an unconfigured plan still describes itself truthfully.

---

## 3. One real backend fix

`can_start_test()`'s grand-test branch denied access when the student had
no `GrandTestAccess` row, without consulting Free Starter — while
`_start_attempt` **did** fall back to the Free Starter grant. The card
would say "locked" on an exam the start endpoint allowed.

Fixed by adding the `_try_free_starter(user, 'grand_test')` fallback to
the grand branch, bringing the capability function in line with the
enforcement path it is supposed to describe. This is the *engine* being
corrected to match enforcement, not the UI being taught a special case.

---

## 4. Security posture — stated plainly

Phase 10 changed **no authorization**. Specifically:

- Nothing in `card_access.py` or `accessState.js` grants access. Removing
  every line of both would not let a student start one additional exam.
- A student who hand-calls `POST /tests/{id}/start/` receives the same
  decision as before, from the same code, for the same reasons.
- Hiding a button is not a security control and is not claimed as one
  anywhere in this phase. The card contract exists so the UI stops
  *over-*locking legitimate students, which is a correctness fix, not a
  hardening one.
- The one genuine security-relevant addition is preview mode's read-only
  guarantee, which is a restriction (suppressing a write), not a grant.
- No internal entitlement IDs are exposed: the `access` block carries a
  `source` **type** (`subscription`, `free_starter`, …), never a
  subscription/purchase/grant row id, and no price, invoice, or refund
  data.

---

## 5. Deliberately not done

- **Exam Builder** — deferred, out of the numbered execution sequence.
- **Question versioning UI** — Phase 8 decided snapshots, not versions.
- **No new caching layer.** Per-request memoization only; a stale card
  after a purchase would be a worse bug than a few extra queries.
- **No frontend re-derivation of academic eligibility**, session windows,
  or timing. Server time governs; the browser clock gets no vote.
