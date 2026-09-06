"""Phase 10 — admin "preview as student".

`implementation-plan.md` Phase 10: *"Admin 'preview as student' simulation:
read-only, must not create real TestAttempt/Purchase/etc. rows — implement
as a request-scoped override of the Phase 4 capability functions' input
user context, not a real impersonation session."*

So this is deliberately **not** impersonation. There is no session switch,
no token, no `request.user` reassignment. All it does is answer "which user
should the catalog's *visibility and capability* questions be asked about
for this one GET" — every other part of the request stays the admin's.
Concretely that means:

* It only ever changes read-time resolution (`TestViewSet.get_queryset`'s
  visibility filter and the `access` block on each card).
* Nothing it touches can write. The one write that Phase 3 normally
  performs during an entitlement check — `has_free_starter_available()`
  lazily provisioning a missing `FreeStarterEntitlement` — is explicitly
  suppressed in preview mode (see `StudentEntitlementSnapshot`), so
  previewing a student cannot create rows against their account.
* Starting, answering, submitting, purchasing: unaffected. Those paths
  never consult this module, so an admin cannot consume a student's
  attempt or quota by previewing.

Access is restricted to admin-role staff — the same tier that already
governs the platform's other cross-student views. A non-admin passing
`?preview_as=` is ignored entirely (not an error: it is a UI affordance,
and silently rendering your own view is the safe failure).
"""


def resolve_preview_user(request):
    """The student whose eyes this GET should be rendered through, or None
    for the ordinary case. Returns None for anyone not entitled to preview,
    for a non-GET request, and for an unknown/staff target."""
    if request is None or request.method != 'GET':
        return None
    raw = request.query_params.get('preview_as') if hasattr(request, 'query_params') else None
    if not raw:
        return None

    actor = getattr(request, 'user', None)
    if not (actor and actor.is_authenticated and actor.is_staff):
        return None
    # Same tier as the platform's other cross-student surfaces: admin role
    # or above, never a plain editor/teacher account.
    if getattr(actor, 'admin_role', None) not in (None, '', 'admin', 'super_admin') and not actor.is_superuser:
        return None

    from django.contrib.auth import get_user_model

    try:
        target = get_user_model().objects.get(pk=int(raw))
    except (ValueError, TypeError, get_user_model().DoesNotExist):
        return None
    if target.is_staff:
        # Previewing another staff account isn't the feature and would just
        # show the staff bypass rather than a student's real experience.
        return None
    return target
