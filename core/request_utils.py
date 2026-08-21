"""Small request-inspection helpers shared across audit-logging modules
(core.deletion_audit, billing.payment_audit) — kept here so both import one
implementation instead of duplicating it."""


def client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')
