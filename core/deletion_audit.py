"""
Shared helper for recording permanent-delete attempts to DeletionAuditLog.
Called from every guarded destroy() across the app — never write to
DeletionAuditLog directly, so the shape/behavior stays consistent (and so
this is the one place that decides what counts as "the actor" or "the IP").
"""


def _client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def record_deletion(request, resource_type, resource_id, resource_label='', result='success', failure_reason=''):
    from .models import DeletionAuditLog

    user = getattr(request, 'user', None)
    authenticated = bool(user and user.is_authenticated)

    DeletionAuditLog.objects.create(
        actor=user if authenticated else None,
        actor_email=getattr(user, 'email', '') if authenticated else '',
        resource_type=resource_type,
        resource_id=str(resource_id),
        resource_label=(resource_label or '')[:255],
        result=result,
        failure_reason=(failure_reason or '')[:500],
        ip_address=_client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:300],
    )


def delete_file_field(file_field):
    """Safely deletes the underlying storage file for a Django
    ImageField/FileField value (works for FileSystemStorage or any
    django-storages backend). Never raises — a storage hiccup shouldn't
    block the model row from being deleted."""
    if not file_field:
        return
    try:
        file_field.delete(save=False)
    except Exception:
        pass
