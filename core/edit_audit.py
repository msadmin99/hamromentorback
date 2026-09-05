"""
Shared helper for recording successful admin edits to AdminEditAuditLog.
Sibling of deletion_audit.record_deletion() — same actor/IP/user-agent
capture, written only from the specific admin-edit endpoint(s) that use it
(see accounts.views.AdminUserViewSet.student_edit, Phase 2), and only after
a save genuinely succeeds. Never called for a rejected/validation-failed
request, so a "no entry" is itself proof nothing was written.
"""
from .request_utils import client_ip


def record_admin_edit(request, resource_type, resource_id, changed_fields, resource_label=''):
    from .models import AdminEditAuditLog

    if not changed_fields:
        return None

    user = getattr(request, 'user', None)
    authenticated = bool(user and user.is_authenticated)

    return AdminEditAuditLog.objects.create(
        actor=user if authenticated else None,
        actor_email=getattr(user, 'email', '') if authenticated else '',
        resource_type=resource_type,
        resource_id=str(resource_id),
        resource_label=(resource_label or '')[:255],
        changed_fields=changed_fields,
        ip_address=client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:300],
    )
