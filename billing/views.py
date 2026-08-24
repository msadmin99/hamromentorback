from decimal import Decimal

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from hamromentor.permissions import IsAdminRoleOrAbove, IsAdminRoleOrAboveOrReadOnly
from tests_app.models import Test

from . import payment_service
from .analytics import build_analytics
from .models import (
    COMBO_DISCOUNT_TIERS,
    ComboPlan,
    Coupon,
    GrandTestAccess,
    NotificationLog,
    PaymentMethod,
    Purchase,
    PurchaseComboItem,
    Scholarship,
    Subscription,
    SubscriptionPlan,
)
from .notifications import send_notification
from .payment_providers import ManualQRProvider
from .payment_service import PaymentError, _extend_or_create_subscription
from .screenshot_storage import screenshot_view_url
from .serializers import (
    ApplyCouponSerializer,
    ComboPlanSerializer,
    CouponSerializer,
    CreatePurchaseSerializer,
    PaymentAuditLogSerializer,
    PaymentMethodSerializer,
    PurchaseSerializer,
    ScholarshipSerializer,
    SubmitPaymentSerializer,
    SubscriptionPlanSerializer,
    SubscriptionSerializer,
)


class SubmitPaymentThrottle(UserRateThrottle):
    # DRF's SimpleRateThrottle reads the rate from settings.REST_FRAMEWORK's
    # DEFAULT_THROTTLE_RATES[scope], not a class attribute — overriding
    # get_rate() keeps this self-contained without touching global settings
    # (same pattern as academics/import_views.py::ImportUploadThrottle).
    scope = 'submit_payment'

    def get_rate(self):
        return '10/hour'


def _resolve_amount(kind, plan, grand_test, teacher_course=None):
    if kind == 'subscription':
        return plan.price, plan.product_type
    if kind == 'teacher_course':
        amount = Decimal('0') if teacher_course.is_free else teacher_course.price
        return amount, 'teacher_course'
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


def compute_price(kind, plan, grand_test, coupon_code, user, teacher_course=None):
    """Server-authoritative price computation — never trust a client-sent discount.

    Tries, in order: a manually-typed coupon code; the best matching auto-apply
    coupon; a one-time referral "friend" discount on a referred student's first
    purchase. At most one of these ever applies — no stacking."""
    amount, product_type = _resolve_amount(kind, plan, grand_test, teacher_course=teacher_course)
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


def _validate_and_price_combo(plans):
    """Shared by ComboQuoteView (preview) and PurchaseViewSet.create()'s
    custom "build your own" combo branch (actual purchase), so the two never
    price the same selection differently. `plans` is a list of already-
    fetched SubscriptionPlan objects. Returns
    (individual_value, discount_percent, you_save, final_price) or raises
    PaymentError with a user-facing message."""
    if len(plans) < 2:
        raise PaymentError('Select at least 2 products to build a combo.')
    course_ids = {p.course_id for p in plans}
    if len(course_ids) > 1:
        raise PaymentError('All combo items must belong to the same course.')
    product_types = [p.product_type for p in plans]
    if len(set(product_types)) != len(product_types):
        raise PaymentError('A combo can only include one plan per product type.')
    if not all(p.is_active for p in plans):
        raise PaymentError('One or more selected plans are no longer available.')

    discount_percent = COMBO_DISCOUNT_TIERS.get(len(plans), 0)
    individual_value = sum((p.price for p in plans), Decimal('0'))
    you_save = individual_value * discount_percent / 100
    final_price = individual_value - you_save
    return individual_value, discount_percent, you_save, final_price


def _price_predefined_combo(combo_plan):
    """Predefined ComboPlan pricing uses the admin-set discount_percent
    directly (not the "build your own" tier table) — the plans M2M was
    already validated one-per-product-type at ComboPlan save time
    (ComboPlanSerializer.validate_plans)."""
    plans = list(combo_plan.plans.all())
    if not plans:
        raise PaymentError('This combo has no plans configured.')
    individual_value = sum((p.price for p in plans), Decimal('0'))
    you_save = individual_value * combo_plan.discount_percent / 100
    final_price = individual_value - you_save
    return plans, individual_value, you_save, final_price


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


class ComboPlanViewSet(viewsets.ModelViewSet):
    queryset = ComboPlan.objects.select_related('course').prefetch_related('plans').all()
    serializer_class = ComboPlanSerializer
    permission_classes = [IsAdminRoleOrAboveOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        course_id = self.request.query_params.get('course')
        if course_id:
            qs = qs.filter(course_id=course_id)
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
        teacher_course = None
        if data.get('teacher_course_id'):
            from marketplace.models import TeacherCourse

            teacher_course = get_object_or_404(TeacherCourse, pk=data['teacher_course_id'])
        if data['kind'] == 'subscription' and not plan:
            return Response({'detail': 'plan_id is required.'}, status=400)
        if data['kind'] == 'grand_test' and not grand_test:
            return Response({'detail': 'grand_test_id is required.'}, status=400)
        if data['kind'] == 'teacher_course' and not teacher_course:
            return Response({'detail': 'teacher_course_id is required.'}, status=400)

        amount, discount, final, coupon, error = compute_price(
            data['kind'], plan, grand_test, data.get('code', ''), request.user, teacher_course=teacher_course,
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


class ComboQuoteView(APIView):
    """Live pricing preview for the "Build Your Own Combo" builder — the
    frontend calls this on every selection change; POST /purchases/ recomputes
    the same way at actual purchase time via the same _validate_and_price_combo
    helper, so preview and purchase never disagree."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        # request.data is a QueryDict for multipart/form-encoded requests —
        # .get() would silently return only the last value of a repeated
        # key, same pitfall DRF's ListField avoids internally via .getlist().
        if hasattr(request.data, 'getlist'):
            plan_ids = request.data.getlist('plan_ids')
        else:
            plan_ids = request.data.get('plan_ids') or []
        if not plan_ids:
            return Response({'valid': False, 'detail': 'Select at least 2 products to build a combo.'}, status=400)
        plans = list(SubscriptionPlan.objects.filter(pk__in=plan_ids))
        if len(plans) != len(set(plan_ids)):
            return Response({'valid': False, 'detail': 'One or more selected plans were not found.'}, status=400)
        try:
            individual_value, discount_percent, you_save, final_price = _validate_and_price_combo(plans)
        except PaymentError as exc:
            return Response({'valid': False, 'detail': exc.message}, status=400)
        return Response({
            'valid': True, 'individual_value': individual_value, 'discount_percent': discount_percent,
            'you_save': you_save, 'final_price': final_price,
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
            coupon_course_ids = set(coupon.courses.values_list('id', flat=True))
            if coupon_course_ids and user.active_course_id not in coupon_course_ids:
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
    queryset = Purchase.objects.select_related(
        'user', 'plan', 'grand_test', 'teacher_course', 'combo_plan', 'coupon', 'grand_test_access',
    ).prefetch_related('combo_items__plan').all()
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
        search = self.request.query_params.get('search')
        if search:
            search = search.strip()
            order_match = None
            if search.upper().startswith('HM-'):
                try:
                    order_match = int(search.upper().removeprefix('HM-'))
                except ValueError:
                    order_match = None
            qs = qs.filter(Q(payment_reference__icontains=search) | Q(pk=order_match)) if order_match else qs.filter(payment_reference__icontains=search)
        provider = self.request.query_params.get('provider')
        if provider:
            qs = qs.filter(payment_method__provider_type=provider)
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = CreatePurchaseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data['kind'] == 'combo':
            return self._create_combo(request, data)

        plan = get_object_or_404(SubscriptionPlan, pk=data['plan_id']) if data.get('plan_id') else None
        grand_test = get_object_or_404(Test, pk=data['grand_test_id']) if data.get('grand_test_id') else None
        teacher_course = None
        if data.get('teacher_course_id'):
            from marketplace.models import TeacherCourse

            teacher_course = get_object_or_404(TeacherCourse, pk=data['teacher_course_id'], status='approved')
        if data['kind'] == 'subscription' and not plan:
            return Response({'detail': 'plan_id is required.'}, status=400)
        if data['kind'] == 'grand_test' and not grand_test:
            return Response({'detail': 'grand_test_id is required.'}, status=400)
        if data['kind'] == 'teacher_course' and not teacher_course:
            return Response({'detail': 'teacher_course_id is required.'}, status=400)
        if data['kind'] == 'grand_test' and GrandTestAccess.objects.filter(user=request.user, test=grand_test).exists():
            return Response({'detail': 'You already have access to this Grand Test.'}, status=400)
        if data['kind'] == 'teacher_course':
            from marketplace.models import CourseEnrollment

            already_enrolled = CourseEnrollment.objects.filter(
                user=request.user, course=teacher_course, is_active=True,
            ).filter(Q(expires_at__isnull=True) | Q(expires_at__gte=timezone.now())).exists()
            if already_enrolled:
                return Response({'detail': 'You already have access to this course.'}, status=400)

        amount, discount, final, coupon, error = compute_price(
            data['kind'], plan, grand_test, data.get('coupon_code', ''), request.user, teacher_course=teacher_course,
        )
        if error:
            return Response({'detail': error}, status=400)

        purchase = Purchase.objects.create(
            user=request.user, kind=data['kind'], plan=plan, grand_test=grand_test, teacher_course=teacher_course,
            coupon=coupon, original_amount=amount, discount_amount=discount, final_amount=final,
            expires_at=(timezone.now() + timezone.timedelta(minutes=Purchase.EXPIRY_MINUTES)) if final > 0 else None,
        )

        if final <= 0:
            # Nothing to verify — a 100%-off / free-grant coupon needs no manual bank-transfer
            # check, so activate immediately instead of leaving it stuck in "pending".
            payment_service.activate(purchase.id, request=request, allow_unpaid=True)
            purchase.refresh_from_db()

        return Response(PurchaseSerializer(purchase).data, status=status.HTTP_201_CREATED)

    def _create_combo(self, request, data):
        """kind='combo' branch, split out of create() since combo purchases
        resolve/price a list of plans instead of one product and never accept
        a coupon_code — see _validate_and_price_combo / _price_predefined_combo."""
        combo_plan = None
        try:
            if data.get('combo_plan_id'):
                combo_plan = get_object_or_404(ComboPlan, pk=data['combo_plan_id'], is_active=True)
                plans, individual_value, you_save, final_price = _price_predefined_combo(combo_plan)
            else:
                plan_ids = data.get('plan_ids') or []
                if not plan_ids:
                    return Response({'detail': 'combo_plan_id or plan_ids is required.'}, status=400)
                plans = list(SubscriptionPlan.objects.filter(pk__in=plan_ids))
                if len(plans) != len(set(plan_ids)):
                    return Response({'detail': 'One or more selected plans were not found.'}, status=400)
                individual_value, _discount_percent, you_save, final_price = _validate_and_price_combo(plans)
        except PaymentError as exc:
            return Response({'detail': exc.message}, status=400)

        purchase = Purchase.objects.create(
            user=request.user, kind='combo', combo_plan=combo_plan,
            original_amount=individual_value, discount_amount=you_save, final_amount=final_price,
            expires_at=(timezone.now() + timezone.timedelta(minutes=Purchase.EXPIRY_MINUTES)) if final_price > 0 else None,
        )
        PurchaseComboItem.objects.bulk_create([
            PurchaseComboItem(purchase=purchase, plan=p, price=p.price) for p in plans
        ])

        if final_price <= 0:
            payment_service.activate(purchase.id, request=request, allow_unpaid=True)
            purchase.refresh_from_db()

        return Response(PurchaseSerializer(purchase).data, status=status.HTTP_201_CREATED)

    @action(
        detail=True, methods=['post'], permission_classes=[IsAuthenticated],
        url_path='submit-payment', throttle_classes=[SubmitPaymentThrottle],
    )
    def submit_payment(self, request, pk=None):
        """Student submits (or resubmits) proof of payment — the same endpoint
        serves both a first-time submission (status='unpaid') and a resubmission
        after the admin requested new proof (status='resubmission_requested'),
        since the form and transition are identical either way. All the actual
        guard/lock/duplicate-reference logic lives in payment_service.submit."""
        purchase = self.get_object()
        serializer = SubmitPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            purchase = payment_service.submit(
                purchase.id, request=request,
                payment_method=data['payment_method'],
                payment_reference=data['payment_reference'],
                screenshot_file=data['payment_screenshot'],
            )
        except PaymentError as exc:
            return Response({'detail': exc.message}, status=400)
        return Response(PurchaseSerializer(purchase).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAdminUser])
    def approve(self, request, pk=None):
        purchase = self.get_object()
        try:
            purchase = ManualQRProvider.approve(purchase.id, actor=request.user, request=request)
        except PaymentError as exc:
            return Response({'detail': exc.message}, status=400)
        return Response(PurchaseSerializer(purchase).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAdminUser])
    def reject(self, request, pk=None):
        purchase = self.get_object()
        note = request.data.get('admin_note') or ''
        try:
            purchase = ManualQRProvider.reject(purchase.id, note, actor=request.user, request=request)
        except PaymentError as exc:
            return Response({'detail': exc.message}, status=400)
        return Response(PurchaseSerializer(purchase).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAdminUser], url_path='request-resubmission')
    def request_resubmission(self, request, pk=None):
        purchase = self.get_object()
        note = request.data.get('admin_note') or ''
        try:
            purchase = payment_service.request_resubmission(purchase.id, note, actor=request.user, request=request)
        except PaymentError as exc:
            return Response({'detail': exc.message}, status=400)
        return Response(PurchaseSerializer(purchase).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def cancel(self, request, pk=None):
        """Student (or staff) explicitly cancels an unpaid/resubmission order —
        e.g. they changed their mind before scanning the QR. Distinct from
        expiry (a passive timeout); this is an active choice."""
        purchase = self.get_object()
        try:
            purchase = payment_service.cancel(purchase.id, actor=request.user, request=request)
        except PaymentError as exc:
            return Response({'detail': exc.message}, status=400)
        return Response(PurchaseSerializer(purchase).data)

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated])
    def screenshot(self, request, pk=None):
        """Returns a short-lived signed URL for the submitted payment
        screenshot — never a direct/predictable public path. Owner or staff
        only; returned as JSON (not a redirect) so the signed URL doesn't end
        up in server access/referrer logs the way a redirect target would."""
        purchase = self.get_object()
        if not (request.user.is_staff or purchase.user_id == request.user.id):
            return Response({'detail': 'Not allowed.'}, status=403)
        url = screenshot_view_url(purchase.payment_screenshot_bucket, purchase.payment_screenshot_key)
        if not url:
            return Response({'detail': 'No screenshot on file for this purchase.'}, status=404)
        return Response({'url': url})

    @action(detail=True, methods=['get'], permission_classes=[IsAdminUser], url_path='audit-log')
    def audit_log(self, request, pk=None):
        purchase = self.get_object()
        entries = purchase.audit_log.select_related('actor').all()
        return Response(PaymentAuditLogSerializer(entries, many=True).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated], url_path='email-invoice')
    def email_invoice(self, request, pk=None):
        """Emails the same line-item breakdown the printable /invoice/{id} page
        shows — plain text, reusing the send_mail pattern already used for
        grand-test-access emails. No PDF generation (no library in this stack;
        the printable page already covers view/print/save-as-PDF)."""
        purchase = self.get_object()
        if purchase.kind == 'grand_test':
            item_label = purchase.grand_test.title if purchase.grand_test_id else 'Grand Test'
        elif purchase.kind == 'teacher_course':
            item_label = purchase.teacher_course.title if purchase.teacher_course_id else 'Course'
        elif purchase.kind == 'combo':
            if purchase.combo_plan_id:
                item_label = purchase.combo_plan.name
            else:
                item_label = 'Custom Combo: ' + ', '.join(
                    i.plan.name for i in purchase.combo_items.select_related('plan')
                )
        else:
            item_label = purchase.plan.name if purchase.plan_id else 'Purchase'
        lines = [
            f'Invoice {purchase.order_id}',
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
            f'Your invoice {purchase.order_id} — Dr. Gutka', '\n'.join(lines),
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


class ExpireStalePaymentsView(APIView):
    """POST /api/cron/expire-stale-payments/ — sweeps 'unpaid' purchases past
    their 30-minute QR window and flips them to 'expired'. Needs a *much*
    more frequent Cloud Scheduler entry than send-renewal-reminders (e.g.
    every 5 minutes, not daily) — same shared-secret pattern, separate job."""
    permission_classes = [AllowAny]

    def post(self, request):
        if not _check_cron_secret(request):
            return Response({'detail': 'Invalid or missing cron secret.'}, status=401)

        stale_ids = list(
            Purchase.objects.filter(status='unpaid', expires_at__isnull=False, expires_at__lt=timezone.now())
            .values_list('id', flat=True)
        )
        expired_count = 0
        for purchase_id in stale_ids:
            payment_service.expire(purchase_id)
            expired_count += 1

        return Response({'checked': len(stale_ids), 'expired': expired_count})
