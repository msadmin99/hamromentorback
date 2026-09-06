# Free Starter Usage Rules — Phase 3

Written before implementation, per Step 10/35's requirement to document a consumption rule before coding it. Every rule below is either (a) reused directly from an existing, already-correct precedent already in this codebase, or (b) an explicit decision with its reasoning stated — none are guessed.

**Documentation-naming note (Step 1):** `PLATFORM_MODERNIZATION_PLAN.md` does not exist under that exact name anywhere in the repo. `Backend/docs/implementation-plan.md` (written during Phase 1) is the equivalent document and was read as the intended reference.

---

## 1. What counts as "consumption" — the general principle

**Browsing never consumes. The first genuinely new use of a specific resource does.** This is Step 8's explicit rule, applied consistently across every resource type below via one shared mechanism: **reuse the existing per-(user, resource) history model that already exists for that resource type, and consume only when no such row exists yet.**

This is deliberate — it means Free Starter consumption is idempotent by construction (a retry, double-click, or re-opening of an already-unlocked resource is detected the same way an ordinary "has this student already done this" check already works elsewhere in the codebase) and it requires **no new tracking table** (Step 18: "do not invent unnecessary duplicate systems").

## 2. QBank consumption

**Rule: consume 1 unit the first time a student submits an answer to a specific question in a non-free subject. Re-answering the same question later never consumes again, regardless of current entitlement status.**

- **Detection mechanism:** `academics.models.QuestionAttempt` already has `unique_together` on `(user, question)` — one row per student per question, created/updated by the existing `record_question_result()` service. "Has this student already attempted this exact question" is `QuestionAttempt.objects.filter(user=user, question=question).exists()`, checked *before* the answer is recorded.
- **Why first-answer, not every-answer:** the spec's own example frames the quota as "100 free questions" — a count of *distinct questions unlocked*, not a count of *answer submissions*. Charging a second time for re-attempting/reviewing a question already unlocked would be an ungenerous, confusing reading not supported by the example, and is inconsistent with the Mock Test precedent the spec gives explicitly (Step 11: "do not consume a Mock Test quota repeatedly merely because the student opens the same existing completed result").
- **Why not "opening a question," "starting a session," etc.:** Step 10 explicitly warns against assuming "opening a question = consumption" — a student must be able to freely browse/preview without being charged. The only server-recorded, unambiguous "the student actually engaged with this question" event already in this codebase is an answer submission (`option_id` present in the request) — this is the natural, already-instrumented consumption point.
- **Free subjects are never gated or consumed** — `Subject.is_free=True` subjects remain exactly as unrestricted as they are today, regardless of Free Starter status. This matches `entitlements.services.can_view_qbank`'s Phase 2 design exactly (free-subject short-circuit before any entitlement check runs).
- **Already-attempted questions are never re-gated** — once a student has legitimately attempted a specific question (via any entitlement — free starter, subscription, or otherwise), reopening/re-answering that same question is never blocked, even if their entitlement has since lapsed. This mirrors the Mock Test precedent above and avoids the confusing UX of losing access to something already legitimately unlocked.

**A necessary, isolated, documented exception to "do not fix unrelated access bugs" (Step 33):** `QuestionViewSet.answer` (`academics/views.py`) had **no server-side commercial-access check at all** before this phase — confirmed by direct re-read of the current code, not assumed. The only existing gate is at the *listing* level (`_locked_subject_ids` excludes Pro-locked subjects' questions from the browse/list endpoints for non-entitled students), which does not stop a direct API call against a known/guessed question id. Free Starter enforcement is structurally meaningless without *some* gate existing on the actual consumption endpoint — the quota can only ever mean anything if something checks and decrements it. This phase therefore adds exactly that check, scoped as narrowly as possible: only for the first-ever attempt at a non-free-subject question, using the existing `can_view_qbank` decision function unmodified. Every other case (free subjects, already-attempted questions, students who do have a real subscription) is completely unaffected — confirmed by dedicated regression tests.

## 3. Mock Test consumption

**Rule: consume 1 unit the first time a student *starts* a specific pro Mock Test they have never attempted before (any attempt status). Subsequent attempts of the *same* test (retries, resumes) never consume again.**

- **Detection mechanism:** `tests_app.models.TestAttempt.objects.filter(user=user, test=test).exists()`, checked before creating a new attempt — the existing model, not a new one.
- **Granularity:** the spec's example ("Mock: 1 complete mock test") is a platform-wide count of *how many distinct Mock Tests* a student may unlock for free, not a per-test attempt allowance — matches `FreeStarterPolicy`'s existing coarse, resource-type-level (not resource-id-level) granularity established in Phase 2. Starting *any one* pro Mock Test consumes the single free unit; every *other* pro Mock Test then requires real entitlement (Step 11: "Mock Test 02: locked if no other entitlement exists").
- **Existing `max_attempts`/resume logic is untouched** — a student re-entering their already-started free Mock Test goes through exactly the same existing `TestAttempt` resume path (`_start_attempt`'s own `existing = attempt_qs.filter(status='in_progress').first()` check, unmodified) as any other test; Free Starter consumption never runs a second time for it.

## 4. Daily Test consumption

**Rule: identical mechanism to Mock Test** — consume 1 unit the first time a student starts a specific pro Daily Test session they've never attempted, using `TestAttempt.objects.filter(user=user, test=test).exists()`. The spec's example ("Daily: 2 selected/free tests") is a count of *how many distinct Daily Tests* may be started for free, consistent with Mock's granularity.

- **Session window is untouched** — Free Starter only participates in the *entitlement* gate; the existing `ExamSession` start/end window enforcement (`_start_attempt`'s `session.refresh_status()`/window checks) runs exactly as before, unmodified, and still applies regardless of which entitlement source granted access.
- **Viewing an upcoming/missed Daily Test never consumes** — consumption only happens inside `_start_attempt`, never at listing/detail time (Step 12 explicit rule, matches the general principle in §1).

## 5. Grand Test consumption

**Rule: identical mechanism** — consume 1 unit the first time a student starts a specific pro Grand Test they've never attempted, only reached when no `GrandTestAccess` grant already exists for them. Admin-configurable, expected to default to `0` (no free Grand Test access) unless an admin explicitly activates a `grand_test` `FreeStarterPolicy` row with a nonzero quantity (Step 13: "0 or 1 promotional test... do not hardcode which").

- **Password remains a strictly additional layer** — `Test.access_password`, when set, is still checked *after* a successful free-starter grant, exactly as it already is for a real purchase (Step "GRAND TEST PASSWORD": password is never a substitute for entitlement).
- **Session/ranking rules are untouched** — everything about how a Grand Test attempt is scored, ranked, and reviewed is completely unaffected by which entitlement source authorized entry.

## 6. Past Year Questions (PYQ) consumption

**Correction made during implementation (Step 1's "do not assume the docs are more current than the source" caught this before it shipped wrong):** an earlier draft of this document described PYQ consumption as QBank-style (per-question, via `QuestionAttempt`). Re-verifying against the actual access function shows this is wrong — `billing.access.has_pyq_access(user, test)`'s own docstring is explicit: PYQ is "membership-gated like QBank... but **per-test, not per-subject**: admins mark specific PYQ tests `is_pro=True`." In this codebase, a Past Year exam is a `tests_app.Test` row with `exam_type='pyq'`, delivered through the exact same `_start_attempt`/`TestAttempt` path as Mock/Daily/Grand — there is no separate Question-level PYQ practice mode. The implementation (and this corrected rule) follows the real mechanism, not the initial, wrong draft.

**Rule: identical mechanism to Mock/Daily/Grand (§3–5)** — consume 1 `pyq` unit the first time a student starts a specific pro PYQ `Test` they've never attempted, via `TestAttempt.objects.filter(user=user, test=test).exists()`. **One shared quota across all institutions, not split per institution.**

This is the one place this phase deliberately does **not** follow the modernization spec's own illustrative numbers literally, and the reasoning is written out in full because Step 5 explicitly forbids guessing here:

- The spec's examples list four *separate* numbers (IOM 50 / BPKIHS 50 / MOE 50 / KU 50), which reads as suggesting per-institution quotas.
- However, re-inspecting the actual data model (`tests_app.Test.university`, Step 1's mandate) shows the institution is a **free-text `CharField`, not a fixed enum** — there is no closed, code-level list of "the" universities anywhere in this codebase. Baking exactly four named institutions into `FreeStarterPolicy`'s `resource_type` choices would mean hardcoding a specific, mutable business fact (which institutions exist) into a Python enum — arguably a *worse* form of hardcoding than the quota numbers the spec explicitly warns against, since a fifth university, a renamed one, or a market where these four don't apply would require a code change and migration to represent, defeating the entire point of an admin-configurable policy.
- The existing, Phase-2-established `FreeStarterPolicy` convention (`resource_type` is coarse, platform-wide, matching the QBank/Mock/Daily/Grand granularity) is the consistent, lower-risk choice, and does not foreclose per-institution quotas later — if that is confirmed as the actual product requirement, it would need a *data-driven* mechanism (e.g., a policy keyed by the actual `Test.university` values present in the system) rather than a hardcoded four-way enum, which is a larger design question than this phase should resolve unilaterally.

**This is flagged explicitly in the completion report as a business decision still required**, not silently resolved.

## 7. Expiry

`FreeStarterPolicy` gains one new field this phase: `validity_days` (nullable `PositiveIntegerField`; blank/null = "no expiry"). `provision_free_starter()` computes `FreeStarterEntitlement.expires_at = valid_from + timedelta(days=policy.validity_days)` at provisioning time if set, else leaves `expires_at` null. This reuses the *existing* `FreeStarterEntitlement.effective_status` property (built in Phase 2, unmodified) to determine live validity — no new expiry-checking logic is introduced, only a new admin-configurable input to the field that already existed.

## 8. Multiple-entitlement precedence

Unchanged from Phase 2's already-correct design: `entitlements.services.can_view_qbank`/`can_start_test` always check real commercial entitlement (subscription, scholarship, Grand Test purchase, course/batch/individual assignment) *first*; Free Starter is consulted only as a fallback when every other check fails. An exhausted or expired Free Starter entitlement therefore can never mask or interfere with a genuinely valid subscription/combo/scholarship/purchase — the two are structurally independent checks, not a shared boolean (Step 15, Step 23).

## 9. Upgrade behavior

Every denial that Free Starter was consulted for (and found either exhausted or genuinely unavailable) returns a structured `access_denied` object (Step 19):
```json
{"reason": "free_limit_reached", "source": "free_starter", "upgrade_available": true}
```
alongside the existing `detail`/`code: "purchase_required"` fields already returned by these endpoints — additive, not a breaking response-shape change. No new user-facing copy/strings were introduced this phase (Step 20's "centralized messaging" guidance is honored by *not* inventing frontend strings at all — see the Frontend section of `FREE_STARTER_IMPLEMENTATION.md` for why no frontend change was made this phase).

## 10. Repeated-request behavior

Every consumption call site uses the same `entitlements.provisioning.ensure_and_consume_free_starter()` wrapper (lazy-provision, then atomic `select_for_update()` consumption — both idempotent/race-safe, per Phase 2's already-tested `consume_free_starter`). A double-click, network retry, or concurrent duplicate request against the same resource is safe by construction: the "already attempted" check (via `QuestionAttempt`/`TestAttempt`) means a retry after a successful first consumption never attempts to consume a second time at all, and the underlying `select_for_update()` lock means even two genuinely concurrent *first* attempts can never both succeed past the limit (verified by a real multi-thread test, matching Phase 2's established testing pattern).
