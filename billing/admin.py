from django.contrib import admin

from .models import Coupon, GrandTestAccess, PaymentMethod, Purchase, Subscription, SubscriptionPlan


@admin.register(PaymentMethod)
class PaymentMethodAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active', 'order')
    list_filter = ('is_active',)


@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    list_display = ('name', 'course', 'product_type', 'price', 'duration_value', 'duration_unit', 'is_active')
    list_filter = ('product_type', 'is_active')


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
    list_display = ('user', 'kind', 'final_amount', 'status', 'created_at')
    list_filter = ('kind', 'status')


@admin.register(GrandTestAccess)
class GrandTestAccessAdmin(admin.ModelAdmin):
    list_display = ('user', 'test', 'password', 'granted_at')
