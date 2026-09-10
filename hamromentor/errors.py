"""Phase 2B-1 (Backend/API Stabilization) — shared, additive error-response
helper.

Introduces a stable, machine-readable `code` alongside the existing
`detail` string every current Website/Admin caller already reads (see
Frontend/src/lib/api.js: apiFetch, which reads `data.detail` /
`data.non_field_errors[0]` on a non-2xx response) — so a future Android/
iOS client can branch on `code` without any existing consumer needing to
change. `message` duplicates `detail` under the key name
PHASE_2A_API_VERSIONING_DESIGN.md §18 settled on as the long-term target,
so a mobile client is never forced to read the legacy key.

This is deliberately NOT a global DRF EXCEPTION_HANDLER — Phase 2A §12
recommends adopting the target shape incrementally, endpoint group by
endpoint group, not as a single big-bang rewrite (see the scope notes in
docs/PHASE_2B_1_ERROR_CONTRACT.md). Call error_response() exactly where a
view previously called Response({'detail': ...}, status=...); nothing
else about the view's behavior changes.
"""
from rest_framework.response import Response


def error_response(status_code, code, message, **extra):
    """Build the standardized error body: {code, message, detail, **extra}.

    `detail` always equals `message`, so any existing caller reading
    `error.data.detail` keeps working unchanged. `extra` preserves any
    additional key a call site already returned (e.g. `access_denied`)
    so migrating a call site to this helper never drops a field a caller
    might already depend on.
    """
    body = {'code': code, 'message': message, 'detail': message}
    body.update(extra)
    return Response(body, status=status_code)
