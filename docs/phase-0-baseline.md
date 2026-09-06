# Phase 0 — Baseline Report

**Date:** 2026-09-02
**Scope:** Read audit reports, inspect current repository, run existing tests, document current state. No functional changes made in this phase.

---

## 1. Source documents read

- `MASTER_PLATFORM_AUDIT_REPORT.md` (repo root) — 42-section platform-wide audit.
- `EXAM_SYSTEM_AUDIT_REPORT.md` (repo root) — Exam Management + Bulk Import audit.

Both were authored directly from live codebase inspection earlier in this engagement (ten parallel read-only investigations for the master audit, five for the exam-system audit); their findings are treated as evidence, re-verified against current source before any fix, per the modernization spec's instruction.

## 2. Repository state

- Not a git repository (`git status` → `fatal: not a git repository`). No commit history, no branches. Every file edit in this and future phases is a direct working-tree change — there is no git-based rollback available; rollback for any phase means re-applying the inverse edit or restoring from a prior copy.
- Backend: Django 4.2.30, DRF 3.16.1, Python 3.9.6 (venv at `Backend/venv`). Ten Django apps: `accounts`, `academics`, `tests_app`, `courses`, `billing`, `marketplace`, `videos_app`, `media_library`, `core`, `smart_practice`.
- Admin frontend: Next.js (Admin panel), no automated test suite configured (`package.json` has no `test` script) — frontend changes in this and future phases are verified by `npm run build` (compiles/type-checks) plus manual/code-review verification, not automated tests, unless a test harness is added.
- Student-facing `Frontend` app exists but was out of scope for this phase's inspection (not touched).

## 3. Baseline test run

```
cd Backend && python manage.py test
```

**Result: 559/559 tests pass.** (Full log tail below; the tracebacks visible in the output are deliberate error-path exercises already present in the suite — a mocked `redis down` exception and a mocked missing-GCP-credentials error for the Cloud Tasks stats-enqueue path — not real failures.)

```
Ran 559 tests in 281.598s

OK
```

No test failures, no errors, no skips at baseline.

## 4. Discrepancies found between audit and current code (inspected before any fix)

Per the modernization spec's instruction ("if audit and current code differ: inspect, verify, document, choose safest compatible implementation"), the following were checked during Phase 0/1 inspection and found to refine or correct the audit's framing:

1. **`CRON_SECRET`'s insecure hardcoded fallback** (`hamromentor/settings.py:31`, `os.environ.get('CRON_SECRET', 'dev-cron-secret-change-me')`) — the master audit flagged this as P0. Direct inspection of `Backend/cloudbuild.yaml` confirms `CRON_SECRET` **is** supplied via GCP Secret Manager in the actual production deploy config (`--set-secrets=...,CRON_SECRET=cron-secret:latest,...`), so the insecure fallback is not actually reachable in the current production deployment path. This lowers the finding's real-world urgency without changing that the fallback itself is still bad practice. **Deferred to a later phase** (not part of Phase 1's authorization-defect scope) rather than fixed now, since the correct fix (require the env var, fail loudly if unset, scoped to non-DEBUG) is a deployment-behavior change that deserves its own review rather than a rushed Phase-1 addition.

2. **The `RolePermission` "permission-config data-loss" finding** — direct inspection of `Admin/src/app/accounts/page.js`'s `RolePermissionCard`/`toggle()`/`savePermissions()` found the actual bug is narrower than the master audit's framing suggested. `toggle()` mutates the *full* stored `features` array (initialized from `record?.features || []`), not a reconstruction from only the rendered checkboxes — so a feature key already present in a saved row survives ordinary toggle-and-save cycles even if the UI doesn't render a checkbox for it. The real, confirmed gap is: (a) there was no way to ever *grant* the 5 hidden keys (`billing`, `question_entry`, `exam_schedule`, `exam_archive`, `exam_delete`) through this screen at all, and (b) a role's *first-ever* save (no pre-existing `RolePermission` row) starts from `[]` and can never have contained them. Fixed accordingly in Phase 1 — see `phase-1-completion-report.md`.

3. **`TestViewSet`'s missing role-level enforcement on delete/reschedule** — confirmed exactly as described (plain `IsStaffOrReadOnly`, no distinction for Editor/Teacher). Direct inspection of `accounts/models.py` found the codebase already has a **designed-but-never-wired** mechanism for exactly this: `EXAM_MANAGEMENT_FEATURES = ['exam_schedule', 'exam_archive', 'exam_delete']`, with an explicit code comment describing the intended capability split ("a Question Editor who builds/edits exams but can't schedule sessions, archive, or delete them"). This made the fix more tractable than a from-scratch design decision — it required *wiring up* an existing, documented intent rather than inventing new policy. `exam_archive` (the `is_draft` publish/unpublish toggle) could not be safely gated in Phase 1 because it has no dedicated endpoint — it goes through the same generic `PATCH`/`update` action as every other field edit — gating it would require either a new dedicated action or payload inspection, judged out of scope for a narrowly-targeted P0 patch. **Explicitly deferred**, documented in code and in the Phase 1 completion report.

## 5. No functional changes made in this phase

Phase 0 was inspection and reporting only. All code edits described above were made under Phase 1 (see `phase-1-completion-report.md`), not this phase.
