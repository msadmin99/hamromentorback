from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import FreeStarterEntitlement
from .serializers import FreeStarterEntitlementSerializer


class MyEntitlementsView(APIView):
    """GET /api/entitlements/mine/ — the authenticated student's own Free
    Starter entitlements (Step 22 IDOR posture: always request.user, never
    a client-supplied id)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .provisioning import provision_free_starter

        # Lazy-provision fallback so a student who registered before Free
        # Starter existed (or before any FreeStarterPolicy was configured)
        # still gets provisioned against whatever policy is active now.
        provision_free_starter(request.user)
        rows = FreeStarterEntitlement.objects.filter(user=request.user)
        return Response(FreeStarterEntitlementSerializer(rows, many=True).data)


class TestAccessView(APIView):
    """GET /api/entitlements/tests/<test_id>/access/ — read-only CanStart
    decision for one Test. Never trusts a client-supplied user id; the
    resource lookup itself carries no authorization weight beyond "does
    this Test exist" — the actual decision is entirely a function of
    request.user."""

    permission_classes = [IsAuthenticated]

    def get(self, request, test_id):
        from tests_app.models import Test

        from .services import can_start_test

        test = get_object_or_404(Test, pk=test_id)
        decision = can_start_test(request.user, test)
        return Response(decision.as_dict())


class SubjectQbankAccessView(APIView):
    """GET /api/entitlements/subjects/<subject_id>/qbank-access/ —
    read-only CanView decision for QBank practice on one Subject."""

    permission_classes = [IsAuthenticated]

    def get(self, request, subject_id):
        from academics.models import Subject

        from .services import can_view_qbank

        subject = get_object_or_404(Subject, pk=subject_id)
        decision = can_view_qbank(request.user, subject)
        return Response(decision.as_dict())
