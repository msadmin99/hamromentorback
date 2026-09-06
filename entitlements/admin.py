from django.contrib import admin

from .models import EntitlementEventLog, FreeStarterEntitlement, FreeStarterPolicy


@admin.register(FreeStarterPolicy)
class FreeStarterPolicyAdmin(admin.ModelAdmin):
    """The actual, admin-editable Free Starter policy — no quantities are
    hardcoded in code; edit rows here to change what every newly-registered
    student receives for free."""

    list_display = ('resource_type', 'quantity', 'unlimited', 'validity_days', 'is_active', 'updated_at')
    list_editable = ('quantity', 'unlimited', 'validity_days', 'is_active')


@admin.register(FreeStarterEntitlement)
class FreeStarterEntitlementAdmin(admin.ModelAdmin):
    list_display = ('user', 'resource_type', 'quantity', 'used', 'unlimited', 'status', 'expires_at')
    list_filter = ('resource_type', 'status', 'unlimited')
    search_fields = ('user__email', 'user__username')
    raw_id_fields = ('user',)


@admin.register(EntitlementEventLog)
class EntitlementEventLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'user', 'event', 'resource_type', 'actor')
    list_filter = ('event', 'resource_type')
    search_fields = ('user__email', 'detail')
    raw_id_fields = ('user', 'actor')
    readonly_fields = [f.name for f in EntitlementEventLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
