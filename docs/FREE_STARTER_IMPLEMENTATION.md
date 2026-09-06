# Free Starter Implementation — Phase 3

Companion document: `FREE_STARTER_USAGE_RULES.md` (the consumption-rule decisions this implementation follows). This document covers the concrete models/services/APIs/migrations/security/tests.

---

## Models

`entitlements` app, extended from Phase 2:

- **`FreeStarterPolicy`** (existing model, one new field this phase): `validity_days` (nullable `PositiveIntegerField`) — days after provisioning an entitlement stays valid; blank = no self-expiry (only exhausts by usage). Never hardcoded; admin-editable. No policy rows are seeded — the system grants nothing until an admin activates at least one.
- **`FreeStarterEntitlement`** (unchanged schema from Phase 2) — `provision_free_starter()` now also computes `expires_at` from the policy's `validity_days` at provisioning time.
- **`EntitlementEventLog`** (unchanged from Phase 2) — every provisioning and consumption event this phase logs continues to use this existing model.

No new tables beyond Phase 2's three. No parallel free-tier model was created.

## Services

`entitlements/provisioning.py`:
- `provision_free_starter(user)` (Phase 2, extended) — now sets `expires_at` per policy.
- `consume_free_starter(user, resource_type, amount=1)` (Phase 2, unchanged) — atomic, `select_for_update()`.
- `has_free_starter_available(user, resource_type)` (**new**) — non-consuming eligibility check with lazy-provisioning. Exists specifically so a live consumption call site can confirm eligibility *before* committing to consumption, deferring the actual decrement until every other gate (password, attempt limits) has also passed.
- `ensure_and_consume_free_starter(user, resource_type, amount=1)` (**new**) — lazy-provision + consume in one call, used only once the caller is certain the request will succeed.

`entitlements/services.py`:
- `_try_free_starter` (Phase 2) now excludes staff/admin accounts explicitly (Step 31), returning a denial rather than ever suggesting free-starter as a viable path for them — keeps the read-only decision preview consistent with the real, now-staff-excluding enforcement in `_start_attempt`.

`tests_app/views.py`:
- `_free_starter_eligibility(user, has_prior_attempt, resource_type)` (**new**) — the two-phase check-then-consume orchestration for `_start_attempt`. Returns `(eligible, should_consume)`: a student with any prior attempt of this specific test is always re-admitted without a second consumption (their only way to have a prior attempt while lacking real commercial access is a previous free-starter grant, since real access is checked earlier in the same function); a genuinely first attempt is gated on, and — once every other check in the function has also passed — consumes, real remaining quota. Staff/admin are never eligible.
- `_free_starter_denied_payload()` (**new**) — the structured `access_denied` shape (Step 19).

`academics/views.py`:
- `QuestionViewSet.answer()` gained an inline gate (not a separate function, to stay close to the exact point it applies): first-ever answer to a non-free-subject question, by a non-staff user, is checked against `entitlements.services.can_view_qbank` and consumes via `ensure_and_consume_free_starter` only when that decision's source is free-starter.

`academics/access.py`:
- `locked_subject_ids(user)` — extended with one additional, flat, indexed check: a subject is not treated as locked if the user has a currently-valid free-starter `qbank` entitlement (any quota remaining, or unlimited). Deliberately a plain read, no lazy-provisioning (this is a hot, scalability-audited path — see the "necessary pre-existing-gap fixes" section below for why this was required at all).

## APIs

No new endpoints this phase. The existing Phase 2 endpoints (`/api/entitlements/mine/`, `/api/entitlements/tests/<id>/access/`, `/api/entitlements/subjects/<id>/qbank-access/`) are unchanged in shape; `/mine/`'s response now reflects `expires_at` correctly for policies with `validity_days` set.

**Response shape addition (additive, not breaking):** every 402 response from `.answer()` and `_start_attempt` that free-starter was consulted for now carries an `access_denied` object:
```json
{"detail": "...", "code": "purchase_required", "access_denied": {"reason": "free_limit_reached", "source": "free_starter", "upgrade_available": true}}
```
Existing `detail`/`code` fields are unchanged — no existing frontend consumer of these endpoints breaks.

## Necessary pre-existing-gap fixes (Step 33: documented, isolated, tested)

Two gaps were found during implementation that made Free Starter's own enforcement either meaningless or actively broken. Both are scoped as narrowly as possible and covered by dedicated tests.

1. **`QuestionViewSet.answer()` had no server-side commercial-access check at all before this phase.** Confirmed by direct re-read, not assumed. Free Starter enforcement requires *some* gate to exist on the actual consumption endpoint. Fixed by adding exactly the check described above, scoped to first-ever-attempt-at-a-non-free-subject-question only.
2. **`locked_subject_ids()` had no awareness of Free Starter**, meaning a free-starter-eligible student's paid-subject question was invisible to `get_object()` (a 404) regardless of remaining quota — the feature could never actually be reached. Fixed by adding the flat, indexed check described above. A related, narrower fix was also needed: `QuestionViewSet.answer()`'s object lookup was changed from `self.get_object()` (which applies the *listing*-scoped `locked_subject_ids` exclusion) to a direct `get_object_or_404(Question, pk=pk)` — this single-question action already runs its own precise entitlement check immediately after, so the coarser listing-level lock was both redundant and, worse, would have produced an uninformative 404 instead of a proper, structured 402 for an exhausted-quota student re-answering a previously-unlocked question. The `Option` lookup two lines below already used this exact `get_object_or_404` pattern — this is now consistent with it, not a new convention.

A third, closely related bug was **caught by this phase's own tests before it shipped**: the initial `_start_attempt` implementation consumed free-starter quota *before* checking a Grand Test's optional password, meaning a wrong-password guess on a free-starter-eligible Grand Test would burn the student's one free grant before they ever entered the real password. Fixed by splitting the free-starter check into a non-consuming eligibility check (run during the gate) and the actual consumption (deferred to immediately before the `TestAttempt` is created, after every other check — password, attempt limits — has passed). See `has_free_starter_available()`'s docstring for the full reasoning.

## Frontend integration

**No frontend code was changed this phase**, and this is a deliberate scope decision, not an oversight: nothing in Phase 3's Definition of Done requires a UI change — every checklist item is backend/API-level (server-authoritative usage, atomic consumption, structured denial responses, security). The new `access_denied` response field is additive and ready for a later phase's UI work (Step 27 and the modernization spec's own Phase 11 — Student UX — are where the actual upgrade-prompt/catalog-card UI belongs). Touching the student-facing `Frontend` app now, with zero corresponding automated test coverage there, would add risk without being required by this phase's own success criteria.

**Minimal admin configuration** (Step 27's "smallest safe interface"): the Django admin registration for `FreeStarterPolicy` (added in Phase 2) was extended with the new `validity_days` field in its `list_display`/`list_editable`. No new Admin-panel (Next.js) screen was built — Django admin remains the configuration surface for this phase, matching Phase 2's own precedent and the explicit "do not introduce unnecessary configuration complexity in Phase 3" instruction.

## Migrations

One new migration: `entitlements/migrations/0002_freestarterpolicy_validity_days.py` — adds one nullable field to an existing table. No data migration. Applied only to the ephemeral test database; not applied to any persistent database.

## Security

- Every consumption/decision call site takes `request.user` only — no endpoint or internal function accepts a client-supplied user id, entitlement id, or resource-owner claim (Step 30).
- Staff/admin exclusion (Step 31) is enforced in three independent places (`_try_free_starter`, `_free_starter_eligibility`, `.answer()`'s inline check) — redundant by design, not just in one spot, so a future edit to any one of the three consumption call sites can't silently reintroduce a staff-consumes-free-quota bug without also having to independently break the other two.
- Atomicity (Step 17) is unchanged from Phase 2's already-tested `select_for_update()` pattern in `consume_free_starter` — reused, not reimplemented, at every new call site.
- Idempotency (Step 18) is achieved by reusing existing per-(user, resource) history models (`QuestionAttempt`, `TestAttempt`) as the "already consumed for this" signal, rather than inventing a new idempotency-key system — a retry after a successful consumption is a no-op by construction, not by a special-cased retry check.

## Tests

54 new tests added this phase (see `PHASE_3_COMPLETION_REPORT.md` for the full breakdown and results), covering: `validity_days`/expiry, live QBank consumption through the real `.answer()` API (including the free-subject/already-attempted/staff/real-subscription bypass cases), live Mock/Daily/Grand/PYQ consumption through the real `/tests/{id}/start/` and `/exam-sessions/{id}/start/` APIs (including resume-without-re-consuming, password-after-free-grant, real-purchase-bypass, and the shared-not-per-institution PYQ quota), and role-safety (staff never provisioned or consumed).
