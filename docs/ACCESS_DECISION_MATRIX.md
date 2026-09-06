# Access Decision Matrix — Phase 4

Written before implementation, per Phase 4's mandatory audit-first sequence. Documents exactly how access is decided **today** (post-Phase-3), source by source and capability by capability, re-verified against live code this session — not assumed from earlier phase reports.

---

## Access sources — current mechanism, re-verified

| Source | Model | Where read | Expiry | Revocation |
|---|---|---|---|---|
| Free Starter | `entitlements.FreeStarterEntitlement` | `entitlements.services`/`provisioning` | `expires_at` (from `validity_days`), correctly checked via `effective_status` | `status='revoked'` field exists; no UI/endpoint sets it yet (unchanged since Phase 2/3 — not a Phase 4 concern) |
| Subscription | `billing.Subscription` | `billing.access._active_subscriptions` and friends | `expires_at`, correctly checked (`Q(expires_at__isnull=True)\|Q(expires_at__gte=now)`) | `is_active=False`; nothing flips this on natural expiry (confirmed unchanged) |
| Combo | *(no dedicated model)* | resolves into N `Subscription` rows at purchase time | inherited from the resulting `Subscription` rows | inherited |
| Direct Purchase (Grand Test) | `billing.GrandTestAccess` | `billing.access.get_grand_test_access` | none (one-time grant, no `expires_at` field — by design) | none (no revoke path found) |
| Scholarship | `billing.Scholarship` | never read directly by any access check (confirmed again this phase) — access flows entirely through its linked `Subscription` | `Scholarship.expires_at` unread; the linked `Subscription.expires_at` is what's enforced | `ScholarshipViewSet.revoke()` deactivates the `Scholarship` and its linked `Subscription`; does not touch `Enrollment` (unchanged, still an open item from Phase 2) |
| Course Enrollment | `courses.Enrollment` | `courses.access.eligible_course_ids`/`eligible_batch_ids` | `expires_at`, correctly checked since the Phase 2 fix (re-verified unchanged) | plain admin `is_active=False` |
| Batch Assignment | `tests_app.Test.assigned_batches` (M2M) | `tests_app.access.can_access_test`/`visible_test_queryset` | none (plain membership) | unassign |
| Individual Assignment | `tests_app.Test.assigned_students` (M2M) | same | none | unassign |
| Staff/admin override | `User.is_staff`/`created_by` | `can_access_test` (bypasses everything); explicitly **excluded** from Free Starter as of Phase 3 | n/a | n/a |

## Capability semantics — defined this phase

| Capability | Meaning | Independent of commercial entitlement? |
|---|---|---|
| `CanView` | The resource is visible/browsable (catalog, detail) | Yes — academic eligibility (or public) only |
| `CanPurchase` | The student could initiate a purchase/upgrade for this resource | Yes — a signal, not an entitlement check |
| `CanRegister` | The visitor could create an account | n/a — anonymous-only |
| `CanStart` | A brand-new attempt/consumption may begin | No — requires real entitlement (any source) |
| `CanContinue` | An already-`in_progress` attempt may be resumed | No — requires the attempt to exist and belong to the user; entitlement was already checked when it started |
| `CanSubmit` | An `in_progress` attempt may be finalized | No — ownership + `in_progress` status |
| `CanReview` | A `submitted` attempt's own review screen may be opened | No — ownership + `submitted` status |
| `CanViewSolutions` | Explanations/correct answers for a submitted attempt may be shown | No — ownership + `submitted` status (see discrepancy below) |
| `CanViewRank`/`CanViewAnalytics` | Rank/percentile/performance data for the student's own record | No — ownership only, always self-scoped |

## Access Decision Matrix — condition per (source × capability)

`✓` = source alone is sufficient when relevant; `—` = source doesn't apply to this capability; `n/a` = capability doesn't depend on commercial source at all.

| | CanView | CanPurchase | CanStart | CanContinue | CanSubmit | CanReview | CanViewSolutions | CanViewRank | CanViewAnalytics |
|---|---|---|---|---|---|---|---|---|---|
| Free Starter (remaining) | n/a | — | ✓ | n/a | n/a | n/a | n/a | n/a | n/a |
| Subscription (active) | n/a | — | ✓ | n/a | n/a | n/a | n/a | n/a | n/a |
| Combo | n/a | — | ✓ (via resulting Subscription) | n/a | n/a | n/a | n/a | n/a | n/a |
| Direct Purchase (Grand) | n/a | — | ✓ | n/a | n/a | n/a | n/a | n/a | n/a |
| Scholarship | n/a | — | ✓ (via linked Subscription) | n/a | n/a | n/a | n/a | n/a | n/a |
| Course Enrollment | ✓ | n/a | prerequisite (academic gate, all rows below still need this too) | n/a | n/a | n/a | n/a | n/a | n/a |
| Batch/Individual Assignment | ✓ (bypasses course) | n/a | prerequisite (alternative to Enrollment) | n/a | n/a | n/a | n/a | n/a | n/a |
| Staff/admin | ✓ (bypasses all) | n/a | still needs real commercial entitlement (Phase 1/3 finding, unchanged) | n/a | n/a | n/a | n/a | n/a | n/a |
| *(attempt/session state, not a commercial source)* | | | needs `CanView` + entitlement + session window open + attempt-limit not reached | needs an existing `in_progress` attempt belonging to the user | needs an existing `in_progress` attempt belonging to the user | needs an existing `submitted` attempt belonging to the user | needs an existing `submitted` attempt belonging to the user | needs an existing `submitted` (or any) attempt belonging to the user | always self-scoped, no attempt needed |

**Multiple-entitlement composition (re-verified unchanged from Phase 2/3):** `CanStart`'s underlying commercial check is "does ANY currently-valid source exist" — never a single boolean flag. An exhausted Free Starter row and an active Subscription for the same resource coexist as two independent, simultaneously-queryable facts; the Phase 3 fix specifically ensures a Scholarship-originated `Subscription` and a paid one are never the same row, so revoking one can never affect the other. This phase adds no new composition logic — it exposes the existing, already-correct composition through named capability functions.

## Discrepancies found (reported, not silently resolved)

1. **`CanViewSolutions`/`CanReview` — real gap found and fixed this phase.** `AttemptDetailView.get` (the primary attempt-detail endpoint) correctly gates solution/result content behind `attempt.status == 'submitted'`. `TestResultView.get` (a second, separate endpoint returning the identical `TestResultSerializer`) had **no status check at all** — any authenticated student could call `GET /api/attempts/{own_attempt_id}/result/` on their own **still-in-progress** attempt and receive full solutions/correct-answer content before submitting. Confirmed via direct code re-read; confirmed zero existing test coverage of this endpoint (so no prior test asserted the permissive behavior was intentional). This is exactly the "scattered access rules across similar endpoints" problem Phase 4 exists to catch — two endpoints serving the same data with two different, disagreeing gates. **Fixed this phase** — see the implementation doc.
2. **`solutions_visibility` (the `auto`/`manual` release-policy field on `Test`) remains unenforced.** Confirmed unchanged from every prior audit this session — `CanViewSolutions` as implemented this phase reproduces the *current, actual* behavior (submitted + owner), not the field's documented intent. Implementing the full release-workflow (manual release action, `released_by`, timestamp, audit entry) is explicitly out of scope for Phase 4 per the modernization plan's own sequencing (Phase 8) and the "no blind rewrite" instruction — flagged here as a known, deliberate limitation, not silently fixed or silently ignored.
3. **`can_access_test` (object-level "can this student reach this Test") and `visible_test_queryset` (listing-level "which Tests show up for this student") are two independently-written functions with similar but not code-shared logic.** Both were confirmed unchanged and still logically consistent with each other (no test in Phases 1-3 found a disagreement), but this is itself an instance of the "scattered rules" pattern Phase 4's primary goal names. **Not unified this phase** — `can_access_test` is reused as-is for the new `can_view_test` capability (see implementation doc); attempting to also collapse `visible_test_queryset` into the same code path is a larger, separate refactor with its own regression surface (every list endpoint that filters by it), and is not required for Phase 4's engine to function correctly. Flagged for a future phase.
4. **The confirmed mock/daily/pyq course-scoping inconsistency in `billing.access`** (unchanged, re-confirmed present) is **not touched** this phase — the access engine's `CanStart` composition works correctly regardless of this pre-existing inconsistency; fixing it is not required for Phase 4 to function safely, matching the explicit "do not fix it merely because you see it" instruction.

## Performance note carried into the design

`locked_subject_ids()`'s existing flat-query discipline (Phase 3) and `_active_subscriptions`/`eligible_course_ids`'s existing single-query shape are reused unmodified by every new capability function in this phase — no new N+1 pattern is introduced (verified in the implementation/testing pass, see completion report §Performance).
