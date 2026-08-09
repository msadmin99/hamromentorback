from decimal import Decimal

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import F, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from hamromentor.permissions import IsAdminRoleOrAbove, IsAdminRoleOrAboveOrReadOnly
from tests_app.models import Test

from .analytics import build_analytics
from .models import (
    Coupon,
    GrandTestAccess,
    NotificationLog,
    PaymentMethod,
    Purchase,
    Scholarship,
    Subscription,
    SubscriptionPlan,
)
from .notifications import send_notification
from .serializers import (
    ApplyCouponSerializer,
    CouponSerializer,
    CreatePurchaseSerializer,
    PaymentMethodSerializer,
    PurchaseSerializer,
    ScholarshipSerializer,
    SubmitPaymentSerializer,
    SubscriptionPlanSerializer,
    SubscriptionSerializer,
)


def _resolve_amount(kind, plan, grand_test):
    if kind == 'subscription':
        return plan.price, plan.product_type
    return (grand_test.price or Decimal('0')), 'grand_test'


def _coupon_usable(coupon, user, product_type, plan, grand_test, amount):
    """Shared eligibility checks for both a manually-typed code and an auto-apply candidate."""
    if not coupon.is_valid_now():
        return False
    if not coupon.applies_to_product(product_type):
        return False
    if not coupon.applies_to_course(plan, grand_test):
        return False
    if not coupon.is_eligible_for(user):
        return False
    if coupon.min_purchase_amount and amount < coupon.min_purchase_amount:
        return False
    uses_by_user = Purchase.objects.filter(user=user, coupon=coupon, status__in=Purchase.OPEN_STATUSES).count()
    return uses_by_user < coupon.max_uses_per_user


def find_auto_apply_coupon(user, product_type, plan, grand_test, amount):
    """Best-value currently-valid, eligible, auto-apply coupon for this purchase, if any —
    used when the student hasn't typed a code, so a site-wide promotion still applies."""
    best, best_discount = None, Decimal('0')
    for coupon in Coupon.objects.filter(auto_apply=True, is_active=True):
        if not _coupon_usable(coupon, user, product_type, plan, grand_test, amount):
            continue
        discount = coupon.compute_discount(amount)
        if discount > best_discount:
            best, best_discount = coupon, discount
    return best


def compute_price(kind, plan, grand_test, coupon_code, user):
    """Server-authoritative price computation — never trust a client-sent discount.

    Tries, in order: a manually-typed coupon code; the best matching auto-apply
    coupon; a one-time referral "friend" discount on a referred student's first
    purchase. At most one of these ever applies — no stacking."""
    amount, product_type = _resolve_amount(kind, plan, grand_test)
    coupon = None
    discount = Decimal('0')
    error = None

    if coupon_code and coupon_code.strip():
        coupon = Coupon.objects.filter(code=coupon_code.strip().upper()).first()
        if not coupon:
            error = 'Invalid coupon code.'
        elif not coupon.is_valid_now():
            error = 'This coupon is not currently active.'
        elif not coupon.applies_to_product(product_type):
            error = 'This coupon does not apply to this product.'
        elif not coupon.applies_to_course(plan, grand_test):
            error = 'This coupon does not apply to your course.'
        elif not coupon.is_eligible_for(user):
            error = 'You are not eligible for this coupon.'
        elif coupon.min_purchase_amount and amount < coupon.min_purchase_amount:
            error = f'This coupon requires a minimum purchase of Rs. {coupon.min_purchase_amount}.'
        else:
            uses_by_user = Purchase.objects.filter(
                user=user, coupon=coupon, status__in=Purchase.OPEN_STATUSES,
            ).count()
            if uses_by_user >= coupon.max_uses_per_user:
                error = 'You have already used this coupon.'
            else:
                discount = coupon.compute_discount(amount)
    else:
        coupon = find_auto_apply_coupon(user, product_type, plan, grand_test, amount)
        if coupon:
            discount = coupon.compute_discount(amount)
        elif (
            user and user.is_authenticated and user.referred_by_id
            and not Purchase.objects.filter(user=user, status__in=Purchase.OPEN_STATUSES).exists()
        ):
            discount = amount * Decimal(settings.REFERRAL_FRIEND_DISCOUNT_PERCENT) / 100

    if error:
        coupon = None
        discount = Decimal('0')
    return amount, discount, amount - discount, coupon, error


def _maybe_reward_referrer(purchase):
    """First time a referred student's purchase is approved, credit the referrer's wallet."""
    user = purchase.user
    if not user.referred_by_id:
        return
    prior_approved = Purchase.objects.filter(user=user, status='approved').exclude(pk=purchase.pk).exists()
    if prior_approved:
        return
    referrer = user.referred_by
    referrer.wallet_balance = referrer.wallet_balance + Decimal(settings.REFERRAL_REWARD_AMOUNT)
    referrer.save(update_fields=['wallet_balance'])


def _send_grand_test_email(access):
    test = access.test
    lines = [
        f'Hi {access.user.first_name or access.user.email},',
        '',
        'Your payment has been confirmed. Here are your exam details:',
        '',
        f'Grand Test: {test.title}',
    ]
    if test.scheduled_start:
        lines.append(f'Exam date/time: {test.scheduled_start.strftime("%d %b %Y, %I:%M %p")}')
    lines += [
        f'Duration: {test.duration_minutes} minutes',
        '',
        f'Your unique password: {access.password}',
        '',
        'Keep this password private — it is unique to you and required to start the exam.',
        '',
        'Good luck!',
        'Dr. Gutka Support',
    ]
    try:
        send_mail(
            f'Your Grand Test access — {test.title}', '\n'.join(lines),
            settings.DEFAULT_FROM_EMAIL, [access.user.email], fail_silently=True,
        )
    finally:
        access.email_sent_at = timezone.now()
        access.save(update_fields=['email_sent_at'])


def _extend_or_create_subscription(user, course, product_type, duration, plan=None, mock_test_quota=None):
    """Shared by purchase activation and the admin manual-grant/scholarship
    endpoint — extends an existing active subscription for the same
    user+course+product rather than creating a duplicate row, exactly like a
    real renewal. Returns (subscription, was_renewal)."""
    now = timezone.now()
    existing = Subscription.objects.filter(
        user=user, course=course, product_type=product_type, is_active=True,
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gte=now)).first()

    start_from = existing.expires_at if (existing and existing.expires_at and existing.expires_at > now) else now
    expires_at = start_from + duration if duration else None

    if existing:
        existing.expires_at = expires_at
        if plan:
            existing.plan = plan
        if mock_test_quota is not None:
            existing.mock_test_quota = (existing.mock_test_quota or 0) + mock_test_quota
        existing.save()
        return existing, True

    subscription = Subscription.objects.create(
        user=user, plan=plan, course=course, product_type=product_type,
        expires_at=expires_at, mock_test_quota=mock_test_quota,
    )
    return subscription, False


def _activate_purchase(purchase):
    """Returns (subscription_or_access, was_renewal) — was_renewal is always
    False for a grand_test purchase (GrandTestAccess has no renewal concept)."""
    if purchase.kind == 'subscription':
        plan = purchase.plan
        subscription, was_renewal = _extend_or_create_subscription(
            purchase.user, plan.course, plan.product_type, plan.duration_timedelta(),
            plan=plan, mock_test_quota=plan.mock_test_quota,
        )
        if was_renewal:
            send_notification(purchase.user, 'renewal_confirmation', subscription)
        return subscription, was_renewal

    access, _created = GrandTestAccess.objects.get_or_create(
        user=purchase.user, test=purchase.grand_test, defaults={'purchase': purchase},
    )
    access.purchase = purchase
    access.granted_at = timezone.now()
    access.save()
    _send_grand_test_email(access)
    return access, False


class GrantAccessView(APIView):
    """Admin-only manual grant — creates a Subscription directly with no
    Purchase row (zero revenue), reusing the same extend-or-create logic real
    purchases use. Optionally tagged as a Scholarship for tracking/reporting."""
    permission_classes = [IsAdminRoleOrAbove]

    def post(self, request):
        from courses.models import Course

        from accounts.models import User

        user = get_object_or_404(User, pk=request.data.get('user_id'))
        course = get_object_or_404(Course, pk=request.data.get('course_id'))
        product_type = request.data.get('product_type')
        if product_type not in dict(SubscriptionPlan.PRODUCT_CHOICES):
            return Response({'detail': 'Invalid product_type.'}, status=400)

        plan_id = request.data.get('plan_id')
        plan = get_object_or_404(SubscriptionPlan, pk=plan_id) if plan_id else None

        duration_value = int(request.data.get('duration_value') or 1)
        duration_unit = request.data.get('duration_unit') or 'month'
        duration = SubscriptionPlan(duration_value=duration_value, duration_unit=duration_unit).duration_timedelta()

        mock_test_quota = request.data.get('mock_test_quota')
        mock_test_quota = int(mock_test_quota) if mock_test_quota not in (None, '') else (plan.mock_test_quota if plan else None)

        subscription, _was_renewal = _extend_or_create_subscription(
            user, course, product_type, duration, plan=plan, mock_test_quota=mock_test_quota,
        )

        response_data = {'subscription': SubscriptionSerializer(subscription).data}
        if request.data.get('is_scholarship'):
            scholarship = Scholarship.objects.create(
                user=user, course=course, product_type=product_type, plan=plan, subscription=subscription,
                reason=request.data.get('reason', ''), granted_by=request.user,
            )
            response_data['scholarship_id'] = scholarship.id
        return Response(response_data, status=status.HTTP_201_CREATED)


class AnalyticsView(APIView):
    """Admin-only revenue/growth dashboard — see billing/analytics.py for the
    exact definition and caveats behind every figure."""
    permission_classes = [IsAdminRoleOrAbove]

    def get(self, request):
        period_days = int(request.query_params.get('days') or 30)
        return Response(build_analytics(period_days=period_days))


class ScholarshipViewSet(viewsets.ModelViewSet):
    """Admin-only listing of scholarship grants. Creation goes through
    GrantAccessView (which also creates the underlying Subscription) — this
    viewset's create() is disabled so a Scholarship row is never made
    without a matching Subscription."""
    queryset = Scholarship.objects.select_related('user', 'course', 'plan', 'granted_by', 'subscription').all()
    serializer_class = ScholarshipSerializer
    permission_classes = [IsAdminRoleOrAbove]
    http_method_names = ['get', 'post', 'head', 'options']

    def create(self, request, *args, **kwargs):
        return Response(
            {'detail': 'Use /api/grant-access/ to create a scholarship (it also creates the Subscription).'},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    @action(detail=True, methods=['post'])
    def revoke(self, request, pk=None):
        scholarship = self.get_object()
        scholarship.is_active = False
        scholarship.save(update_fields=['is_active'])
        if scholarship.subscription:
            scholarship.subscription.is_active = False
            scholarship.subscription.save(update_fields=['is_active'])
        return Response(ScholarshipSerializer(scholarship).data)


class SubscriptionPlanViewSet(viewsets.ModelViewSet):
    queryset = SubscriptionPlan.objects.select_related('course').all()
    serializer_class = SubscriptionPlanSerializer
    permission_classes = [IsAdminRoleOrAboveOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        course_id = self.request.query_params.get('course')
        product_type = self.request.query_params.get('product_type')
        if course_id:
            qs = qs.filter(course_id=course_id)
        if product_type:
            qs = qs.filter(product_type=product_type)
        user = self.request.user
        if not (user.is_authenticated and user.is_staff):
            qs = qs.filter(is_active=True)
        return qs


class PaymentMethodViewSet(viewsets.ModelViewSet):
    queryset = PaymentMethod.objects.all()
    serializer_class = PaymentMethodSerializer
    permission_classes = [IsAdminRoleOrAboveOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if not (user.is_authenticated and user.is_staff):
            qs = qs.filter(is_active=True)
        return qs


class CouponViewSet(viewsets.ModelViewSet):
    queryset = Coupon.objects.all()
    serializer_class = CouponSerializer
    permission_classes = [IsAdminRoleOrAbove]


class ApplyCouponView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ApplyCouponSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        plan = get_object_or_404(SubscriptionPlan, pk=data['plan_id']) if data.get('plan_id') else None
        grand_test = get_object_or_404(Test, pk=data['grand_test_id']) if data.get('grand_test_id') else None
        if data['kind'] == 'subscription' and not plan:
            return Response({'detail': 'plan_id is required.'}, status=400)
        if data['kind'] == 'grand_test' and not grand_test:
            return Response({'detail': 'grand_test_id is required.'}, status=400)

        amount, discount, final, coupon, error = compute_price(
            data['kind'], plan, grand_test, data.get('code', ''), request.user,
        )
        if error:
            return Response({'detail': error, 'valid': False}, status=400)
        discount_source = None
        if coupon:
            discount_source = 'coupon'
        elif discount > 0:
            discount_source = 'referral'
        return Response({
            'valid': True, 'original_amount': amount, 'discount_amount': discount, 'final_amount': final,
            'coupon_code': coupon.code if coupon else None, 'discount_source': discount_source,
        })


class MyCouponsView(APIView):
    """Student dashboard: which promo codes they can currently use, which they've
    already redeemed with how much they saved, and a running savings total."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        available = []
        for coupon in Coupon.objects.filter(is_active=True):
            if not coupon.is_valid_now() or not coupon.is_eligible_for(user):
                continue
            if coupon.course_id is not None and coupon.course_id != user.active_course_id:
                continue
            uses_by_user = Purchase.objects.filter(
                user=user, coupon=coupon, status__in=Purchase.OPEN_STATUSES,
            ).count()
            if uses_by_user >= coupon.max_uses_per_user:
                continue
            available.append(coupon)

        used_purchases = (
            Purchase.objects.filter(user=user, coupon__isnull=False, status='approved')
            .select_related('coupon', 'plan', 'grand_test')
        )
        all_savings = Purchase.objects.filter(user=user, status='approved', discount_amount__gt=0)
        total_savings = sum((p.discount_amount for p in all_savings), Decimal('0'))

        return Response({
            'available': CouponSerializer(available, many=True).data,
            'used': [
                {
                    'id': p.id,
                    'coupon_code': p.coupon.code,
                    'item': p.plan.name if p.kind == 'subscription' else (p.grand_test.title if p.grand_test else ''),
                    'savings': p.discount_amount,
                    'used_at': p.decided_at or p.created_at,
                }
                for p in used_purchases
            ],
            'total_savings': total_savings,
        })


class PurchaseViewSet(viewsets.ModelViewSet):
    queryset = Purchase.objects.select_related('user', 'plan', 'grand_test', 'coupon', 'grand_test_access').all()
    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'post', 'head', 'options']

    def get_serializer_class(self):
        if self.action == 'create':
            return CreatePurchaseSerializer
        return PurchaseSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if not (user.is_authenticated and user.is_staff):
            return qs.filter(user=user)
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = CreatePurchaseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        plan = get_object_or_404(SubscriptionPlan, pk=data['plan_id']) if data.get('plan_id') else None
        grand_test = get_object_or_404(Test, pk=data['grand_test_id']) if data.get('grand_test_id') else None
        if data['kind'] == 'subscription' and not plan:
            return Response({'detail': 'plan_id is required.'}, status=400)
        if data['kind'] == 'grand_test' and not grand_test:
            return Response({'detail': 'grand_test_id is required.'}, status=400)
        if data['kind'] == 'grand_test' and GrandTestAccess.objects.filter(user=request.user, test=grand_test).exists():
            return Response({'detail': 'You already have access to this Grand Test.'}, status=400)

        amount, discount, final, coupon, error = compute_price(
            data['kind'], plan, grand_test, data.get('coupon_code', ''), request.user,
        )
        if error:
            return Response({'detail': error}, status=400)

        purchase = Purchase.objects.create(
            user=request.user, kind=data['kind'], plan=plan, grand_test=grand_test, coupon=coupon,
            original_amount=amount, discount_amount=discount, final_amount=final,
        )

        if final <= 0:
            # Nothing to verify — a 100%-off / free-grant coupon needs no manual bank-transfer
            # check, so activate immediately instead of leaving it stuck in "pending".
            _activate_purchase(purchase)
            purchase.status = 'approved'
            purchase.decided_at = timezone.now()
            purchase.save()
            if purchase.coupon:
                Coupon.objects.filter(pk=purchase.coupon_id).update(usage_count=F('usage_count') + 1)
            _maybe_reward_referrer(purchase)

        return Response(PurchaseSerializer(purchase).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated], url_path='submit-payment')
    def submit_payment(self, request, pk=None):
        """Student submits (or resubmits) proof of payment — the same endpoint
        serves both a first-time submission (status='unpaid') and a resubmission
        after the admin requested new proof (status='resubmission_requested'),
        since the form and transition are identical either way."""
        purchase = self.get_object()
        if purchase.status not in ('unpaid', 'resubmission_requested'):
            return Response({'detail': 'This purchase is not awaiting payment submission.'}, status=400)
        serializer = SubmitPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        purchase.payment_method = data['payment_method']
        purchase.payment_reference = data['payment_reference']
        purchase.payment_screenshot = data['payment_screenshot']
        purchase.status = 'pending'
        purchase.admin_note = ''
        purchase.save()
        return Response(PurchaseSerializer(purchase).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAdminUser])
    def approve(self, request, pk=None):
        purchase = self.get_object()
        if purchase.status != 'pending':
            return Response({'detail': 'This purchase has already been decided.'}, status=400)
        _activate_purchase(purchase)
        purchase.status = 'approved'
        purchase.decided_at = timezone.now()
        purchase.decided_by = request.user
        purchase.save()
        if purchase.coupon:
            Coupon.objects.filter(pk=purchase.coupon_id).update(usage_count=F('usage_count') + 1)
        _maybe_reward_referrer(purchase)
        return Response(PurchaseSerializer(purchase).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAdminUser])
    def reject(self, request, pk=None):
        purchase = self.get_object()
        if purchase.status not in ('pending', 'resubmission_requested'):
            return Response({'detail': 'This purchase has already been decided.'}, status=400)
        note = (request.data.get('admin_note') or '').strip()
        if not note:
            return Response({'detail': 'A reason is required to reject a purchase.'}, status=400)
        purchase.status = 'rejected'
        purchase.admin_note = note
        purchase.decided_at = timezone.now()
        purchase.decided_by = request.user
        purchase.save()
        return Response(PurchaseSerializer(purchase).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAdminUser], url_path='request-resubmission')
    def request_resubmission(self, request, pk=None):
        purchase = self.get_object()
        if purchase.status != 'pending':
            return Response({'detail': 'This purchase is not awaiting verification.'}, status=400)
        note = (request.data.get('admin_note') or '').strip()
        if not note:
            return Response({'detail': 'A reason is required when requesting new proof.'}, status=400)
        purchase.status = 'resubmission_requested'
        purchase.admin_note = note
        purchase.save()
        return Response(PurchaseSerializer(purchase).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated], url_path='email-invoice')
    def email_invoice(self, request, pk=None):
        """Emails the same line-item breakdown the printable /invoice/{id} page
        shows — plain text, reusing the send_mail pattern already used for
        grand-test-access emails. No PDF generation (no library in this stack;
        the printable page already covers view/print/save-as-PDF)."""
        purchase = self.get_object()
        item_label = purchase.grand_test.title if purchase.kind == 'grand_test' else (purchase.plan.name if purchase.plan else 'Purchase')
        lines = [
            f'Invoice #{purchase.id}',
            f'Date: {purchase.created_at.strftime("%d %b %Y")}',
            '',
            f'Item: {item_label}',
            f'Original amount: Rs. {purchase.original_amount}',
        ]
        if purchase.discount_amount and purchase.discount_amount > 0:
            coupon_note = f' ({purchase.coupon.code})' if purchase.coupon else ''
            lines.append(f'Discount{coupon_note}: - Rs. {purchase.discount_amount}')
        lines += [
            f'Total paid: Rs. {purchase.final_amount}',
            f'Status: {purchase.get_status_display()}',
            '',
            f'View/print this invoice: {settings.FRONTEND_URL}/invoice/{purchase.id}',
            '',
            'Dr. Gutka Support',
        ]
        if not purchase.user.email:
            return Response({'detail': 'No email address on file for this account.'}, status=400)
        send_mail(
            f'Your invoice #{purchase.id} — Dr. Gutka', '\n'.join(lines),
            settings.DEFAULT_FROM_EMAIL, [purchase.user.email], fail_silently=False,
        )
        return Response({'sent': True})


class MySubscriptionsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .serializers import GrandTestAccessSerializer

        subs = Subscription.objects.filter(user=request.user).select_related('course', 'plan')
        accesses = GrandTestAccess.objects.filter(user=request.user).select_related('test')
        purchases = Purchase.objects.filter(user=request.user).select_related('plan', 'grand_test', 'coupon')[:20]
        return Response({
            'subscriptions': SubscriptionSerializer(subs, many=True).data,
            'grand_test_access': GrandTestAccessSerializer(accesses, many=True).data,
            'purchases': PurchaseSerializer(purchases, many=True).data,
        })


class SubscriptionAutoRenewView(APIView):
    """PATCH /subscriptions/{id}/auto-renew/ — the subscription's own user only.
    Toggling this doesn't charge anything (no payment gateway exists) — it just
    makes the renewal-reminder job flag this subscription so the student gets
    a clearer nudge; they still confirm and submit payment like any purchase."""
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk=None):
        subscription = get_object_or_404(Subscription, pk=pk, user=request.user)
        subscription.auto_renew = bool(request.data.get('auto_renew'))
        subscription.save(update_fields=['auto_renew'])
        return Response(SubscriptionSerializer(subscription).data)


# Reminder schedule: notification_type -> days relative to expiry (positive = before,
# 0 = on expiry, negative = days into the grace period after expiry).
REMINDER_SCHEDULE = {
    'reminder_30': 30, 'reminder_15': 15, 'reminder_7': 7, 'reminder_3': 3, 'reminder_1': 1,
    'expiry': 0, 'grace_period': -3,
}


def _check_cron_secret(request):
    provided = request.headers.get('X-Cron-Secret') or request.query_params.get('secret')
    return provided == settings.CRON_SECRET


class SendRenewalRemindersView(APIView):
    """POST /api/cron/send-renewal-reminders/ — meant to be hit by an external
    scheduler, same shared-secret pattern as courses.views.PruneExpiredPackagesView."""
    permission_classes = [AllowAny]

    def post(self, request):
        if not _check_cron_secret(request):
            return Response({'detail': 'Invalid or missing cron secret.'}, status=401)

        today = timezone.localdate()
        counts = {'checked': 0, 'sent': 0, 'skipped': 0, 'failed': 0}
        subs = Subscription.objects.filter(is_active=True, expires_at__isnull=False).select_related('user', 'course')
        for sub in subs:
            counts['checked'] += 1
            days_left = (sub.expires_at.date() - today).days
            notification_type = next((t for t, d in REMINDER_SCHEDULE.items() if d == days_left), None)
            if not notification_type:
                continue
            if NotificationLog.objects.filter(subscription=sub, notification_type=notification_type).exists():
                continue
            logs = send_notification(sub.user, notification_type, sub)
            for log in logs:
                counts[log.status] = counts.get(log.status, 0) + 1

        return Response(counts)
