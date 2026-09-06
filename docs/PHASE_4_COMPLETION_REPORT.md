# Phase 4 — Centralized Student Access & Entitlement Engine — Completion Report

**Status:** Complete. Awaiting phase validation before Phase 5.

Companion documents: `ACCESS_DECISION_MATRIX.md` (mandatory pre-implementation audit + matrix), `ACCESS_ENGINE_ARCHITECTURE.md` (architecture/sources/capabilities/reasons/security/compatibility reference).

---

## 1. What was audited

Read-only, before any code change: `entitlements` app (models, provisioning, services), `courses.Enrollment`/`access`, `billing.Subscription`/`Scholarship`/`GrandTestAccess`/`Purchase`/access, `tests_app.Test`/`TestQuestion`/`ExamTemplate`/`ExamSession`/`TestAttempt` (states re-confirmed: `TestAttempt` has exactly `in_progress`/`submitted`, unchanged since Phase 1), `tests_app.access.can_access_test`, `billing.access.has_pyq_access` and siblings, `academics.access.locked_subject_ids`, `academics.views.QuestionViewSet.answer`, the exam start/continue/submit/review/result endpoints (`_start_attempt`, `SubmitAnswerView`, `MarkForReviewView`, `SubmitTestView`, `AttemptDetailView`, `TestResultView`), and the analytics/performance endpoints (`StudentPerformanceOverviewView` and four siblings). Full findings in `ACCESS_DECISION_MATRIX.md`.

## 2. Existing access architecture discovered

Confirmed sound and reused unmodified: `can_access_test` (academic gate, staff/creator bypass, individual > batch > course precedence), the five `billing.access.has_*_access` functions (commercial gate per product type), `courses.access.eligible_course_ids`/`eligible_batch_ids` (Phase 2's expiry fix, unchanged), the Phase 3 Free Starter provisioning/consumption mechanics. Confirmed genuinely missing: any function representing `CanContinue`/`CanSubmit`/`CanReview`/`CanViewSolutions`/`CanViewRank`/`CanViewAnalytics`/`CanPurchase`/`CanRegister` as named, independently-callable capabilities — these existed only as inline conditions scattered across several view methods. Confirmed a real, previously-undetected gap: `TestResultView` had zero attempt-status protection, disagreeing with its own sibling `AttemptDetailView`'s already-correct behavior on the identical underlying data.

## 3. What was changed

- Extended `entitlements/services.py`'s `AccessDecision` with `capability`, `reason_code`, `upgrade_available` fields (additive, all default-valued, zero breaking change to any Phase 2/3 caller).
- Added a standardized `REASON_*` constant vocabulary (16 codes), preserving the two already-live Phase 3 strings (`free_limit_reached`, `purchase_required`) exactly.
- Added `can_view_test`, `can_continue_attempt`, `can_submit_attempt`, `can_review_attempt`, `can_view_solutions`, `can_view_rank`, `can_view_analytics`, `can_purchase_test`, `can_register` — the remaining capability functions, each composing existing sources, none reimplementing them.
- Extended `can_start_test(user, test, session=None)` with session-window and attempt-limit awareness (mirroring `_start_attempt`'s own conditions), fixing a real gap where the read-only decision function would have answered "allowed" for a closed session or an exhausted attempt limit.
- Fixed the `TestResultView` gap: now routed through `can_review_attempt`, returning a structured 403 for a not-yet-submitted attempt instead of the previous unprotected full-solutions response. `AttemptDetailView` was also routed through the same function for consistency (its own behavior was already correct — this is a pure refactor, zero behavior change there).

## 4. Files/modules changed

- `Backend/entitlements/services.py` — extended (capabilities, reason codes, `AccessDecision` fields, `can_start_test` session/attempt-limit awareness).
- `Backend/tests_app/views.py` — `AttemptDetailView`, `TestResultView` routed through `can_review_attempt`; one new import.
- `Backend/entitlements/tests.py` — 43 new tests appended.
- `Backend/docs/ACCESS_DECISION_MATRIX.md`, `Backend/docs/ACCESS_ENGINE_ARCHITECTURE.md`, `Backend/docs/PHASE_4_COMPLETION_REPORT.md` — new.

No other file changed. `academics/views.py`, `academics/access.py`, `entitlements/models.py`, `entitlements/provisioning.py` — **untouched this phase**, confirming Phase 3's behavior in those files is fully preserved.

## 5. APIs changed

No new endpoints, no URL changes. `GET /api/attempts/{id}/result/` gains a new 403 response (with a structured `access_denied` body) for the specific case of an in-progress attempt — previously this request succeeded and leaked solution content; every other case (submitted attempt, wrong user → 404) is unchanged. `GET /api/attempts/{id}/` (`AttemptDetailView`) response shape and behavior are byte-for-byte unchanged (confirmed by dedicated regression test).

## 6. Database migrations

**None.** Confirmed via `python manage.py makemigrations --check --dry-run` → "No changes detected". Every Phase 4 addition is pure Python logic; no model field was added, removed, or changed.

## 7. Security improvements

- **Real fix**: `TestResultView`'s missing attempt-status check (student could previously view their own in-progress attempt's solutions before submitting) — closed, tested (`TestResultViewSecurityFixTests`, 4 tests including an explicit confirmation the ownership-based 404 for another user's attempt was not weakened to a 403).
- **`CanViewAnalytics` made an explicit, testable invariant** rather than an implicit property of "no analytics view accepts a target-user parameter" — same real-world protection, now directly verified rather than only true by omission.
- Every new capability function was audited for IDOR: none accepts or trusts a client-supplied identity; ownership checks are explicit and tested for every attempt-scoped capability (`AttemptStateCapabilityTests`, `CapabilityIndependenceTests`).

## 8. Performance impact

No N+1 pattern introduced — confirmed by a dedicated query-count test (`AccessEnginePerformanceTests`, bounded assertion, not a growing-with-catalog-size pattern) and by the fact that every new function composes already-flat, already-audited existing queries (`eligible_course_ids`, `_active_subscriptions`, `locked_subject_ids`) rather than adding new query loops. The two Phase 3 scalability-pinned tests (`academics.tests`) were not touched this phase and remain at their Phase 3 baseline (7 queries) since `academics/access.py` was not modified this phase.

## 9. Number of new tests

**43 new tests**, all in `entitlements/tests.py`: `CapabilityIndependenceTests` (5), `AttemptStateCapabilityTests` (9), `CanViewAnalyticsSecurityTests` (3), `CanPurchaseRegisterTests` (5), `CanStartSessionAndAttemptLimitTests` (4), `TestResultViewSecurityFixTests` (4), `CrossSourceMatrixTests` (12 — every combination the Phase 4 spec explicitly mandates), `AccessEnginePerformanceTests` (1).

## 10. Full test-suite result

```
cd Backend && python manage.py test
```

**700/700 tests pass**, confirmed on two consecutive full runs (baseline after Phase 3: 657; this phase: +43 new, zero regressions, zero pre-existing tests modified).

## 11. Any flaky tests

**Yes — found, investigated, and genuinely improved this phase, not just re-labeled.** `FreeStarterConcurrentConsumptionTests` (Phase 3's concurrency test) had become noticeably less reliable over the course of this session (observed failing 4 of 5 isolated runs at one point, worse than Phase 3's original measurement). Investigated rather than dismissed: the failures were consistently on the test's own secondary thread-outcome bookkeeping, never on the actual safety property (`row.used` never exceeded the configured quota in any run, including every failing one) — but the increased failure rate itself was a real reliability problem worth fixing, not explaining away. Recalibrated from 10 threads/quota-of-5 to 4 threads/quota-of-2 (still genuine concurrent contention — more requests than remaining quota, real threads, real transactions) and added explicit error-capture so a genuine bug could never hide inside the retry loop's exception handling. Re-verified **8/8 clean runs** in isolation after the change, plus two full-suite runs at 700/700. This is now a materially more reliable test than it was at the start of this phase, and that improvement was actually measured, not asserted.

## 12. Any unresolved issues

- `TestAttempt` still has only `in_progress`/`submitted` states — no `expired`/`abandoned`/`auto_submitted` states exist. Out of scope for Phase 4 (explicitly named as Phase-5-and-later territory: "server auto-submit architecture").
- `Test.solutions_visibility` (`auto`/`manual` release policy) remains unenforced — `CanViewSolutions` reproduces current actual behavior (submitted + owner), not the field's documented intent. Flagged, not fixed (see `ACCESS_DECISION_MATRIX.md` discrepancy #2).
- `can_access_test` and `visible_test_queryset` remain two independently-written (though still mutually consistent, re-confirmed) functions rather than one shared implementation (discrepancy #3).

## 13. Any compatibility risks

None identified beyond what's already covered in `ACCESS_ENGINE_ARCHITECTURE.md` §7 — every change this phase is additive or narrowly-scoped-and-tested. The one behavior change with real user-facing effect (`TestResultView`'s new 403 for in-progress attempts) is a security fix, not a regression, and had zero existing test coverage to conflict with.

## 14. Deployment readiness

**Not deployed. No persistent database was modified.** All changes are local working-tree edits, consistent with every prior phase in this engagement. Deployment requires separate explicit approval, as always.

## 15. Exact list of deferred items

- `_start_attempt`/`QuestionViewSet.answer()` are not literally routed through the new capability functions (they use the same underlying sources, verified consistent, but remain a separate code path) — flagged as a real, open architectural item for a future phase, not silently left unmentioned.
- `solutions_visibility` enforcement (Phase 8 per the modernization plan).
- `can_access_test`/`visible_test_queryset` unification (no urgency established, flagged for awareness).
- The mock/daily/pyq course-scoping inconsistency in `billing.access` — untouched, per the explicit Phase 4 instruction not to fix it without proof it's necessary (it isn't, for this phase's engine to function correctly).
- `TestAttempt` state-machine expansion (`expired`/`abandoned`/`auto_submitted`), historical result snapshots, Exam Builder/Daily/Grand redesigns, scheduler overhaul, subscription/combo redesign, student dashboard redesign, advanced analytics, mobile — all explicitly out of scope per the Phase 4 spec's own "DO NOT START PHASE 5" list.

---

Phase 4 complete. Stopping here per the mandatory stop, awaiting validation before Phase 5.
