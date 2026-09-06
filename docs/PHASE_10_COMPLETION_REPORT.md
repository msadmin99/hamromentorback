# Phase 10 — Student UX — Completion Report

**Status:** complete. Full backend suite `Ran 909 tests — OK`.
Frontend `24/24` pass, build ✓. Nothing deployed. Production DB untouched.

---

## 1. What the audit found

The Phase 4 capability engine — ten capabilities, `AccessDecision`,
seventeen `REASON_*` codes — was **completely unconsumed by the student
app**. `grep` for `entitlements/` across `Frontend/src` returned nothing.

In its absence `ExamCard.js` had invented its own rule:

```js
const locked = test.is_pro && card_status !== "completed";
```

Lockedness from **price alone**. A student holding legitimate access via
subscription, scholarship, direct Grand Test purchase, individual
assignment, or Free Starter saw a padlock on an exam the backend would
have started. The card and the server disagreed, and the card was wrong.

## 2. What changed

| Area | Change |
|---|---|
| `tests_app/card_access.py` (new) | Batched projection of `can_start_test()` → a 10-field `access` block per card |
| `tests_app/preview.py` (new) | Admin "preview as student": GET-only, admin-role-only, read-only |
| `tests_app/serializers.py` | `access` on `TestListSerializer`; `preview_only` on `TestDetailSerializer`; `card_status` marked legacy, retained |
| `tests_app/views.py` | Preview user resolved before `visible_test_queryset` |
| `entitlements/services.py` | **Bug fix** — grand branch now falls back to Free Starter, matching `_start_attempt` |
| `billing/models.py` + serializer + migration `0014` | `SubscriptionPlan.features` / `display_features` |
| `Frontend/src/lib/accessState.js` (new) | The single presentation mapping — renders, never decides |
| `Frontend/src/components/ExamCard.js` | `is_pro ⇒ locked` deleted; renders the server's `state` |
| `Frontend/src/app/tests/[id]/page.js` | Denial copy + source label from the server |
| `Frontend/src/app/plans/page.js` + 2 sections | Four hardcoded feature arrays deleted → `plan.display_features` |
| `Frontend/src/components/FreeAccessSummary.js` (new) | Reads `GET /api/entitlements/mine/` |

## 3. The one real backend bug fixed

`can_start_test()`'s grand-test branch denied access when no
`GrandTestAccess` row existed, without consulting Free Starter — while
`_start_attempt` **did** fall back to the Free Starter grant. The
capability function contradicted the enforcement path it describes. Fixed
by adding `_try_free_starter(user, 'grand_test')` to that branch: the
engine was corrected to match enforcement, rather than the UI being
taught a special case.

## 4. The N+1 the query-count test caught

The first `has_product_access` implementation called `has_*_access` per
card: **+1 query per card** (10 → 14 queries for 1 → 5 cards). Fixed by
memoizing per product type, after confirming those functions resolve
purely from `(user, product_type)` and only touch `test` for an `is_pro`
short-circuit already handled before the call — so the memoization is
exact, not an approximation. `CardAccessApiTests` now pins that the query
count does **not grow with card count**.

## 5. Security posture — stated plainly

Phase 10 changed **no authorization**.

- Deleting every line of `card_access.py` and `accessState.js` would not
  let a student start one additional exam.
- A student hand-calling `POST /tests/{id}/start/` receives the same
  decision as before, from the same code.
- **No security improvement is claimed here on the basis of a hidden
  button.** The card contract stops the UI *over*-locking legitimate
  students — a correctness fix, not a hardening one.
- The one security-relevant addition is preview mode's read-only
  guarantee, which suppresses a write (Phase 3's lazy Free Starter
  provisioning) rather than granting anything.
- No internal entitlement IDs, prices, invoices, or refund data are
  exposed: `source` is a **type** (`subscription`, `free_starter`, …),
  never a row id.

## 6. Verification

| Check | Result |
|---|---|
| Backend suite | `Ran 909 tests in 372.307s` — **OK** |
| Baseline → final | 870 → 909 (**+39** backend tests) |
| Frontend tests | **24/24 pass** (`node --test`, zero new dependencies) |
| Frontend build | ✓ Compiled successfully in 3.3s; 35/35 static pages |
| `makemigrations --check` | **No changes detected** |
| Migrations added | 1 — `billing/0014_phase10_plan_features.py` |
| Lint | 44 pre-existing findings app-wide; none introduced by this phase (verified against `git diff`) |
| Production DB | **not touched** |
| Deployed | **no** |
| Flaky tests | none |

The suite log contains `apply_question_stats_deltas: retryable DB error
(errno 1213/1205)` lines. These are the deadlock-retry path's own
logging from the tests that deliberately exercise it, not failures.

## 7. API compatibility

Additive only. `access` (list), `preview_only` (detail), and
`features`/`display_features` (plans) are new fields. `card_status` is
**retained** and still populated — marked legacy in its docstring — so
no existing consumer breaks.

## 8. Not done, deliberately

- **Exam Builder** — deferred, out of the numbered execution sequence.
- No new caching layer (per-request memoization only, so a purchase
  approved seconds ago shows on the next load).
- No frontend re-derivation of academic eligibility, session windows, or
  timing. Server time governs.

## 9. Correction to earlier phase reports

Phases 6–9's reports stated the project "is not a git repository." That
is true of the project root only. `Backend/` and `Frontend/` are each
their own git repository. Nothing has been committed in any phase.
