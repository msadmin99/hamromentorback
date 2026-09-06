# Dr. Gutka Platform Modernization — Implementation Plan

**Status:** Phases 0–8 complete (see each phase's own completion report in this directory). Phases 9–11 planned below, not yet started — each requires explicit go-ahead before work begins, per the modernization spec's phase-gate rule.

This plan is the "exact implementation plan" requested by the modernization spec, Step 5. It does not contain code — only scope, sequencing, and the specific architectural decisions each phase must make. Source evidence for every item is `MASTER_PLATFORM_AUDIT_REPORT.md` and `EXAM_SYSTEM_AUDIT_REPORT.md`, re-verified against current code per `phase-0-baseline.md`.

---

## Phase-numbering normalization (performed at the start of Phase 9)

Phases 6, 7 and 8 each had to be renumbered at kickoff because this
document's numbering had drifted out of sync with the actual execution
order. The root cause was structural, not incidental: **"Exam Builder"**
sat at Phase 6, but every kickoff prompt from Phase 6 onward described
the next *backend-integrity* phase instead. Each time, Exam Builder was
pushed down exactly one slot (6 → 7 → 8 → 9) — which fixed that phase's
number but regenerated the identical off-by-one for the next one. Four
consecutive conflicts.

Normalized once, permanently, here: Exam Builder was **removed from the
numbered sequence entirely** (see "DEFERRED / OUT-OF-SEQUENCE — Exam
Builder" near the end of this document) and the remaining substantive
phases were renumbered contiguously so that **phase number = actual
execution order**. Completed Phases 1–8 are untouched — their numbers,
scope, and completion reports all remain exactly as executed.

| Phase number before normalization | Feature | Phase number after |
|---|---|---|
| 1–8 | Security → … → Historical Integrity (all complete) | **1–8, unchanged** |
| 9 | Exam Builder | **Deferred / out-of-sequence** (no number) |
| 10 | Commercial Integrity | **9** |
| 11 | Student UX | **10** |
| 12 | Analytics / Performance / Accessibility | **11** |

Nothing about any phase's *scope* was changed to make the numbering fit —
only positions moved. Cross-references elsewhere in this document that
predate the normalization are annotated inline where they would otherwise
read as stale. Full account: `PHASE_9_COMPLETION_REPORT.md` §1–2.

---

## Phase 1 — P0 Security ✅ COMPLETE

See `phase-1-completion-report.md` for full detail, evidence, and test results. Summary: fixed the unauthenticated exam-session-participant PII leak, the billing authorization gap, `TestViewSet`'s missing role-level enforcement on delete/reschedule (wired to the platform's own pre-existing `EXAM_MANAGEMENT_FEATURES` design), the `MediaAssetDetailView` ownership gap, and the `RolePermission` UI/enforcement gap (both the missing-checkboxes UI issue and the previously-unenforced `EDITOR_ALLOWED_FEATURES` ceiling). 582/582 tests pass (23 new).

**Deliberately not touched in Phase 1** (belongs to later phases per the spec's own phase list): entitlement/expiry enforcement, `TestAttempt` timeout, refund system, coupon race condition, `CRON_SECRET` hardening, `Question` option-edit history loss. These are data-integrity/missing-feature items, not authorization defects — Phase 1 was scoped strictly to authorization.

---

## Phase 2 — Entitlement Foundation

**Goal:** one central access-decision architecture, without redesigning the student UI yet.

**Key design decision to make before coding:** the audit found five independent `billing.access` functions (`has_qbank_access`, `has_video_access`, `has_mock_test_access`, `has_daily_test_access`, `has_pyq_access`) plus `courses.access.eligible_course_ids`/`eligible_batch_ids` plus `tests_app.access.can_access_test`. The modernization spec's "Absolute Rule #4: do not create multiple entitlement engines" and its instruction to separate **catalog visibility** from **entitlement** from **access** means the target shape is:

- A single `entitlements` module (new, or consolidated into `courses.access`/`billing.access` — decide during design, not blind-guessed here) exposing one function per concept, not per product:
  - `academic_eligibility(user, course)` → wraps the existing `eligible_course_ids`/`eligible_batch_ids`/individual-assignment logic, **fixed to respect `expires_at`** (the audit's top data-integrity finding — currently ignored entirely).
  - `commercial_entitlement(user, product_type, course)` → replaces the five hand-written `has_*_access` functions with one parameterized implementation, **fixing the confirmed course-scoping inconsistency** (mock/daily/pyq currently ignore course; qbank/video correctly scope by course) as part of this consolidation, not as an afterthought.
  - `free_starter_entitlement(user, resource_type)` → new, see Phase 3.
- Preserve every existing model (`Enrollment`, `Subscription`, `ComboPlan`, `Scholarship`, `GrandTestAccess`) — this is a **query/decision consolidation**, not a schema rewrite. "Do not create multiple entitlement engines" is satisfied by having one *decision layer*, not by collapsing the underlying commercial-source models (which legitimately differ: a subscription and a scholarship are different real-world things and should stay queryable as such for support/audit purposes).
- Fix, as part of this phase (data-integrity, not just architecture):
  - `Enrollment`/`Subscription` expiry: add a real deactivation mechanism (background job — see Phase 2's background-job companion work) so `is_active` stops lying once `expires_at` passes, instead of only being correctly filtered at read time.
  - Scholarship revocation: decide (business decision required, flagged in the audit) whether revoking a scholarship should also deactivate the `Enrollment` it created, then implement whichever is decided.
  - Distinguish scholarship-origin vs. paid-origin on a shared `Subscription` row (the audit's "shared-row merge" risk) so a scholarship revocation can never retroactively kill paid access.

**Explicitly not in this phase:** the `CanView`/`CanStart`/etc. capability model (that's Phase 4, built *on top of* this foundation) and any student-facing UI change.

**Tests required:** full regression on every existing consumer of `eligible_course_ids`/`can_access_test`/the five `has_*_access` functions (tests_app, academics, videos_app, billing) — this is the single highest-regression-risk phase in the whole plan, since it changes behavior every other domain depends on.

---

## Phase 3 — Freemium Starter Access

**Goal:** configurable Free Starter Policy, auto-granted at registration, server-enforced quota.

- New model, e.g. `FreeStarterPolicy` (admin-configurable singleton or per-resource-type row set — decide shape during design: the spec's example fields are `resource`, `resource_type`, `quantity`, `unlimited`) — **admin-editable via a new Settings screen**, values never hardcoded in application code (per Absolute Rule #10).
- New model, `FreeStarterEntitlement` (or integrated as a `commercial_entitlement` source per Phase 2's decision layer) — one row per (user, resource_type), tracking `quantity`, `used`, `remaining`, `valid_from`, `expires_at`, `source='free_starter'`, `status`.
- Created automatically on registration (after the "account verification" step the spec names — verify current registration flow has no email verification today, per the audit; decide whether to add one now or treat "verification" as "registration complete" for v1 — **business decision**, document the choice).
- Server-authoritative consumption: every free-action-consuming endpoint (QBank answer, Mock/Daily/PYQ start) must atomically decrement remaining quota — `select_for_update()` or an equivalent DB-level guard, matching the pattern already used correctly elsewhere in this codebase (`academics/services.py`'s `_apply_question_stat_delta`, `billing`'s payment-reference locking) so a race condition can't double-consume or go negative.
- Anti-abuse minimum (per spec): one starter entitlement per account (enforced by the (user, resource_type) uniqueness above), server-side records only (no client-trusted counter), redemption history via the entitlement row's own `used` field, rate-limiting reuses the existing DRF throttle pattern already in use in `billing` (`SubmitPaymentThrottle`) and `academics` import (`ImportUploadThrottle`).

**Tests required:** concurrency test for quota consumption (two simultaneous requests must not both succeed past the limit) — matches the audit's own recommendation for the coupon-race fix in the Commercial Integrity phase (Phase 9 post-normalization; this line originally said "Phase 10"), applied here proactively.

---

## Phase 4 — Student Access Engine

**Goal:** implement `CanView`/`CanPurchase`/`CanRegister`/`CanStart`/`CanContinue`/`CanSubmit`/`CanReview`/`CanViewSolutions`/`CanViewRank`/`CanViewAnalytics` as one reusable server-side decision set, built on Phase 2's entitlement layer + Phase 3's free-starter layer.

- Each capability is a function of (user, resource) returning a decision **plus a reason** (the spec's "access explanation" requirement — "Allowed / Source: CEE-MBBS Premium Subscription / Expires: 15 Nov 2026" or "Denied / Reason: Free QBank allocation exhausted").
- `CanView` must never be `NO` merely because the student lacks commercial entitlement (per the spec's "no hard paywall before value discovery" principle) — it reflects only draft/publish status and academic (course/batch) scoping, never commercial state.
- This is where `solutions_visibility` (confirmed by the exam audit to be stored but never enforced anywhere) finally gets real enforcement — `CanViewSolutions` implements `immediate`/`after_exam_window`/`manual_release`/`never`, with `manual_release` requiring the audit trail fields the spec names (`released_by`, `release timestamp`, confirmation step).

**Tests required:** one 200/one 403 test per capability per representative access-source (free starter, subscription, combo, scholarship, course enrollment, batch assignment, individual assignment, admin override) — this is the bulk of the new test surface for this phase.

---

## Phase 5 — Exam Type Policies

**Goal:** default policy templates for Practice/Mock/Daily/Grand/Past Year, admin-overridable.

- A `ExamTypePolicy` (or similar) default-value set per `exam_type`, consumed by both `CreateExamWizardShell`/`TestConfigStep` (already flagged in the exam audit as two independently-hand-coded default objects that already drifted once — **this phase is where that unification finally happens**, per the exam audit's own Phase-1-of-that-plan recommendation) and the Import-and-create-exam flow.
- Encodes exactly the five policy tables the spec lays out (Practice/Mock/Daily/Grand/PYQ) as data, not scattered conditionals.

**Tests required:** regression on the existing `TestAdminSerializer`/`TestConfigStep` default-value tests plus new tests confirming each exam_type's template is actually applied and is admin-overridable.

---

## Phase 6 — Daily / Grand Delivery

> **Renumbered from Phase 7** at the start of this phase's execution — the
> Phase 6 kickoff prompt's content (session lifecycle, `MIN(attempt_start +
> duration, session_end)`, auto-submit, Daily/Grand delivery semantics)
> matched this section verbatim, not the original "Exam Builder" Phase 6
> below. Per the kickoff prompt's own conflict-resolution procedure
> (identify → report → smallest-safe interpretation → document), this
> section was promoted to Phase 6 and "Exam Builder" was renumbered to
> Phase 7. See `PHASE_6_COMPLETION_REPORT.md` §1 for the full account.

**Goal:** session lifecycle, global vs. personal time, late entry, auto-submit, missed/re-release, registration.

**This phase fixes the audit's single highest-priority stale-state finding:** `TestAttempt.status='in_progress'` currently has zero server-side timeout — implement the `MIN(attempt_start + duration, session_end)` rule the spec specifies, with server-authoritative enforcement (not client timer trust) and an actual auto-submit mechanism (background job or lazy-on-access enforcement — decide during design; the audit found `ExamSession.refresh_status()`'s existing lazy-reactive pattern as a precedent already in this codebase, worth considering for consistency, but a genuinely abandoned attempt needs a job that runs *without* anyone hitting the API, unlike `refresh_status()`).

- Add the `not_started`/`in_progress`/`submitted`/`auto_submitted`/`expired`/`abandoned`/`cancelled` state set the spec requires (current codebase only has a subset).
- "Missed" Daily Test handling: a session past its window with no attempt cannot be started; a re-release creates a **new** session/opportunity, never mutates the historical one (matches the exam audit's confirmed-correct precedent: `clone_test_as_new_version` already does exactly this for exam content — apply the same never-mutate-history principle here).
- Grand Test: fixed start/end, optional registration/late entry, ranking/percentile (already correctly implemented per the exam audit — `SubmitTestView`'s ranking logic is sound, reuse as-is), password as an **additional** layer never a substitute for entitlement (already correctly separated in current code — `_start_attempt`'s sequential check order already puts academic+commercial entitlement before password; preserve this ordering).

---

## Phase 7 — Results / Solutions

> **Renumbered from Phase 8** at the start of this phase's execution — the
> Phase 7 kickoff prompt's content (result/solution/ranking/analytics
> access, `CanViewSolutions`, manual-release, server-side solution
> protection) matched this section verbatim, not "Exam Builder" below
> (which was Phase 7 before this swap). Same conflict-resolution procedure
> as Phase 6's own renumbering. See `PHASE_7_COMPLETION_REPORT.md` §1.

**Goal:** make `solutions_visibility` actually work (built on Phase 4's `CanViewSolutions`), with a real manual-release action.

- New endpoint: release solutions for a session/exam — `released_by`, `release timestamp`, confirmation step, audit entry (feeds Phase-general audit log, see cross-cutting note below).
- Protect answer/explanation content server-side (not just hide it in the frontend) until release — the serializer that currently always includes `explanation`/correct-option data must gate on the new `CanViewSolutions` capability.

---

## Phase 8 — Historical Integrity

> **Renumbered from Phase 9** at the start of this phase's execution — the
> Phase 8 kickoff prompt's content (Question Versioning, attempt/delivery
> snapshots, historical correctness) matched this section verbatim, not
> "Exam Builder" below (which was Phase 8 before this swap). Same
> conflict-resolution procedure as Phases 6 and 7's own renumbering — now
> an established pattern across three consecutive phases. See
> `PHASE_8_COMPLETION_REPORT.md` §1.

**Goal:** Question Versioning + attempt/delivery snapshots — **only after a design document and review**, per the spec's explicit instruction.

- **Before any migration:** write `docs/QUESTION_VERSIONING_DESIGN.md` covering exactly what the exam audit already flagged as the two concrete, evidenced problems this must solve:
  1. Editing a `Question`'s text/options/correct-answer after use currently changes what a graded past attempt's result page displays, while the frozen score stays computed from the old content (a live, unresolved data-integrity gap).
  2. Editing a `Question`'s options via `QuestionAdminSerializer.update()` currently hard-deletes and recreates every `Option` row, `SET_NULL`-ing `selected_option` on every historical `Answer`/`QuestionAttempt` — destroying "what did the student actually pick" on an ordinary content edit.
- The design doc must decide: snapshot-on-attempt (copy content into `Answer`/`TestQuestion` at submission time) vs. true question versioning (immutable `Question` versions, `TestQuestion` points at a specific version) vs. a hybrid. This is explicitly **not** decided by this plan — it requires its own review per the spec.
- Migration must handle existing historical `Answer`/`TestAttempt` rows that have no snapshot to backfill from — decide and document the graceful-degradation behavior (fall back to live-read for pre-migration rows) before writing the migration.

---

## Phase 9 — Commercial Integrity

**Goal:** Subscription/Combo/Payment/Coupon/Refund/Scholarship fixes, with historical entitlements protected.

- **Refund system** (currently does not exist at all, confirmed by the audit): new `Purchase.status` value or a related `Refund` model, an explicit refund→entitlement policy (spec: "must not revoke unrelated entitlements," "should update/revoke future entitlement according to policy" — **business decision required** on exactly what a refund revokes).
- **Coupon race condition fix**: row-lock (`select_for_update()`) the `Coupon` at both validation and redemption time, and re-check `max_uses`/`max_uses_per_user` at `activate()` time, not only at `Purchase` creation time (the audit's confirmed gap).
- **Historical purchase/combo protection**: already correctly snapshotted today (`Purchase.original_amount`/`final_amount`, `PurchaseComboItem.price` — confirmed sound by the audit, preserve as-is, no change needed here).
- **`CRON_SECRET` hardening**: deferred from Phase 1, addressed here alongside the rest of the commercial-integrity hardening pass — remove the insecure default outside `DEBUG`, confirm production Secret Manager wiring (already confirmed present in `cloudbuild.yaml` per Phase 0 baseline) stays authoritative.

---

## Phase 10 — Student UX

**Goal:** catalog, free/locked cards, upgrade prompts, plan comparison, dashboard, access explanations — built entirely on Phases 2–4's server-side decisions, no new access logic invented in the frontend.

- Every card's `VISIBLE`/`ENTITLED`/`STARTABLE`/`PURCHASABLE` state comes from the Phase 4 capability functions via the API — the frontend renders, it does not decide (per Absolute Rule #5: do not trust frontend access checks — this applies equally to not *re-implementing* access logic client-side for display purposes).
- Plan comparison page reads actual configured plan entitlements (Phase 2/9 data — "2/10" before the normalization above), never hardcoded plan names/features (per spec's explicit instruction).
- Admin "preview as student" simulation: read-only, must not create real `TestAttempt`/`Purchase`/etc. rows — implement as a request-scoped override of the Phase 4 capability functions' input user context, not a real impersonation session.

**Delivered.** See `PHASE_10_ARCHITECTURE.md` and
`PHASE_10_COMPLETION_REPORT.md`. All three bullets above were
implemented as written; no bullet was reinterpreted or dropped. Exam
Builder remained out of scope per the normalization above.

---

## Phase 11 — Analytics / Performance / Accessibility

- Extend Analytics (confirmed fully live-computed today, no caching layer — acceptable to keep as-is per the audit) with the new conversion/usage metrics the spec lists (free registrations, free usage, free-to-paid conversion, upgrade clicks, quota exhaustion, etc.), sourced from Phase 3's `FreeStarterEntitlement` and Phase 2's entitlement layer.
- Add the composite `Question` indexes the audit identified as missing (`subject`+`chapter`+`topic`, `course`+`subject`) plus any new indexes Phase 2/3/4's new query patterns require.
- Accessibility pass against the spec's WCAG 2.2 checklist — out of this plan's architectural scope beyond noting it belongs here, not earlier.

**Accessibility parked item: targeted remediation delivered** (scheduled
explicitly after Phase 11 — **not** a new numbered phase; the sequence
still ends at Phase 11). Scope was deliberately **targeted remediation,
not a WCAG 2.2 conformance program**, and no conformance is claimed: no
screen reader was available to test with. Delivered: `<main>` landmark and
skip link, global `:focus-visible` and `prefers-reduced-motion`, dialog
Escape/focus-trap/focus-return via one shared hook, login label and error
association, and the fix for the exam timer announcing every second. 8
jsx-a11y lint rules enforced with zero new dependencies. See
`ACCESSIBILITY_AUDIT.md` — §10 lists what was deliberately left
unresolved.

**Follow-up parked item: MCQ / exam-control remediation — delivered.**
The primary exam answer control (`aria-pressed` buttons) was converted to
native `<fieldset>` + radio inputs, chosen because the data model
(`tests_app.Answer.selected_option`, a single nullable FK) confirms every
question is single-answer — no checkbox-group variant was needed. QBank's
one-shot commit-on-click buttons were deliberately left as buttons, with a
regression test guarding against a future blanket conversion. See
`ACCESSIBILITY_AUDIT.md` §9a.

**Delivered (bullets 1–2).** See `PHASE_11_ANALYTICS_ARCHITECTURE.md` and
`PHASE_11_ANALYTICS_COMPLETION_REPORT.md`. Bullet 1 shipped as
`entitlements/analytics.py`, composed into the existing admin dashboard;
"upgrade clicks" is reported as explicitly unavailable because no
interaction telemetry exists on this platform — it was not proxied.
Bullet 2 shipped the `(subject, chapter, topic)` composite index; the
`course`+`subject` composite named in this bullet is **not expressible**
as an index (`Question.courses` is M2M, so the two columns live in
different tables) — documented in the architecture doc, §9. Bullet 3
(accessibility) remains scoped out by this bullet's own wording; Phase
11's own new UI meets the bar but no platform-wide WCAG audit was done.

Two corrections to this section's own text, recorded rather than silently
edited: the analytics layer is no longer strictly "no caching layer" (a
later scalability pass added a short-TTL, per-user cache on
`/api/performance/overview/`), and the student analytics layer described
as needing extension already existed in full (`tests_app/performance.py`).

**Further parked item: Unified Exam Catalog Visibility / Free Access
Correction — delivered.** A non-paid student saw 0 Daily Tests while a
paid student saw the real catalog. Root cause (confirmed by an
exhaustive audit — no commercial filter exists anywhere in the catalog
pipeline for any exam type): `courses.access.eligible_course_ids()`, the
one gate behind Test/Question/Video catalog visibility platform-wide,
reads only `courses.Enrollment`, and registration never created one — so
a self-registered free student stayed catalog-blind until an admin
happened to enroll them, which in practice mostly only occurred as a
side effect of paying. Fixed at the root: `RegisterSerializer` now
creates a free `Enrollment` (mirroring `_ensure_enrollment`'s existing
fix for the identical bug on the paid side), plus a data-only migration
backfilling it for students who registered before the fix. One
additional "paid user sees a false lock" finding was also fixed (Grand
Test's single-purchase catalog). See
`UNIFIED_CATALOG_ACCESS_REMEDIATION.md`.

---

## DEFERRED / OUT-OF-SEQUENCE — Exam Builder

> **Removed from the numbered execution sequence during Phase 9's
> numbering normalization** (see "Phase-numbering normalization" at the
> top of this document). This item was the sole cause of four consecutive
> phase-number conflicts: it originally sat at Phase 6, and each kickoff
> prompt from Phase 6 onward described the *next backend-integrity* phase
> instead, so Exam Builder was pushed down one slot every time
> (6 → 7 → 8 → 9) and never executed, regenerating the same off-by-one on
> the next kickoff. It is parked here, outside the numbered sequence,
> until it is explicitly scheduled — which is a product decision, not an
> automatic next-in-line one. **Scope is unchanged; only its position is.**

**Goal:** one reusable Exam Builder (General/Questions/Sections/Access/Rules/Schedule/Results/Monetization/Security/Advanced) used by Create, Edit, and Import-and-Create — **without merging their entry workflows**, per the spec's explicit instruction.

- Reuses `TestAdminSerializer` as the single backend contract (already true today, confirmed by the exam audit — no change needed there).
- Frontend: extract the shared default-config object (Phase 5 already did the data side; this phase does the component side) so `CreateExamWizardShell.js` and `TestConfigStep.js` render the same section components against the same config shape.
- Import-and-Create keeps its distinct upload→preview→configure flow (per spec: "Import and Create Exam may remain its own workflow"), but the configuration step at the end must have access to every section the Builder offers — closing the audit-confirmed gap where the import flow has no batch/individual-student assignment UI at all.
- **Partially delivered already**: Phase 5 unified the *data* side (`ExamTypePolicy` — both flows now read one canonical default-config source). What remains is the *component* side (shared section components) plus the import flow's missing batch/individual-student assignment UI.

---

## Cross-cutting: Audit Log

Not a standalone phase — implemented incrementally as each phase adds an action worth auditing (the codebase already has `DeletionAuditLog`/`PaymentAuditLog`/`AdminEditAuditLog` precedents to extend, per the audit's confirmed-sound existing patterns). Solution release (delivered in Phase 7 — it reuses `AdminEditAuditLog`), refund/revoke (Phase 9, post-normalization — this section originally said "Phase 10"), and Phase 1's `RolePermission` changes are the concrete additions.

**`RolePermission` auditing: delivered** (scheduled explicitly as a parked
item after Phase 11, not as a new numbered phase — the numbered sequence
still ends at Phase 11). Reuses `AdminEditAuditLog` /
`record_admin_edit()`; no new audit model and no migration. Covers both
request-driven mutation paths — the DRF viewset (POST/PUT/PATCH; DELETE
is not offered) and the Django admin site (add/change/delete/bulk
delete). See `ROLE_PERMISSION_AUDIT.md`.

## Cross-cutting: Feature Flags / Rollout

Per the spec's deployment guidance, any phase touching live access/billing behavior (2, 3, 4, 7, and 9 — that last was "10" before the normalization above) should ship behind a feature flag with a dark-launch → small-rollout → verify → full-rollout sequence once deployment for that phase is authorized — this plan covers code/test scope only; actual rollout sequencing is decided per-phase at approval time, not pre-committed here.

## Phase Gate Rule (per modernization spec)

After every phase: run unit/integration/security/migration/regression tests, test APIs, test student experience, verify existing paid customers, verify existing exam attempts/results. Do not begin the next phase if P0/P1 tests fail. **No phase after Phase 1 begins without explicit user go-ahead.**
