# RolePermission Audit Logging

> Scope: add an audit trail to `RolePermission` mutations. Nothing else.
> Not a roadmap phase — this is the "Cross-cutting: Audit Log" item the
> plan flagged as outstanding: *"Phase 1's `RolePermission` changes (still
> not audit-logged — flagged as a later addition)."*

---

## 1. Why this needed auditing

`RolePermission.features` is a JSON list of dashboard feature keys, one
row per role (`admin`, `editor`). Since the P0 security fix it is
**enforced server-side** — both `accounts.models.user_feature_list()` and
`hamromentor.permissions.HasFeature` read it — so editing that row
changes what other staff accounts can actually do across the platform.

Before this change, **every mutation path recorded nothing**. There was
no way to answer "who granted the Editor role billing access, and when."

## 2. Audit findings

| Question | Answer (from code) |
|---|---|
| Who can modify it via the API? | `IsSuperAdmin` only — `is_staff` is deliberately not sufficient, and an Admin-role account cannot rewrite it |
| Which fields can change? | `role`, `features` (the serializer's whole field list besides `id`) |
| Is DELETE possible via the API? | **No** — excluded by `RolePermissionViewSet.http_method_names` |
| Is there a second mutation path? | **Yes** — `RolePermissionAdmin` in the Django admin site (add / change / delete / bulk delete) |
| Was any of it audited? | **No.** Zero records for this resource type |
| Is there an existing mechanism to reuse? | Yes — `core.models.AdminEditAuditLog` + `core.edit_audit.record_admin_edit()` |
| Does the existing log need schema changes? | **No** |

An existing ceiling check already lives in `RolePermissionSerializer.
validate()`: a feature outside `EDITOR_ALLOWED_FEATURES` is rejected at
write time for the `editor` row. That is unchanged.

## 3. Existing infrastructure reused — no new audit model

Records go to `AdminEditAuditLog` via `record_admin_edit()`, in the same
`{'field': {'old': ..., 'new': ...}}` shape that
`AdminUserViewSet.student_edit` (the reference implementation) already
uses. The existing log represents this change type without alteration:
actor, actor_email, resource_type, resource_id, resource_label,
changed_fields, ip_address, user_agent, created_at.

**Consequence: no migration.** `makemigrations --check` → *No changes
detected*.

## 4. Mutation paths audited

| Path | Operations | Audited |
|---|---|---|
| `RolePermissionViewSet` (DRF) | POST, PUT, PATCH | ✅ via `perform_create` / `perform_update` |
| `RolePermissionViewSet` | DELETE | n/a — not offered (405); no audit invented for it |
| `RolePermissionAdmin` (Django admin) | add / change | ✅ via `save_model` |
| `RolePermissionAdmin` | delete | ✅ via `delete_model` |
| `RolePermissionAdmin` | bulk delete action | ✅ via `delete_queryset` |
| `seed_data` management command | `get_or_create` bootstrap | ❌ — see §9 |

### One authoritative point per path, no duplication

DRF and `django.contrib.admin` are entirely separate stacks, so a single
mutation only ever traverses one of them. Within DRF, `create()` funnels
to `perform_create` and both `update()` and `partial_update()` funnel to
`perform_update` — one insertion point each, so PUT and PATCH cannot
double-log. **No model `save()` override and no `post_save` signal was
added**, deliberately: either would fire for every path *including* the
un-attributable management command, and would double up with the explicit
call sites.

The Django admin path is additionally covered by Django's own `LogEntry`.
That is left alone and is not duplicated work: `LogEntry` records *that*
an object changed; this records what the value was **before and after**,
which `LogEntry` does not capture for a JSONField.

## 5. Record format

```
resource_type  : "RolePermission"
resource_id    : the row's pk (captured before deletion, so a delete
                 still names its subject)
resource_label : the role name — "admin" or "editor"
changed_fields : {"features": {"old": [...], "new": [...]},
                  "role":     {"old": ..., "new": ...}}
```

Create and delete are expressed within the same shape rather than needing
an action column the existing log does not have: a create is
`{"old": null, "new": <value>}`, a delete is `{"old": <value>, "new":
null}`.

`id` is carried inside the internal snapshot for identification but is
excluded from `AUDITED_FIELDS`, so it can never surface as a "change".

**`features` is copied with `list()` when snapshotted.** Holding the live
JSONField reference would let a later mutation of the same object rewrite
what the record claims the old value was — the misleading-record case the
brief called out. Pinned by a test.

## 6. Transaction behavior

The audit write shares the mutation's `transaction.atomic()` block, so
the two cannot diverge in either direction:

- a rolled-back mutation leaves **no** record claiming it succeeded;
- a failed audit write rolls the mutation back rather than silently
  losing the trail.

This is **deliberately stricter than the `student_edit` precedent**,
which calls `record_admin_edit()` after its atomic block. That is
acceptable for a student profile edit; for the row that governs
platform-wide administrative capability, the audit trail falling behind
the change it is evidence of is the worse failure. Pinned by
`test_rollback_leaves_neither_the_change_nor_the_record`.

A no-op update (resubmitting identical values) writes **no** record — it
is not a permission change, and an entry for it would later read as one.

## 7. Actor attribution and forgery resistance

The actor is resolved by `record_admin_edit()` from `request.user`, the
authenticated server-side principal. No path reads an actor, username, or
timestamp from the request body. Tests assert that `actor`, `actor_id`,
`actor_email` and `created_at` in the payload are all ignored, and that a
**denied** caller cannot cause any entry to be written at all — the
record is a side effect of a successful mutation, never of a request.

## 8. Authorization, privacy, immutability

**Authorization is unchanged.** `IsSuperAdmin` still governs the API;
anonymous, student, teacher, editor and admin-role accounts are all
denied, asserted by test. Nothing was broadened, and `is_staff` was not
turned into `can_modify`.

**Privacy.** Only `role` and `features` are recorded — no credentials,
tokens, payment data, or unrelated PII. The actor's own email is stored
by the existing log's established convention (it is the accountability
record's subject).

**Immutability.** `AdminEditAuditLog` has no serializer, no route, and no
Django-admin registration, so there is no surface through which an entry
can be edited or deleted. This is immutability by absence of a write
path, not by a permission check — and a test asserts that absence, so
adding such a surface later fails this test first. No update/delete
behavior was added to support this feature.

## 9. Performance — measured, not assumed

An audited `PATCH /api/role-permissions/{id}/` costs **5 queries**; the
same request with the audit write stubbed out costs **4**. So the audit
adds exactly **+1 query — one INSERT — and the cost is constant**, not
proportional to anything.

The Django-admin `save_model` path adds **+2**: one SELECT to re-read the
stored row for the before-state (necessary because `obj` already carries
the form's new values by then, so snapshotting it would record
new-vs-new and show no change), plus the INSERT.

`delete_queryset` snapshots the already-fetched queryset in Python and
writes one INSERT per row. `RolePermission.role` is `unique` with two
choices, so that loop is bounded at two rows by the schema.

No existing query-count baseline elsewhere in the suite changed.

## 10. Known limitations — stated, not papered over

1. **`seed_data.py` bootstrap is not audited.** It runs without a request
   and therefore without an authenticated principal, so it cannot produce
   an attributable record. It only ever `get_or_create`s rows that are
   absent — it never edits an existing one. Auditing it would require
   inventing a synthetic actor, which would make the trail less
   trustworthy, not more.
2. **Direct database or `manage.py shell` writes are not audited.** No
   application-level mechanism can catch those; that is a DB-access
   control question, not an application one.
3. **This audits `RolePermission` only.** The platform is *not* now
   comprehensively audited. `DeletionAuditLog`, `AdminEditAuditLog` and
   `PaymentAuditLog` still cover only the specific actions that
   explicitly call them.
4. **No read/reporting surface was added.** Entries are queryable from
   the database or the Django shell. Building an audit-log viewer is a
   separate piece of work.

## 11. Deployment readiness

Code-ready, not deployed. No migration, no schema change, no API contract
change, no data backfill. Production database untouched; nothing
deployed.
