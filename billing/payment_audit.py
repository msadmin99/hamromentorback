"""
Shared helper for recording payment state transitions to PaymentAuditLog.
Called from every branch of billing.payment_service — never write to
PaymentAuditLog directly, so the shape/behavior stays consistent. Mirrors
core/deletion_audit.py's record_deletion() convention.
"""
from core.request_utils import client_ip


def record_payment_event(purchase, action, previous_status, new_status, request=None, actor=None, reason='', metadata=None):
    from .models import PaymentAuditLog

    user = actor
    if user is None and request is not None:
        candidate = getattr(request, 'user', None)
        user = candidate if (candidate and candidate.is_authenticated) else None

    PaymentAuditLog.objects.create(
        purchase=purchase,
        action=action,
        previous_status=previous_status,
        new_status=new_status,
        actor=user,
        actor_email=getattr(user, 'email', '') or '',
        ip_address=client_ip(request) if request is not None else None,
        reason=(reason or '')[:500],
        metadata=metadata or {},
    )
