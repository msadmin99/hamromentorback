# Phase 11 — Analytics Architecture

> **Scope note.** The Phase 11 kickoff prompt described a large
> student-analytics build. The current `implementation-plan.md` Phase 11 —
> the stated source of truth — is much narrower, because the student
> analytics layer **already exists**. The discrepancy and how it was
> resolved are documented in §1. This document describes what the plan
> actually specified, plus the audit findings behind it.

---

## 1. Plan vs. prompt — the discrepancy, stated up front

`implementation-plan.md` → "Phase 11 — Analytics / Performance /
Accessibility" contains exactly three bullets:

1. Extend Analytics *"with the new conversion/usage metrics the spec lists
   (free registrations, free usage, free-to-paid conversion, upgrade
   clicks, quota exhaustion, etc.), sourced from Phase 3's
   `FreeStarterEntitlement` and Phase 2's entitlement layer."*
2. *"Add the composite `Question` indexes the audit identified as missing
   (`subject`+`chapter`+`topic`, `course`+`subject`)."*
3. An accessibility pass, which the plan itself scopes out: *"out of this
   plan's architectural scope beyond noting it belongs here."*

The plan's own framing of the first bullet is the key: **"Extend"**, and
its parenthetical *"confirmed fully live-computed today, no caching layer
— acceptable to keep as-is per the audit"*. The plan does not ask for a
student analytics layer to be built, because one is already there.

Per the kickoff's own instruction — *"follow the current
implementation-plan.md where compatible"* — Phase 11 implemented the
plan's three bullets. The prompt's quality bars (CanViewAnalytics
enforcement, no second grading/ranking system, historical integrity,
IDOR, bounded query counts, privacy, no fabricated metrics) were applied
as **verification** criteria against the existing layer, and one real
gap they exposed was fixed (§6).

One inaccuracy in the plan's own text, noted rather than silently
corrected: it says the analytics layer has *"no caching layer"*. That was
true when the plan was written; a later scalability pass added a
short-TTL, per-user cache on `/api/performance/overview/`. The cache key
always includes `request.user.id`, so cross-user leakage is impossible by
construction.

---

## 2. Existing analytics architecture (audit result)

Two independent, non-overlapping layers already existed:

| Layer | Module | Audience | Nature |
|---|---|---|---|
| Student performance | `tests_app/performance.py` (~37 KB, 20 functions) | the student, self-scoped | live aggregation, per-request memoized, short-TTL per-user cache on the overview |
| Business/revenue | `billing/analytics.py` | admin role or above | live aggregation over Purchase/Subscription/Coupon |

Front ends: `Frontend/src/app/performance/` (KPI cards, trend charts,
subject table, chapter drilldown, comparative card, strengths/weaknesses,
recommendations, activity calendar, mock-test and question analytics) and
`Admin/src/app/analytics/`.

Neither layer stores derived metrics. There are no analytics tables, no
materialized aggregates, and no scheduled aggregation jobs. Phase 11 did
not add any — the plan explicitly accepts request-time aggregation, and
the prompt's own rule applies: *"Do NOT invent Celery/cron if the
repository has no infrastructure for it."*

---

## 3. Metric definitions — derived from the implementation, not invented

Read out of `tests_app/performance.py: kpi_overview()` and pinned by
`tests_app/tests_phase11.py: AccuracyDefinitionTests`:

| Metric | Definition as actually implemented |
|---|---|
| `total_attempts` | count of the student's `status='submitted'` attempts in scope |
| `questions_attempted` | `Answer` rows with a non-null `selected_option` |
| `questions_correct` / `_incorrect` | `is_correct=True` / attempted − correct |
| `questions_unanswered` | `sum(test.question_count) − attempted` |
| **`overall_accuracy`** | **correct / answered × 100** — an unanswered question does not count against it |
| **`overall_score`** | **sum of finalized `TestAttempt.score`** — read, never recomputed |
| `avg_seconds_per_question` | attempt wall-clock seconds / answered |
| `total_study_seconds` | attempt seconds + `VideoProgress.max_position_seconds` |
| streaks, `questions_today` | from `QuestionEvent` |

**Accuracy, score and percentage are three different things** and are
kept apart. Accuracy is `correct/answered`. Score is the finalized marks
total from Phase 6/7 including negative marking. A single "percentage"
figure over a whole date range is deliberately *not* published by
`kpi_overview` — summing maximum-possible-marks across heterogeneous
tests would produce a number that looks precise and is not comparable.

---

## 4. Attempt eligibility

`TestAttempt.STATUS_CHOICES` has exactly two values: `in_progress` and
`submitted`. Two consequences settle the eligibility question completely:

- **Auto-submitted attempts are `submitted`.** Phase 6 deliberately did
  not add a third status — `auto_submitted=True` is an informational
  marker only, because such an attempt is scored, ranked and reviewable
  in every way a manual submission is. Excluding it would erase the
  results of every student who ran out of time.
- **"Missed" is not a status.** A student who never started has no
  `TestAttempt` row. There is nothing to exclude, and nothing was
  invented to represent it.

So the rule is the single filter `status='submitted'`, which
`performance._attempts_qs()` already applied. Pinned by
`AttemptEligibilityTests`.

---

## 5. Historical integrity — what snapshots can and cannot support

Analytics consumes the **finalized** `TestAttempt.score` / `.accuracy`.
Editing a question's marks or flipping its correct answer after
finalization does not move any historical figure — pinned by
`ScoreAuthorityTests`, including the classic "admin fixes a wrong answer
key after students were graded" case.

**Limitation, stated honestly.** `AttemptQuestionSnapshot` (Phase 8)
stores question text, options, explanation, references, order, `marks`,
`negative_marks` and a denormalized `subject_name` **string** — it does
**not** store chapter or topic, and its subject is a name, not an FK.

Therefore:

- Score/accuracy analytics **are** historically exact, because they read
  the finalized attempt totals, which were computed from snapshot marks.
- Subject/chapter/topic **breakdowns** join live `Question.subject/
  chapter/topic`. If a question is re-tagged to a different chapter, a
  historical breakdown shifts with it.

This was not "fixed" in Phase 11, and deliberately so: making taxonomy
breakdowns historical requires snapshotting chapter/topic FKs at
finalization, which is a Phase 8 schema change affecting every future
attempt, with no backfill possible for existing rows. That is a design
decision with its own migration and backward-compatibility questions —
not something to bolt onto an analytics phase. Recorded in §11.

---

## 6. Authorization — and the one gap the audit found

Student analytics is self-scoped by construction: no endpoint accepts a
target-user parameter. Phase 7 wired the canonical
`entitlements.services.can_view_analytics` (`CanViewAnalytics`) via
`_deny_if_cannot_view_own_analytics()` so that invariant is enforced and
testable rather than merely a property of query shape.

**Audit finding:** four of the five analytics endpoints called that
guard. `AttemptComparativeView` did not.

This was **not** an IDOR — its ownership filter
(`TestAttempt.objects.filter(pk=..., user=request.user)`) has always been
present, and returns `NotFound` rather than `PermissionDenied` so another
student's attempt id is indistinguishable from a nonexistent one. But the
capability was unenforced there, so any future change to analytics
visibility policy would have silently skipped this endpoint. Fixed: the
guard now runs **before** the ownership lookup, so a capability denial
cannot be inferred from a 404.

`CanViewRank` stays separate from `CanViewAnalytics`. The comparative
payload deliberately emits no `rank` or `percentile` field, so analytics
visibility can never leak rank visibility — pinned by
`RankingIsNotDuplicatedTests`.

Admin analytics uses `IsAdminRoleOrAbove`, not bare `is_staff`: an
Editor-role staff account is denied.

---

## 7. No second engine, anywhere

- **Grading:** analytics reads `TestAttempt.score`. It never re-derives.
- **Ranking:** exam rank/percentile remain Phase 7's, carried on the
  attempt row. `performance._subject_rank` is a *different concept* —
  cross-student ranking by subject mastery, not exam rank — and is not a
  competing implementation of the same thing.
- **Entitlement:** free-tier metrics read `FreeStarterEntitlement` and
  `EntitlementEventLog`. No parallel quota logic.
- **Authorization:** the canonical `CanViewAnalytics`, not a new
  `can_view_dashboard_stats`-style permission.

---

## 8. Phase 11's new metrics — `entitlements/analytics.py`

Two authoritative sources, for two different questions:

- **`FreeStarterEntitlement` rows** → current state (reach, exhaustion).
- **`EntitlementEventLog`** (`created`/`consumed`/`exhausted`, written by
  `entitlements.provisioning`) → period-scoped figures. It is the only
  source with a per-event timestamp; the entitlement row's running `used`
  counter has no history and is never used for period figures.

Metrics delivered: `students_provisioned`, per-resource-type active /
exhausted / expired / revoked, `students_who_used_free` (all-time and
in-period), `consumption_events_in_period` and its per-resource
breakdown, `provisioned_in_period`, `quota_exhaustion_events_in_period`,
`students_hitting_quota_in_period`, and a free-tier `free_to_paid`
conversion.

Two definitional choices worth stating:

- **"Free registrations" is reported as `students_provisioned`** —
  students who actually hold a grant, not all registered users. Phase 3
  provisions *lazily*, so a registered student who never browsed has no
  row. Reporting registrations as free-tier reach would overstate it.
- **`free_to_paid` is deliberately narrower** than
  `billing.analytics.conversion_metrics()`'s existing
  `free_to_paid_conversion_percent`, whose denominator is every
  registered non-staff user. Both are kept, side by side, under
  different names: they answer different questions and will legitimately
  disagree. Neither silently replaced the other.

### Exhaustion is classified in SQL — and checked against the model

`FreeStarterEntitlement.effective_status` is a Python property and cannot
be filtered in the database. `_effective_status_case()` mirrors it
branch-for-branch in the same precedence (revoked → expired → exhausted →
active), and `EffectiveStatusAgreementTests` asserts the SQL
classification equals the property for every row across the matrix,
including the precedence cases where two conditions hold at once. Drift
fails the suite rather than quietly skewing a dashboard.

### The metric that does not exist

**Upgrade clicks are not tracked.** There is no click/impression model,
no telemetry endpoint, and no beacon anywhere in the platform — Phase 10
rendered upgrade prompts but added no instrumentation. Rather than
substitute a proxy and label it "upgrade clicks", the payload returns an
explicit `unavailable` entry naming what would have to be built, and the
admin UI renders it under "Not measured". A fabricated number here would
be read as real funnel data.

---

## 9. Query strategy and performance

The free-tier block is **aggregate-only**: 11 queries total, and that
count is independent of how many students exist.
`FreeStarterAnalyticsScaleTests` asserts the count is identical at 5 and
60 students, and separately asserts the *numbers are still correct* at
the larger size — a fixed query count computed over a truncated result
set would pass the first check and still be wrong.

No existing query-count baseline was changed anywhere in this phase.

**Index (plan bullet 2).** `Question` had no `Meta` at all, so taxonomy
queries relied only on the single-column FK indexes Django creates.
Added: a composite `question_taxonomy_idx` on `(subject, chapter,
topic)`, matching the platform's real hot path — `subject_breakdown` /
`chapter_breakdown` / `topic_mastery` and the QBank practice-session
filters all narrow subject → chapter → topic, which independent
single-column indexes cannot serve in one seek.

**Not added: the plan's `course`+`subject` composite.** It is not
expressible as an index on this table. `Question.courses` is a
ManyToManyField, so `course` lives in the auto-created through table
(which already carries its own `(question_id, course_id)` unique index —
the index that join actually uses) while `subject` lives on `Question`.
A single index cannot span two tables. Documented rather than silently
dropped.

Declaring `Meta` for the first time also risked introducing a default
`ordering` that would silently change row order for every existing
`Question` queryset. None was declared, and a test asserts
`Question._meta.ordering == []`.

---

## 10. Privacy

The free-tier block is aggregate-only — no user id, username, or email
appears anywhere in it, asserted by `FreeStarterAnalyticsPrivacyTests`
against a payload built from a deliberately identifiable fixture user.
The endpoint is role-gated, not `is_staff`-gated.

Student analytics remains self-scoped; no endpoint accepts a target user.
There are no analytics export endpoints on this platform, so there is no
separate export authorization surface to protect.

---

## 11. Known limitations

1. **Taxonomy breakdowns are not historical** (§5). Re-tagging a question
   shifts historical subject/chapter/topic breakdowns. Score and accuracy
   are unaffected. Fixing this is a Phase 8 schema change.
2. **Upgrade clicks are not measurable** (§8) without new instrumentation.
3. **Per-question timing is not captured for test attempts.**
   `QuestionEvent.time_taken_seconds` exists for QBank practice, but
   `Answer` has no per-question timing, so `avg_seconds_per_question` is
   attempt wall-clock divided by answered — an average, not a measurement.
   No per-question timing metric was fabricated for tests.
4. **No cross-attempt "percentage" metric** is published, by choice (§3).
5. **Teacher analytics does not exist** and was not built — the plan does
   not specify it, and building a cross-student scope would be a new
   authorization surface.
6. **The accessibility pass (bullet 3)** is scoped out by the plan itself.
   Phase 11's own new UI meets the bar (table semantics, `scope`,
   `caption`, no colour-alone encoding, exact values as text, its own
   horizontal-scroll container), but a platform-wide WCAG 2.2 audit was
   not performed and is not claimed.

## 12. Deferred: predictive / AI analytics

Not implemented, and not started: pass-probability prediction, adaptive
recommendations, AI-generated insights, ML models, personalized study
plans. The current plan requires none of them.
