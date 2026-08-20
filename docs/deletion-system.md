# Permanent Deletion System

This document covers the guarded hard-delete system added across `core`,
`academics`, `courses`, `marketplace`, `accounts`, `tests_app`, and
`media_library`. It's a hard-delete system, not soft-delete: when a delete
succeeds, the row and its owned files are gone. Every high-risk delete is
guarded so it can't run when doing so would silently corrupt another
resource (a paying student's access, a financial record, a ranked exam
result).

## Principles

1. **Guard, then delete.** Every guarded `destroy()` checks for dependent
   records *before* calling `super().destroy()`. If a block exists, return
   `400` with a clear message and stop — never partially delete.
2. **Never let a `ProtectedError` reach the client as a raw 500.** If a
   guard is missing or incomplete and Django's `on_delete=PROTECT` fires
   anyway, `destroy()` catches it, logs a failure entry, and returns a
   clean `500` with a generic "no partial deletion" message instead of a
   stack trace.
3. **Audit every attempt, not just successes.** A blocked or failed delete
   is logged too — the audit log is a record of "what did an admin try to
   delete and what happened," not just "what got deleted."
4. **The audit log stores metadata, never content.** No question text, no
   course description, no answer data — just resource type/id/label
   (e.g. a question's `public_id`, a course's `name`), actor, result, IP,
   user-agent, timestamp.
5. **Deactivate instead of delete when in doubt.** Every guard message
   tells the admin what to do instead (e.g. "set it inactive," "archive
   it," "cancel its sessions first").

## Shared infrastructure

### `core.models.DeletionAuditLog`

One row per delete *attempt* (success or failure): `actor` (nullable FK,
`SET_NULL` — the log entry survives even if the actor account is later
deleted), `actor_email`, `resource_type`, `resource_id`, `resource_label`,
`result` (`success`/`failure`), `failure_reason`, `ip_address`,
`user_agent`, `created_at`. Indexed on `(resource_type, resource_id)` and
`created_at` for lookups.

### `core.deletion_audit`

- `record_deletion(request, resource_type, resource_id, resource_label='', result='success', failure_reason='')`
  — the only place that writes to `DeletionAuditLog`. Call it on every
  branch of a guarded `destroy()` (blocked, failed, succeeded), never
  write to the model directly.
- `delete_file_field(file_field)` — safely deletes the underlying storage
  file behind a Django `ImageField`/`FileField`. Swallows storage errors
  (a network hiccup shouldn't block the DB row from being deleted); no-op
  on an empty field.

### `media_library.service.delete_media_asset(asset)`

Handles the one non-obvious case in this system: **content-hash
deduplication**. Multiple `MediaAsset` rows can point at the exact same
GCS objects (`storage_key`) when two uploads happen to have identical
content. Before deleting the original + variants from GCS, it checks
whether any other `MediaAsset` row still shares that `storage_key`; if so,
it skips the GCS delete and only removes the DB row. GCS failures are
swallowed — the DB row is the source of truth for "does this asset exist,"
so an orphaned GCS object is a cheap failure mode, a half-deleted DB row
is not.

## Guarded resources

| Resource | Endpoint | Blocked when | On success |
|---|---|---|---|
| `academics.Question` | `DELETE /api/questions/{id}/` | Has practice-attempt history, or is used in a test that has student attempts | Deletes its `Option`s, both legacy `ImageField`s and both `MediaAsset` images (question + explanation + each option's) |
| `courses.Course` | `DELETE /api/courses/{id}/` | Has active `Enrollment`s or `Subscription`s | Deletes the course row (and its `CoursePackage`s via existing `CASCADE`) |
| `marketplace.TeacherCourse` | `DELETE /api/teacher-courses/{id}/` | Has student `CourseEnrollment`s | Deletes the course, its sections, and lessons (existing `CASCADE`) |
| `accounts.User` (admin accounts) | `DELETE /api/auth/admin-accounts/{id}/` | Owns marketplace courses (`taught_courses`), or has `Purchase` history | Deletes the account |
| `tests_app.Test` | `DELETE /api/tests/{id}/` | Has student `TestAttempt`s, or has any `ExamSession` (`exam_version` is `PROTECT`) | Deletes the test and its `TestQuestion` rows |
| `tests_app.ExamSession` | `DELETE /api/exam-sessions/{id}/` | Has `TestAttempt`s | Deletes the session |
| `media_library.MediaAsset` | `DELETE /api/media/{id}/` | Never blocked by dependents (an asset can be orphaned safely) — staff-only | Dedup-aware GCS cleanup, see above |

Every row in this table follows the same shape in code:

```python
def destroy(self, request, *args, **kwargs):
    from core.deletion_audit import record_deletion

    obj = self.get_object()
    label = obj.some_display_field

    if obj.some_dependency.exists():
        msg = 'Clear message telling the admin what to do instead.'
        record_deletion(request, 'ResourceType', obj.id, label, result='failure', failure_reason=msg)
        return Response({'detail': msg}, status=status.HTTP_400_BAD_REQUEST)

    try:
        response = super().destroy(request, *args, **kwargs)
    except Exception as exc:
        record_deletion(request, 'ResourceType', obj.id, label, result='failure', failure_reason=str(exc)[:500])
        return Response({'detail': 'Deletion failed. No partial deletion should remain.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    record_deletion(request, 'ResourceType', obj.id, label, result='success')
    return response
```

### Adding a new guarded delete

1. Identify what would be silently destroyed by the existing `on_delete`
   behavior (`CASCADE` on a paying-student or financial record is the
   thing to look for — grep the model for `related_name` and check every
   reverse relation).
2. Write the guard: what dependency, if present, should block this?
3. Copy the `destroy()` shape above into the `ViewSet`.
4. Add tests mirroring the pattern in `academics/tests.py` /
   `courses/tests.py`: one test per guard (blocked case), one for the
   success path, one for permission enforcement.
5. If the resource owns files (an `ImageField` or a `MediaAsset` FK),
   clean them up inside the `try` block using `delete_file_field()` /
   `delete_media_asset()` before calling `super().destroy()`.

## Bugs found and fixed during this work

- **`Test.destroy()` PROTECT edge case** — the original guard only
  blocked deletion when a `Test` was the *sole* version under its
  `exam_template` and had sessions. A version with a session that wasn't
  the sole version slipped through and hit Django's `on_delete=PROTECT`
  on `ExamSession.exam_version` unhandled, surfacing as a raw `500`.
  Fixed by unconditionally checking `test.sessions.exists()`.
- **`Coupon.course` was `on_delete=CASCADE`** despite being nullable with
  help text describing "blank = applies across every course" — deleting a
  `Course` would have silently destroyed every coupon scoped to it.
  Changed to `SET_NULL` so a coupon survives its course being deleted and
  falls back to unscoped.

## Backups (separate from live deletion)

Cloud SQL automated backups are enabled with 7-day retention and binary
logging (point-in-time recovery), configured independently of this
deletion system — see the `hamromentor-app` Cloud SQL instance
configuration. This is the safety net for "someone permanently deleted
something they shouldn't have," distinct from the guards above, which
exist to stop that from happening in the first place.

## Explicitly out of scope (not built)

These were named in the original spec but deliberately deferred — do not
assume they exist:

- Guarded deletes for `Video`, `PaymentMethod`, `CourseLesson`,
  `Subject`/`Chapter`/`Topic`.
- A backend bulk-delete endpoint (the Admin bulk-delete UI for Questions
  calls the same single-item guarded endpoint per row via
  `Promise.allSettled`, so partial failures in a batch are reported
  individually rather than all-or-nothing).

## Tests

`academics/tests.py`, `courses/tests.py`, `marketplace/tests.py`,
`accounts/tests.py`, `tests_app/tests.py`, `media_library/tests.py`, and
`core/tests.py` — 36 tests total, covering every guard's blocked/success
paths, permission enforcement, the audit log's shape (actor/IP/result,
never content), and the `MediaAsset` dedup logic. Run with:

```bash
python manage.py test
```

## Admin UI

`Admin/src/components/ConfirmDeleteModal.js` replaces native `confirm()`
dialogs for every guarded delete. It shows a red warning banner, an
optional list of consequences, and — for high-risk or bulk deletes
(`requireTyped`) — gates the confirm button behind typing `DELETE`. Wired
into Questions (single + bulk), Courses, Course Packages, Marketplace
(TeacherCourse), and Admin Accounts. A backend `400` guard response is
surfaced as the modal's error text; the confirm button stays disabled
until the guard's condition is resolved or the admin cancels.
