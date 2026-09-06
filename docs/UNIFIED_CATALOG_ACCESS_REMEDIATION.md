# Unified Exam Catalog Visibility / Access Remediation

> A targeted platform-wide correctness remediation. **Not a numbered
> roadmap phase.** No Phase 12 was created; the sequence still ends at
> Phase 11. Exam Builder remains deferred and untouched.

---

## 1. The reported symptom

A non-paid student saw "0" Daily Tests while a paid student saw the real
catalog — a possible violation of the platform's product model:
**entitlement controls consumption, not legitimate catalog visibility.**

## 2. Root cause — confirmed by audit, not assumed

`courses.access.eligible_course_ids(user)` is the single gate behind
Test/Question/Video catalog visibility everywhere in the app
(`tests_app.access.visible_test_queryset`,
`academics.access.question_course_scoped`/`_course_scoped` all call it).
It reads **only `courses.Enrollment`** — nothing else.

Only two things ever created an `Enrollment` row:

1. An admin manually approving an `EnrollmentRequest`
   (`EnrollmentRequestViewSet.approve()`).
2. A successful paid purchase/subscription
   (`billing.payment_service._ensure_enrollment()` — itself a fix for the
   *identical* bug on the paid side, see that function's own docstring:
   *"a student could pay... and still see zero content."*).

**Registration created neither.** `RegisterSerializer.create()` stored
the student's chosen course on `User.course` (a plain `Course.prefix`
string) and provisioned Free Starter, but never turned that choice into
catalog membership. So every self-registered free student was
catalog-blind — 0 Daily/Mock/Grand/PYQ/QBank content — until an admin
happened to enroll them, which in practice almost only ever happened as a
side effect of paying. That asymmetry is the entire bug, reproduced
directly in `courses/tests_catalog_registration.py::
test_this_is_the_exact_bug_reproduced_and_fixed`.

## 3. No commercial filter exists anywhere in the catalog pipeline

Before writing any fix, every `has_mock_test_access` / `has_daily_test_
access` / `has_pyq_access` / `has_qbank_access` / `get_grand_test_access`
call site in the codebase was grepped and read. **None of them filter a
catalog queryset.** Each one gates either:

- an *action* (`_start_attempt`, the one true enforcement point), or
- a *presentation field* (`card_access.py`'s `access` block, the legacy
  `has_access` field — both documented as informational).

`TestViewSet.get_queryset()` applies `visible_test_queryset` (academic
eligibility only) and query-param filters that are either narrowing
(`?course=`, always within the eligible set) or admin-only
(`?access=pro/free`, `?status=`, documented in the code as *"student-
facing pages simply never send them"*). `academics.SubjectViewSet` uses
the same course-eligibility gate and no commercial one; `QuestionViewSet`
correctly excludes individual **questions** in a Pro-locked subject —
never the subject/chapter/topic listing itself. `card_status` (the
legacy field Phase 10 kept for compatibility) is derived purely from
`scheduled_start`/`scheduled_end` and attempt state — no entitlement
input at all.

**This is why one fix — at the registration layer — resolves the bug for
all five exam families simultaneously**, rather than requiring five
separate remediations: they all share the one upstream data-completeness
gap, not five independent filtering defects.

---

## 4. The fix

### 4a. Code fix — `accounts/serializers.py: RegisterSerializer.create()`

Immediately after the existing Free Starter provisioning, a free
`Enrollment` (`access_type='free'`) is created for the course the student
selected at registration — mirroring `_ensure_enrollment`'s exact
`update_or_create` shape, with `'free'` instead of `'package'`. `'free'`
is not a new concept: `Enrollment.ACCESS_CHOICES` has carried it since
Phase 2; it was simply never wired to the one path that needed it.

Best-effort, matching the Free Starter provisioning pattern immediately
above it: a blank or unmatched course value never fails registration.

### 4b. Data fix — migration `courses/0008_backfill_enrollment_from_
registered_course.py`

The code fix only helps *future* registrations. Students who registered
before it exist in the same catalog-blind state today. A data migration
backfills the missing `Enrollment` for every non-staff user whose
`course` matches a real `Course.prefix` and who has no `Enrollment` at
all yet — mirroring `0007_backfill_enrollment_from_subscription` (the
established precedent for this exact bug class) field-for-field: same
print-based audit trail, same `student_code` regeneration workaround for
historical migration models, same guarantee of being purely additive
(never touches an existing `Enrollment`, `User`, `Subscription`, or
`Purchase` row).

**No schema changed.** `makemigrations --check` → *No changes detected*.
This is a `RunPython` data migration only.

### 4c. Frontend fix — `components/plans/SingleTestSection.js`

A secondary, real "paid user sees a false lock" finding from the audit:
the Grand Test single-purchase catalog computed ownership as
`ownedTestIds.has(t.id) || !t.is_pro`, where `ownedTestIds` came from a
`grandTestAccess` prop carrying **only direct `GrandTestAccess`
purchases**. A student who held the same Grand Test through a
Subscription, Combo, Scholarship, or an admin assignment saw "Buy Now" on
a test they could already start.

Fixed to read `t.access?.can_start || t.access?.can_continue || t.access
?.can_review` — the same server-derived projection (`tests_app/
card_access.py`) `ExamCard` already reads elsewhere, which every
entitlement source is already resolved into. No new prop, no new
request.

---

## 5. Catalog vs. access — the invariant, and how it's proven

| Layer | Determines | Server-derived from |
|---|---|---|
| Catalog membership | Can this student even know the resource exists | `visible_test_queryset` / `question_course_scoped` — course/batch/assignment eligibility only |
| Access state | Can this student act on it right now | `entitlements.services.can_start_test` (canonical) → `tests_app/card_access.py` (batched projection) → `access` block |

`tests_app/tests_catalog_parity.py` provides the reusable
`assert_catalog_parity(testcase, free_user, paid_user, filters=)` helper
the remediation brief asked for, and applies it against the **real**
`TestViewSet` pipeline (`visible_test_queryset`), not a hand-rolled
equivalent — so a real regression shows the exact differing ids, not just
a boolean mismatch.

### Per exam type

- **Daily / Mock** — identical catalog membership for a free vs. paid
  student in the same course; `can_start_test` correctly still denies the
  free student and allows the paid one.
- **Grand** — proven as the *strongest* form of the invariant: catalog
  membership matches even when **neither** student holds Grand access
  (it's presence-based via `GrandTestAccess`, not subscription-based, so
  the shared "paid student" fixture doesn't grant it either). Detail-page
  response is checked directly: title/duration visible, `access.can_start
  = false`, `access.upgrade_available = true`. A direct purchase is then
  shown to change access without moving catalog membership at all.
- **PYQ** — all four named institutions (IOM, BPKIHS, MOE, KU) checked in
  one parametrized test, since they're `Test.university` values on the
  same pipeline, confirmed identical rather than assumed. The
  `universities()` grouping endpoint itself is checked for the same
  parity. **PYQ Free Starter consumption is confirmed still Test-level**
  — `tests_app.card_access.FREE_STARTER_RESOURCE['pyq'] == 'pyq'`,
  drawn down once per exam start, not per question — asserted directly
  against the real mapping, not merely stated.
- **QBank** — audited on its own terms per the brief's own instruction
  (Subject/Chapter/Topic catalog, not Test rows). Subject listing and its
  annotated question count are identical regardless of entitlement;
  individual **question** content for a Pro-locked subject stays excluded
  — confirmed as the correct, *unrelated* consumption guard, not a
  catalog-visibility regression.

### Free Starter

- Exhausted quota does **not** remove a test from the catalog list
  response — confirmed against the live `/api/tests/` endpoint, with
  `access.can_start = false` and `access.upgrade_available = true` on the
  still-visible card.
- Browsing (list, by exam type, with `?search=`) never consumes quota or
  writes an `EntitlementEventLog` row — checked directly against live
  usage counters before/after four separate catalog requests.
- A valid paid entitlement overrides an exhausted Free Starter without
  touching its usage counter at all (`used` stays exactly where it was).

### Direct API security — unaffected

Catalog visibility is proven to never become an authorization bypass: the
free student sees a Pro Daily Test in `/api/tests/?exam_type=daily`, and
`POST /api/tests/{id}/start/` still returns 402/403. A guessed attempt id
belonging to another (equally enrolled) student still gets 403/404 on
`answer/`.

### Completed attempts survive entitlement change

A submitted Daily Test attempt stays in the catalog and reviewable
(`can_review_attempt`) after the subscription that granted the original
Start is expired — `can_review_attempt` was already, correctly, ownership
+ status-only with zero entitlement dependency; this is confirmed by
test, not newly built.

---

## 6. What was deliberately NOT built

- **No new access engine.** No `MockAccessService`/`DailyAccessService`/
  etc. The canonical pipeline (queryset → `card_access.py` → `Access
  Decision` → frontend) was reused throughout; only the missing
  *prerequisite data* (Enrollment) was supplied.
- **No rewrite of Free Starter, commerce, or the access engine.**
  `entitlements/services.py`, `billing/access.py`, and `tests_app/
  card_access.py` are unmodified.
- **No hardcoded Free Starter quantities.** The existing `Free
  StarterPolicy`-driven configuration is untouched and still the sole
  source of truth.
- **No fabricated "5–10 sample questions" behavior.** No such mechanism
  exists in the backend today, and none was invented in the frontend.
  QBank's existing consumption remains exactly what it was: a
  question-level draw against the configured `FreeStarterPolicy`
  quantity, gated in `entitlements/services.py`, unchanged by this work.
- **No change to anonymous-user visibility.** `visible_test_queryset`
  still resolves an anonymous request to almost nothing (only the
  `needs_course_review` legacy fallback). The brief's own instruction
  (Step 22) was to determine actual product intent rather than assume —
  that is a separate, larger product decision than the confirmed
  Enrollment-gap bug, and was left untouched. Recorded as a known
  limitation, not silently resolved either way.
- **No Exam Builder work.**

---

## 7. Files changed

**Backend:**
- `accounts/serializers.py` — `RegisterSerializer.create()`: free
  Enrollment creation (code fix).
- `courses/migrations/0008_backfill_enrollment_from_registered_course.py`
  (new) — data backfill.
- `courses/tests_catalog_registration.py` (new, 13 tests) — the fix and
  the migration, including the direct bug reproduction.
- `tests_app/tests_catalog_parity.py` (new, 18 tests) — cross-exam-type
  parity, Free Starter, direct-API security, completed-attempt survival.

**Frontend:**
- `components/plans/SingleTestSection.js` — false-lock fix.
- `app/plans/page.js` — dropped the now-unused `grandTestAccess` prop.
- `components/plans/singleTestSection.test.mjs` (new, 5 tests) —
  permanent regression guard against reintroducing the `is_pro`/
  direct-purchase-only inference.

**No changes** to `tests_app/access.py`, `tests_app/card_access.py`,
`tests_app/views.py`'s `get_queryset`, `academics/access.py`,
`entitlements/services.py`, or `billing/access.py` — the audit confirmed
none of them contain the defect.

## 8. API contract

**No API contract changed.** `RegisterSerializer`'s request/response
shape is identical; Enrollment creation is an internal side effect. The
migration adds no field and no endpoint. `SingleTestSection.js` no longer
sends a `grandTestAccess` prop to itself, but that was never part of any
HTTP contract — it was already-fetched data reshaped client-side.

## 9. Performance

No queryset, projection, or serializer touched by this remediation — the
existing batched card-access projection and its query-count guarantees
(`tests_app/tests_phase10.py::CardAccessApiTests`, re-run and confirmed
green) are unaffected by construction, not merely by re-running the test.
Registration
gains exactly one additional `Course.objects.filter(prefix=...).first()`
plus, at most, one `Enrollment.objects.update_or_create()` — a single
one-time cost on account creation, not a per-request or per-card cost.

## 10. Security

No authorization changed. `_start_attempt`, `SubmitAnswerView`,
`SubmitTestView`, `TestResultView`, and every `CanView`/`CanStart`/
`CanReview`/`CanViewSolutions` capability check are unmodified — verified
directly (§5, direct-API tests), not inferred from the catalog fix alone.
Enrollment grants **catalog visibility only**; a dedicated test
(`test_grants_catalog_membership_only_not_any_commercial_entitlement`)
confirms a freshly-registered free student still cannot start a Pro test.

## 11. Known limitations

1. **Anonymous catalog visibility was not changed or decided** (§6). The
   brief instructed determining actual product intent rather than
   assuming; that decision was left to a future, explicitly scoped item.
2. **The "PURCHASED" badge label** on `SingleTestSection` now covers every
   access source (subscription, combo, scholarship, assignment — not
   only a direct purchase), so the exact word is slightly imprecise for
   a non-purchase source, though the *behavior* (Start vs Buy) is
   correct. Left as-is: a wording nit, not a functional or security
   issue, and changing copy further risked scope creep beyond the
   confirmed defect.
3. **`user.program`** (the sibling field to `user.course`) is unused by
   this fix — only `Enrollment` (keyed by `Course`) gates catalog
   visibility; `program_group` filtering happens elsewhere and was not
   part of the confirmed defect.

## 12. Deployment readiness

Code-ready, not deployed. One data-only migration (no schema change),
tested against a disposable copy of the local dev database (never
production) — applied cleanly, backfilled the expected rows, left all
User/Subscription/Purchase data untouched. Production database was not
touched by this session; the migration has **not** been run against it.
