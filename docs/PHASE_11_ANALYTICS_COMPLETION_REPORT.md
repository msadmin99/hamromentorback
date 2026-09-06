# Phase 11 — Analytics — Completion Report

**Status:** complete. Backend `Ran 951 tests — OK` (909 → 951, +42).
Both frontends build. Nothing deployed. Production DB untouched.

---

## 1. Plan vs. prompt — reported before implementation

The Phase 11 kickoff prompt described a large student-analytics build.
The current `implementation-plan.md` Phase 11 is **three bullets**, and
its own wording is *"Extend Analytics (confirmed fully live-computed
today, no caching layer — acceptable to keep as-is per the audit)"* — it
does not ask for student analytics to be built, because a full student
analytics layer already exists (`tests_app/performance.py`, ~37 KB, 20
functions, plus a complete `/performance` dashboard).

Per the kickoff's own rule — *"follow the current implementation-plan.md
where compatible"* — the plan's three bullets were implemented. The
prompt's quality bars were applied as **verification criteria** against
the existing layer, and the one real gap they exposed was fixed.

Full detail: `PHASE_11_ANALYTICS_ARCHITECTURE.md` §1.

## 2. Phase numbering

Confirmed unchanged. Phase 11 = Analytics; Exam Builder remains deferred
and out of the numbered sequence. It was not implemented.

## 3. What was delivered

| Plan bullet | Outcome |
|---|---|
| 1 — free/conversion/usage metrics | **Done** — new `entitlements/analytics.py`, composed into the existing admin dashboard, plus a new admin UI section |
| 2 — composite `Question` indexes | **Partially possible** — `(subject, chapter, topic)` shipped; `course`+`subject` is **not expressible** as an index (see §9) |
| 3 — accessibility pass | **Scoped out by the plan itself.** Phase 11's own new UI meets the bar; no platform-wide WCAG 2.2 audit was performed and none is claimed |

Plus one audit fix: `AttemptComparativeView` was the one analytics
endpoint of five that never ran `CanViewAnalytics`.

## 4. Metric definitions (read out of the code, not invented)

`accuracy = correct / answered` (an unanswered question does not count
against it). `score` = the sum of finalized `TestAttempt.score`, read
never recomputed. A cross-attempt "percentage" is deliberately **not**
published — summing maximum marks across heterogeneous tests would look
precise and not be comparable. Full table in the architecture doc §3,
pinned by `AccuracyDefinitionTests`.

## 5. Attempt eligibility

Two statuses exist: `in_progress`, `submitted`. Auto-submitted attempts
are `submitted` with an informational marker (Phase 6's deliberate
choice) and **do** count. "Missed" is not a status — such an attempt has
no row, so there is nothing to exclude and nothing was invented for it.
The existing filter `status='submitted'` is correct; pinned by
`AttemptEligibilityTests`.

## 6. Historical integrity — and its honest limit

Score and accuracy are historically exact: editing a question's marks or
flipping its correct answer after finalization moves nothing
(`ScoreAuthorityTests`).

**Limitation:** `AttemptQuestionSnapshot` stores `marks`,
`negative_marks` and a denormalized `subject_name` **string** — it does
**not** store chapter or topic. So subject/chapter/topic *breakdowns*
join live `Question` taxonomy and will shift if a question is re-tagged.
This was not "fixed" here: making it historical is a Phase 8 schema
change affecting every future attempt with no backfill possible for
existing rows. Recorded, not papered over.

## 7. Security

`AttemptComparativeView` now runs `CanViewAnalytics`, **before** the
ownership lookup so a capability denial cannot be inferred from a 404.

This was **not** an IDOR — the ownership filter was always present, and
returns `NotFound` (not `PermissionDenied`) so another student's attempt
id is indistinguishable from a nonexistent one. What was missing was
capability enforcement, meaning a future analytics-visibility policy
change would have silently skipped this endpoint.

`CanViewRank` stays separate: the comparative payload emits no `rank` or
`percentile`, so analytics visibility cannot leak rank visibility.
Admin analytics is role-gated (`IsAdminRoleOrAbove`) — an Editor-role
staff account is denied, asserted by test. **No security claim here rests
on hidden UI.**

## 8. Privacy

The free-tier block is aggregate-only. `FreeStarterAnalyticsPrivacyTests`
builds it from a deliberately identifiable fixture user and asserts the
serialized payload contains no username, email, or user id.

## 9. Database changes

One migration: `academics/0026_question_question_taxonomy_idx.py` — an
`AddIndex` only, no data migration, no column change.

`Question` previously had **no `Meta` at all**. Declaring one risked
introducing a default `ordering` that would silently change row order for
every existing `Question` queryset; none was declared, and a test asserts
`Question._meta.ordering == []`.

**The plan's `course`+`subject` composite was not added** because it
cannot exist: `Question.courses` is a ManyToManyField, so `course` lives
in the auto-created through table (which already carries its own
`(question_id, course_id)` unique index) while `subject` lives on
`Question`. One index cannot span two tables.

**Migration verification.** Fresh DB: covered by the suite, which builds
the schema from migrations. Existing DB: applied to a **copy** of the
local dev database in the scratchpad — applied cleanly, index created,
rows intact. Honest caveat: that dev DB holds **0 question rows**, so
this verifies the migration mechanism, *not* index-build time on a
populated production table. It was not run against production.

## 10. Performance

The free-tier block is aggregate-only: **11 queries, independent of
student count** — asserted identical at 5 and 60 students, with a
separate assertion that the numbers are still correct at the larger size
(a fixed query count over a truncated result set would pass the first
check and still be wrong).

**No existing query-count baseline was changed anywhere in this phase.**

## 11. Verification

| Check | Result |
|---|---|
| Backend suite | `Ran 951 tests in 152.792s` — **OK** |
| Baseline → final (backend) | 909 → 951 (**+42**) |
| Frontend tests (student app) | 24 → 24, **24/24 pass** (no frontend test changes this phase) |
| Admin build | ✓ Compiled successfully; 30/30 static pages |
| Student app build | ✓ Compiled successfully; 35/35 static pages |
| Lint | `npx eslint src/app/analytics/page.js` — **clean, 0 findings** |
| `makemigrations --check` | **No changes detected** |
| Migrations | 1 (index only) |
| Flaky tests | none |
| Production DB | **not touched** |
| Deployed | **no** |

## 12. API compatibility

Additive only. `GET /api/analytics/` gains a `free_starter` block; every
pre-existing key is unchanged, asserted by
`test_existing_analytics_blocks_are_unchanged`. No student analytics
response shape changed. The only behavioral change to an existing
endpoint is the added capability check on `/api/attempts/{id}/
comparative/`, which returns 403 only where the capability itself denies
— for an ordinary authenticated student it is a no-op.

## 13. Known limitations

1. **Upgrade clicks are not tracked** and were not proxied. No click or
   impression model, no telemetry endpoint exists. The payload returns an
   explicit `unavailable` entry; the admin UI renders it under "Not
   measured". A fabricated number would read as real funnel data.
2. Taxonomy breakdowns are not historical (§6).
3. **Per-question timing is not captured for test attempts.** `Answer`
   has no timing field, so `avg_seconds_per_question` is attempt
   wall-clock ÷ answered — an average, not a measurement. No per-question
   timing metric was fabricated.
4. No teacher analytics exists; the plan does not specify it and building
   a cross-student scope would be a new authorization surface.
5. Index-build cost on a populated production table is unverified (§9).

## 14. Deferred: predictive / AI analytics

Not implemented and not started: pass-probability prediction, adaptive
recommendations, AI-generated insights, ML models, personalized study
plans. The current plan requires none.

## 15. Deployment readiness

Code-ready, not deployed. The migration is additive and index-only. It
has **not** been run against production, and no deployment was performed
or attempted — that remains a separate, explicitly-approved step.
