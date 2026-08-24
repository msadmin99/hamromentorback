from django.contrib import admin

from .models import ComboPlan, Coupon, GrandTestAccess, PaymentMethod, Purchase, Subscription, SubscriptionPlan


@admin.register(PaymentMethod)
class PaymentMethodAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active', 'order')
    list_filter = ('is_active',)


@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    list_display = ('name', 'course', 'product_type', 'price', 'duration_value', 'duration_unit', 'is_active')
    list_filter = ('product_type', 'is_active')


@admin.register(ComboPlan)
class ComboPlanAdmin(admin.ModelAdmin):
    list_display = ('name', 'course', 'discount_percent', 'is_best_value', 'is_popular', 'is_active')
    list_filter = ('course', 'is_active')
    filter_horizontal = ('plans',)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ('user', 'course', 'product_type', 'expires_at', 'is_active')
    list_filter = ('product_type', 'is_active')


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = ('code', 'discount_type', 'discount_value', 'applies_to', 'usage_count', 'is_active')
    list_filter = ('discount_type', 'applies_to', 'is_active')


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    """Financial-integrity guard: an approved purchase is a real financial/
    access record and must never be hard-deleted, even by a Django-admin
    superuser — that's the one thing this ModelAdmin exists to prevent
    (everything else about it is default). Unapproved orders (unpaid,
    rejected, expired, cancelled) carry no financial weight and stay
    deletable for routine cleanup."""
    list_display = ('user', 'kind', 'final_amount', 'status', 'created_at')
    list_filter = ('kind', 'status')

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.status == 'approved':
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        # Django's built-in "Delete selected" bulk action runs a single
        # QuerySet.delete() over the whole selection with no per-object
        # has_delete_permission check — the only reliable way to keep an
        # approved purchase in a bulk selection from being deleted is to not
        # offer bulk delete on this model at all. Single-object delete above
        # still works correctly for unapproved (unpaid/rejected/expired/
        # cancelled) purchases via the confirmation page.
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions


@admin.register(GrandTestAccess)
class GrandTestAccessAdmin(admin.ModelAdmin):
    list_display = ('user', 'test', 'password', 'granted_at')
