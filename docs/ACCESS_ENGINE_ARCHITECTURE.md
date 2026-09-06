# Access Engine Architecture — Phase 4

Companion document: `ACCESS_DECISION_MATRIX.md` (the audit findings and per-source/per-capability matrix this architecture implements). This document covers the five requested areas: access architecture, access sources, capability semantics, denial reasons, security model, and compatibility notes.

---

## 1. Access architecture

Five distinct concepts, deliberately never collapsed into one:

```
CATALOG           →  is this resource discoverable at all?
     ↓                 (can_view_test / can_view_qbank — academic-only,
                        never checks commercial entitlement)
ENTITLEMENT        →  does the student have a valid source of access?
     ↓                 (commercial_entitlement / _try_free_starter — the
                        UNION of every commercial source, never a single
                        boolean)
CAPABILITY         →  what specifically can the student do right now?
     ↓                 (CanView/CanStart/CanContinue/CanSubmit/CanReview/
                        CanViewSolutions/CanViewRank/CanViewAnalytics/
                        CanPurchase/CanRegister — ten independent
                        functions, each composing the layers below it)
ATTEMPT            →  does a TestAttempt exist, and what state is it in?
     ↓                 (in_progress / submitted — checked by can_continue_
                        attempt/can_submit_attempt/can_review_attempt,
                        never re-deriving entitlement)
SESSION            →  is the delivery window (ExamSession) open?
                       (checked by can_start_test when a session is
                        passed — status/start_datetime/end_datetime)
```

**Why this separation matters, concretely:** a pro Mock Test with no purchase is `CanView=True` (it's in the catalog) but `CanStart=False` (no entitlement) — the student sees it, understands its value, and gets a clear upgrade path, without the backend ever pretending a single `has_access` boolean could represent both facts at once. An entitled student whose Daily Test session has already closed is `CanStart=False` for a completely different reason (`exam_closed`, not `purchase_required`) — the ENTITLEMENT layer and the SESSION layer are independent checks, and conflating them would produce a wrong or misleading denial reason.

## 2. Access sources — implementation reference

See `ACCESS_DECISION_MATRIX.md` §Access sources for the full re-verified table (model, expiry, revocation per source). Summary of where each is composed:

- **Free Starter** → `entitlements.services._try_free_starter` (read-only) / `entitlements.provisioning.{has_free_starter_available,ensure_and_consume_free_starter}` (the real, consuming path used by live endpoints).
- **Subscription, Scholarship** (indistinguishable to the student except by `source_type` in the response — Scholarship is never its own check, only a `Subscription` row's reverse `.scholarship` relation) → `entitlements.services.commercial_entitlement`.
- **Combo** → not a distinct check; resolves into ordinary `Subscription` rows at purchase time, then flows through `commercial_entitlement` identically.
- **Direct Purchase (Grand Test)** → `billing.access.get_grand_test_access`, composed inline in `can_start_test`'s `exam_type == 'grand'` branch.
- **Course Enrollment** → `entitlements.services.academic_eligibility` (used directly for course-level `CanView`); `tests_app.access.can_access_test` (used for Test-level `CanView`/the academic prerequisite of `CanStart`) reads the same underlying `courses.access.eligible_course_ids`.
- **Batch/Individual Assignment, Staff override** → both live entirely inside `tests_app.access.can_access_test`, reused unmodified — the access engine does not re-implement this precedence, it calls the existing, already-correct function.

## 3. Capability semantics

Full definitions and the matrix of what each capability requires are in `ACCESS_DECISION_MATRIX.md` §Capability semantics / §Access Decision Matrix. Function reference:

| Capability | Function | Resource argument |
|---|---|---|
| `CanView` | `can_view_test(user, test)` / `can_view_qbank(user, subject)` | `Test` / `Subject` |
| `CanPurchase` | `can_purchase_test(user, test)` | `Test` |
| `CanRegister` | `can_register()` | none |
| `CanStart` | `can_start_test(user, test, session=None)` | `Test` (+ optional `ExamSession`) |
| `CanContinue` | `can_continue_attempt(user, attempt)` | `TestAttempt` |
| `CanSubmit` | `can_submit_attempt(user, attempt)` | `TestAttempt` |
| `CanReview` | `can_review_attempt(user, attempt)` | `TestAttempt` |
| `CanViewSolutions` | `can_view_solutions(user, attempt)` | `TestAttempt` |
| `CanViewRank` | `can_view_rank(user, attempt)` | `TestAttempt` |
| `CanViewAnalytics` | `can_view_analytics(user, target_user)` | a `User` (always self in every current caller) |

Every function returns an `AccessDecision` (see §5 below) with `.capability` set to the matching constant (`entitlements.services.CAN_VIEW`, etc.) — a caller (or a test) can always confirm which capability a given decision answers, not just whether it was allowed.

## 4. Denial reasons

`entitlements.services` exports 16 standardized `REASON_*` constants (see the module for the full, exact list) — one clear meaning each, no overlapping pairs. Two are preserved byte-for-byte from Phase 3 (`free_limit_reached`, `purchase_required`) since they're already load-bearing in live API response shapes (`_free_starter_denied_payload()` in `tests_app/views.py`, the `code` field on several 402 responses) — renaming either would be a breaking change to an existing, documented contract, which the Phase 4 spec explicitly forbids without a compatibility plan.

## 5. `AccessDecision` — the canonical representation

```python
@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    capability: str = ''
    source_type: str = SOURCE_NONE
    source_id: Optional[int] = None
    valid_from: Optional[object] = None
    expires_at: Optional[object] = None
    remaining: Optional[int] = None
    reason: str = ''
    reason_code: str = ''
    upgrade_available: bool = False
```

This is the Phase 2/3 dataclass, **extended additively** (`capability`, `reason_code`, `upgrade_available` are new fields with defaults) rather than replaced — every existing Phase 2/3 construction of `AccessDecision(...)` (none of which passed these three) continues to work unchanged, and no test that inspects `.as_dict()`'s contents by key (confirmed via a full-file grep — none do exact-dict-equality) could break from the additional keys.

`.as_dict()` is deliberately the **only** thing ever serialized into an HTTP response — never a raw model instance, never internal implementation state. It contains no password, no payment reference, no other user's data, and no reference to *how* an internal check was implemented — only the outcome, the source, and a safe, generic reason.

## 6. Security model

- **Every decision function takes the real Django `User` object obtained from `request.user` — never a client-supplied ID.** Confirmed for every new Phase 4 function and every existing endpoint this phase touched.
- **Ownership is enforced at the query layer before a capability function is ever called** (`get_object_or_404(TestAttempt, pk=attempt_id, user=request.user)` — a request for another user's attempt 404s before reaching any capability check at all, so a capability-level denial can never be used to distinguish "exists but not yours" from "doesn't exist," which would itself be a minor information leak).
- **`CanViewAnalytics` is the one capability that takes an explicit `target_user` argument** rather than only ever implicitly meaning "self" — it exists specifically so this invariant (self-only, or staff) is a real, tested check rather than an implicit property of every analytics view never accepting a target-user parameter at all.
- **Solutions security fix**: `TestResultView` previously had no attempt-status check at all (see `ACCESS_DECISION_MATRIX.md` discrepancy #1) — a real, confirmed information-disclosure gap, now closed via `can_review_attempt`.

## 7. Compatibility / migration notes

- **No database migration this phase** — every addition is pure Python (new functions, new dataclass fields with defaults). Confirmed via `makemigrations --check --dry-run`.
- **`can_start_test`'s signature changed from `(user, test)` to `(user, test, session=None)`** — fully backward compatible (every Phase 2/3 call site omits `session`, which behaves identically to before this phase; the new session-window/attempt-limit checks are additive and only activate when a session is actually passed, or — for the attempt-limit check specifically — always active but only ever matters once an attempt already exists, which no Phase 2/3 test fixture created before calling it).
- **`AttemptDetailView`'s and `TestResultView`'s response shapes are unchanged for every already-passing case** — `TestResultView` gains a new 403 response only for the specific, previously-unprotected in-progress-attempt case; every submitted-attempt request (the only case any pre-Phase-4 code could have been relying on, since none of it was tested) behaves exactly as before.
- **`_start_attempt`/`QuestionViewSet.answer()` (Phase 3's live consumption endpoints) were NOT rewired to call the new capability functions this phase** — a deliberate, documented decision (see `PHASE_4_COMPLETION_REPORT.md` §Deferred items), not an oversight. They already correctly use the same underlying source functions (`has_mock_test_access` et al., `has_free_starter_available`/`ensure_and_consume_free_starter`) that the capability layer also uses — the two are consistent in behavior, verified by the cross-source test matrix, even though they are not literally one shared code path yet.
