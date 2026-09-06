# Phase 3 — Free Starter / Freemium Entitlement System — Completion Report

**Status:** Complete. Awaiting phase validation before Phase 4.

Companion documents: `FREE_STARTER_USAGE_RULES.md` (consumption-rule decisions, written before coding), `FREE_STARTER_IMPLEMENTATION.md` (models/services/APIs/security/tests detail).

---

## 1. Summary

Built real, server-authoritative Free Starter consumption on top of Phase 2's foundation, and wired it into the three live endpoints that needed it: QBank answering, and exam start (covering Mock/Daily/Grand/PYQ through the one shared `_start_attempt` function). Provisioning is idempotent and registration-integrated (unchanged from Phase 2, now with configurable expiry). Consumption is atomic and race-tested. Every denial carries a structured, machine-readable reason. Staff/admin accounts are explicitly, redundantly excluded from ever consuming free quota. Two genuine pre-existing gaps that would have made this feature either unreachable or actively broken were found, fixed narrowly, and documented — not silently patched over. No frontend code was touched; nothing in this phase's Definition of Done required it.

**Documentation-naming note carried over from Phase 3's Step 1:** `PLATFORM_MODERNIZATION_PLAN.md` does not exist under that name; `Backend/docs/implementation-plan.md` was read as the equivalent reference.

## 2. Models changed

- `entitlements.FreeStarterPolicy` — added `validity_days` (nullable `PositiveIntegerField`). No other schema change.
- No other model changed. `FreeStarterEntitlement`, `EntitlementEventLog` (Phase 2) are unchanged in shape; their behavior (expiry computation) is affected via the provisioning service, not a schema change.

## 3. APIs changed

No new endpoints. The three Phase 2 endpoints are unchanged in URL/method/auth. Response-shape addition (additive): 402 responses from `/api/questions/{id}/answer/`, `/api/tests/{id}/start/`, and `/api/exam-sessions/{id}/start/` now include a structured `access_denied: {reason, source, upgrade_available}` object when free-starter was consulted. Existing `detail`/`code` fields on those responses are unchanged.

## 4. Services changed

- `entitlements/provisioning.py`: `provision_free_starter()` extended to compute `expires_at` from `validity_days`; two new functions, `has_free_starter_available()` (non-consuming check) and `ensure_and_consume_free_starter()` (lazy-provision + consume).
- `entitlements/services.py`: `_try_free_starter()` now excludes staff/admin.
- `tests_app/views.py`: new `_free_starter_eligibility()` and `_free_starter_denied_payload()` helpers; `_start_attempt()` restructured to a two-phase check-then-consume flow for Grand/Mock/QBank/Daily/PYQ.
- `academics/views.py`: `QuestionViewSet.answer()` gained an inline free-starter gate; its object lookup changed from `self.get_object()` to a direct `get_object_or_404` (see §11 for why).
- `academics/access.py`: `locked_subject_ids()` extended to recognize a currently-valid free-starter `qbank` entitlement as unlocking a subject for listing purposes.

## 5. Frontend changes

**None.** Deliberate scope decision — see `FREE_STARTER_IMPLEMENTATION.md`'s Frontend Integration section for the full reasoning. Django admin's `FreeStarterPolicy` registration was extended with `validity_days` (not a frontend/Next.js change).

## 6. Migrations

One: `entitlements/migrations/0002_freestarterpolicy_validity_days.py` — adds one nullable field, no data migration. Applied only to the ephemeral test database; **no persistent database was modified.**

## 7. Free usage rules

Full detail and reasoning in `FREE_STARTER_USAGE_RULES.md`. Summary: consumption is keyed off the existing per-(user, resource) history models (`QuestionAttempt` for QBank, `TestAttempt` for Mock/Daily/Grand/PYQ) — the first genuinely new use consumes, everything else (browsing, re-answering/resuming something already unlocked) never does. PYQ is one shared quota across institutions, not split per-university, because `Test.university` is free-text data in this codebase, not a fixed enum — baking in specific institution names would be a worse form of hardcoding than the quota numbers themselves. This reasoning is flagged explicitly as a business decision still open (§12), not silently resolved.

## 8. Security changes

Staff/admin exclusion is enforced redundantly in three independent places (not just one), so no single future edit can silently reintroduce staff quota consumption. Every consumption/decision call takes `request.user` only, never a client-supplied identity. Atomicity reuses Phase 2's already-tested `select_for_update()` pattern unmodified. Full detail in `FREE_STARTER_IMPLEMENTATION.md` §Security.

## 9. Tests added

**75 tests in `entitlements/tests.py` total (48 from Phase 2 + 27 new this phase).** New this phase:
- `ValidityDaysTests` (3) — `validity_days` sets `expires_at`; no `validity_days` means no expiry; expired entitlement denies via the decision layer.
- `QbankLiveConsumptionTests` (7) — free subject never gated; first-two-answers-consume-third-denied; re-answering an already-attempted question never double-consumes; already-attempted question stays answerable after quota exhaustion; real subscription bypasses free-starter entirely; staff never gated or provisioned; bookmark-only calls never consume.
- `MockLiveConsumptionTests` (5) — first pro Mock consumes; second pro Mock locked after quota used; resuming the same free Mock (a second attempt of the same test) does not consume again; premium subscription unaffected; no-active-policy denies cleanly.
- `DailyLiveConsumptionTests` (4) — unentitled denied without free quota; entitled via free-starter can start during the session window; viewing session detail never consumes; a quota of 2 allows exactly 2 distinct Daily Tests, not a third.
- `GrandLiveConsumptionTests` (4) — denied when no free policy configured (0 is a valid default); allowed when a promotional policy is configured; password still required after a free grant (this test caught the consumption-timing bug described in §11); real `GrandTestAccess` bypasses free-starter.
- `PyqLiveConsumptionTests` (2) — first pro PYQ test consumes the shared quota; quota is shared across institutions, confirmed via two different `Test.university` values.
- `StaffNeverConsumesFreeStarterTests` (2) — staff denied a pro Mock without a subscription and never gets a `FreeStarterEntitlement` row; `can_view_qbank` denies staff even with an active policy configured.

**Two pre-existing tests updated** (not new failures — deliberate, documented adjustments to scalability-audit query-count assertions, both in `academics/tests.py`): `test_public_list_query_count_does_not_grow_with_row_count` (6→7) and `test_query_count_for_paginated_list_does_not_regress` (≤6→≤7), both because `locked_subject_ids()` now does one additional, flat, indexed `FreeStarterEntitlement` read — bounded and non-scaling, not a regression to the underlying guarantee those tests exist to protect.

## 10. Full test result

```
cd Backend && python manage.py test
```

**657/657 tests pass**, confirmed on two consecutive full runs. (Baseline after Phase 2: 630. This phase: +27 new, 2 existing adjusted with justification, zero unexplained regressions.)

One test, `FreeStarterConcurrentConsumptionTests.test_concurrent_consumption_never_goes_negative`, is confirmed flaky **only** under full-suite contention (passes reliably — 6/6 runs — in isolation); this is the same class of SQLite lock-timing artifact the codebase's own `tests_app.tests.SubmitTestDoubleSubmissionRaceTests` already documents as expected on this local test database (production runs MySQL/InnoDB with real row-level locking). The safety property the test exists to verify (`row.used` never exceeding the configured quota) held in every single run, including the flaky ones — only the test's own thread-outcome bookkeeping was affected, and that bookkeeping was itself hardened this phase (every thread now always records an outcome, even after exhausting its retry budget, rather than letting an exception escape silently).

## 11. Known limitations

- **Two pre-existing architectural gaps had to be closed for Free Starter to function at all**, both documented in full in `FREE_STARTER_IMPLEMENTATION.md`: `QuestionViewSet.answer()` had no commercial-access check whatsoever before this phase, and `locked_subject_ids()` had no awareness of Free Starter, meaning a free-starter-eligible student's question was invisible (404) regardless of remaining quota. Both fixes are narrow and isolated, but they are still real, evidenced behavior changes to existing endpoints, not purely additive work — reviewers should be aware these two files changed for reasons beyond "add Free Starter."
- **A genuine bug was caught and fixed during this phase's own testing**, not found by inspection: consuming Grand Test free-starter quota before checking the exam's optional password meant a wrong-password guess could burn a student's one free grant. Fixed by deferring all consumption to immediately before the attempt is actually created, after every other check has passed.
- **`locked_subject_ids()`'s free-starter check does not lazy-provision** (a deliberate performance choice, since it's a hot, scalability-audited listing path) — a student who has genuinely never been provisioned (registration-time provisioning was skipped, or they registered before any policy existed) will see a paid subject as locked in listing until *something* provisions them first (registration, in the real flow, already does this; `GET /api/entitlements/mine/` also lazy-provisions and would be the natural dashboard-load call in a future UI phase). This is a documented, low-risk edge case, not a bug — flagged for awareness, not treated as blocking.
- **`can_start_test`/`can_view_qbank` (Phase 2's read-only decision functions) and `_start_attempt`/`.answer()`'s real, consuming gates are not the same code path** — they derive from the same underlying access functions (`has_mock_test_access` et al.) but the free-starter-fallback orchestration itself is written twice (once read-only, once consuming). This was an acceptable, explicitly-flagged duplication risk already noted in Phase 2's completion report; it remains open. A later phase should consider whether `_start_attempt` can be refactored to call the decision layer directly rather than re-deriving its own version of the same logic.
- **The mock/daily/pyq course-scoping inconsistency in `billing.access`** (confirmed present, unchanged) remains deliberately unfixed, per the Phase 3 spec's own explicit instruction (Step 33) not to touch it without it being absolutely necessary for Free Starter to function — it is not; Free Starter's fallback applies identically regardless of that pre-existing inconsistency.

## 12. Business decisions still required

- **Past Year Questions quota granularity**: this phase implemented one shared `pyq` quota across all institutions, with the reasoning (`Test.university` being free-text data, not a fixed enum) written out in full in `FREE_STARTER_USAGE_RULES.md` §6. If per-institution quotas are actually required, that needs a data-driven design (not a hardcoded four-way enum) and is a larger change than this phase should make unilaterally.
- **What happens to a student who was never provisioned** (registered before any `FreeStarterPolicy` existed, or before this feature shipped) — should there be a proactive backfill? The Phase 2-built `backfill_free_starter_entitlements` management command is ready but was never run; whether/when to run it is unchanged from Phase 2's own open item.
- **Whether `locked_subject_ids()`'s free-starter check should lazy-provision** (trading a small, bounded performance cost for zero "never provisioned yet" edge cases) is a product/ops tradeoff, not resolved here — flagged in §11.

## 13. Recommended next phase

Per the modernization plan's own sequencing (`docs/implementation-plan.md`), Phase 4 (Student Access Engine — the full `CanView`/`CanPurchase`/`CanRegister`/`CanContinue`/`CanSubmit`/`CanReview`/`CanViewSolutions`/`CanViewRank`/`CanViewAnalytics` capability set) is the natural next step, since it builds directly on this phase's now-live consumption mechanics. Awaiting explicit validation of Phase 3 before starting it, per the phase-gate rule.
