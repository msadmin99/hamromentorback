"""Audit trail for `RolePermission` mutations.

`RolePermission.features` decides which dashboard feature keys an Admin or
Editor account may use, and since the P0 security fix it is enforced
server-side (`hamromentor.permissions.HasFeature` and
`accounts.models.user_feature_list` both read it). Editing that row
therefore changes what other staff accounts can do platform-wide —
exactly the class of change that has to be attributable. Until now no
mutation path recorded anything: the roadmap flagged it as outstanding
("Phase 1's RolePermission changes — still not audit-logged").

## No new audit model

This writes to the existing `core.models.AdminEditAuditLog` through the
existing `core.edit_audit.record_admin_edit()` helper, in the same
`{'field': {'old': ..., 'new': ...}}` shape that `AdminUserViewSet.
student_edit` (the reference implementation) already uses. Nothing new
was modelled — the existing log represents this change type without
alteration, so there is no migration.

## Two request-driven mutation paths, one representation

* `accounts.views.RolePermissionViewSet` — the admin panel's API.
  POST/PUT/PATCH only; DELETE is excluded by the viewset's own
  `http_method_names`, so no delete audit is invented for it.
* `accounts.admin.RolePermissionAdmin` — the Django admin site, which can
  add, change and delete (including the bulk delete action).

They are wholly separate stacks (DRF vs. django.contrib.admin), so a
single mutation can only ever traverse one of them — the record is
written once, by the path that actually performed it. Both call the
functions below so the two produce byte-identical record shapes.

The Django admin path is *additionally* covered by Django's own
`LogEntry`. That is left alone: `LogEntry` records that a change happened,
this records what the value was before and after.

## Actor attribution

The actor is always `request.user`, resolved by `record_admin_edit()` from
the authenticated server-side principal. No mutation path reads an actor,
username, or timestamp from the request body, so a caller cannot forge or
misattribute an entry.

## Known gap, stated rather than papered over

`core/management/commands/seed_data.py` bootstraps the two rows with
`get_or_create`. That runs without a request and therefore without an
authenticated principal, so it cannot produce an attributable record and
does not write one. It is a deployment action, not an admin action, and
it only ever creates rows that are absent — it never edits an existing
one.
"""
from core.edit_audit import record_admin_edit

# The whole model. `id` is deliberately excluded: it is not a meaningful
# permission change, and it appears as resource_id on the record anyway.
AUDITED_FIELDS = ('role', 'features')

RESOURCE_TYPE = 'RolePermission'


def snapshot(instance):
    """The audited state of a RolePermission row, or `{}` for "did not
    exist" (a create's before-state, a delete's after-state).

    `features` is copied with `list()` rather than referenced. The stored
    value is a JSONField list, and holding the live reference would let a
    later mutation of that same object edit what the audit record claims
    the old value was — the misleading-record case this helper exists to
    avoid.
    """
    if instance is None or instance.pk is None:
        return {}
    return {
        # Carried for identification only — not in AUDITED_FIELDS, so it can
        # never surface as a "change". Needed because Django sets pk to None
        # on the instance after a delete, and the record still has to name
        # which row went away.
        'id': instance.pk,
        'role': instance.role,
        'features': list(instance.features or []),
    }


def diff(before, after):
    """`{'field': {'old': ..., 'new': ...}}` for fields that actually
    changed — the same shape `record_admin_edit` already stores for
    student edits, so one audit reader handles both.

    A field absent from a snapshot reads as `None`, which is how a create
    (`before={}`) and a delete (`after={}`) express themselves without
    needing a separate action column the existing log does not have.
    """
    changed = {}
    for field in AUDITED_FIELDS:
        old = before.get(field)
        new = after.get(field)
        if old != new:
            changed[field] = {'old': old, 'new': new}
    return changed


def record_role_permission_change(request, *, before, after, instance=None):
    """Write the audit record for one RolePermission mutation.

    Returns the created log row, or None when nothing actually changed —
    a no-op PATCH that resubmits identical values is not a permission
    change and does not deserve an entry that would later read as one.

    Call this only after the mutation has genuinely succeeded, and inside
    the same transaction, so a rolled-back change cannot leave behind a
    record claiming it happened.
    """
    changed = diff(before, after)
    if not changed:
        return None

    # A delete has no surviving instance; fall back to the state we
    # captured before it went away so the record still names its subject.
    resource_id = getattr(instance, 'pk', None) or before.get('id') or 'deleted'
    label = (after.get('role') or before.get('role') or '')

    return record_admin_edit(
        request,
        resource_type=RESOURCE_TYPE,
        resource_id=resource_id,
        resource_label=label,
        changed_fields=changed,
    )
