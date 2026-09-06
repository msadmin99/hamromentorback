from rest_framework import serializers

from .models import FreeStarterEntitlement


class FreeStarterEntitlementSerializer(serializers.ModelSerializer):
    remaining = serializers.SerializerMethodField()
    effective_status = serializers.SerializerMethodField()

    class Meta:
        model = FreeStarterEntitlement
        fields = [
            'id', 'resource_type', 'quantity', 'unlimited', 'used', 'remaining',
            'valid_from', 'expires_at', 'status', 'effective_status',
        ]

    def get_remaining(self, obj):
        return obj.remaining

    def get_effective_status(self, obj):
        return obj.effective_status
