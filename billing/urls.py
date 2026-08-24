from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AnalyticsView,
    ApplyCouponView,
    ComboPlanViewSet,
    ComboQuoteView,
    CouponViewSet,
    ExpireStalePaymentsView,
    GrantAccessView,
    MyCouponsView,
    MySubscriptionsView,
    PaymentMethodViewSet,
    PurchaseViewSet,
    ScholarshipViewSet,
    SendRenewalRemindersView,
    SubscriptionAutoRenewView,
    SubscriptionPlanViewSet,
)

router = DefaultRouter()
router.register('subscription-plans', SubscriptionPlanViewSet, basename='subscription-plan')
router.register('combo-plans', ComboPlanViewSet, basename='combo-plan')
router.register('payment-methods', PaymentMethodViewSet, basename='payment-method')
router.register('coupons', CouponViewSet, basename='coupon')
router.register('purchases', PurchaseViewSet, basename='purchase')
router.register('scholarships', ScholarshipViewSet, basename='scholarship')

urlpatterns = [
    path('coupons/apply/', ApplyCouponView.as_view(), name='apply-coupon'),
    path('coupons/mine/', MyCouponsView.as_view(), name='my-coupons'),
    path('combo-quote/', ComboQuoteView.as_view(), name='combo-quote'),
    path('my-subscriptions/', MySubscriptionsView.as_view(), name='my-subscriptions'),
    path('grant-access/', GrantAccessView.as_view(), name='grant-access'),
    path('analytics/', AnalyticsView.as_view(), name='billing-analytics'),
    path('subscriptions/<int:pk>/auto-renew/', SubscriptionAutoRenewView.as_view(), name='subscription-auto-renew'),
    path('cron/send-renewal-reminders/', SendRenewalRemindersView.as_view(), name='send-renewal-reminders'),
    path('cron/expire-stale-payments/', ExpireStalePaymentsView.as_view(), name='expire-stale-payments'),
] + router.urls
