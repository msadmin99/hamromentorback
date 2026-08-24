from decimal import Decimal

from rest_framework import serializers

from .models import (
    MAX_COMBO_DISCOUNT_PERCENT,
    ComboPlan,
    Coupon,
    GrandTestAccess,
    PaymentAuditLog,
    PaymentMethod,
    Purchase,
    PurchaseComboItem,
    Scholarship,
    Subscription,
    SubscriptionPlan,
)
from .screenshot_storage import InvalidScreenshot, detect_image_format

MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024


class PaymentAuditLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = PaymentAuditLog
        fields = [
            'id', 'action', 'previous_status', 'new_status', 'actor', 'actor_name', 'actor_email',
            'ip_address', 'reason', 'metadata', 'created_at',
        ]

    def get_actor_name(self, obj):
        if not obj.actor:
            return obj.actor_email or 'System'
        return f'{obj.actor.first_name} {obj.actor.last_name}'.strip() or obj.actor.email


class PaymentMethodSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentMethod
        fields = [
            'id', 'name', 'slug', 'provider_type', 'merchant_name', 'merchant_id', 'account_info',
            'qr_code_image', 'instructions', 'is_active', 'order',
        ]
        extra_kwargs = {'slug': {'required': False}}


class SubscriptionPlanSerializer(serializers.ModelSerializer):
    course_name = serializers.CharField(source='course.name', read_only=True)

    class Meta:
        model = SubscriptionPlan
        fields = [
            'id', 'course', 'course_name', 'product_type', 'name', 'duration_value', 'duration_unit',
            'mock_test_quota', 'price', 'is_active', 'is_popular', 'is_best_value', 'order',
        ]


class ComboPlanSerializer(serializers.ModelSerializer):
    plan_details = serializers.SerializerMethodField()
    individual_value = serializers.SerializerMethodField()
    you_save = serializers.SerializerMethodField()
    final_price = serializers.SerializerMethodField()

    class Meta:
        model = ComboPlan
        fields = [
            'id', 'name', 'course', 'plans', 'plan_details', 'discount_percent', 'individual_value',
            'you_save', 'final_price', 'is_popular', 'is_best_value', 'is_active', 'order', 'created_at',
        ]

    def _individual_value(self, obj):
        return sum((p.price for p in obj.plans.all()), Decimal('0'))

    def get_plan_details(self, obj):
        return [
            {
                'id': p.id, 'product_type': p.product_type, 'name': p.name, 'price': p.price,
                'duration_value': p.duration_value, 'duration_unit': p.duration_unit,
                'mock_test_quota': p.mock_test_quota,
            }
            for p in obj.plans.all()
        ]

    def get_individual_value(self, obj):
        return self._individual_value(obj)

    def get_you_save(self, obj):
        return self._individual_value(obj) * obj.discount_percent / 100

    def get_final_price(self, obj):
        value = self._individual_value(obj)
        return value - (value * obj.discount_percent / 100)

    def validate_discount_percent(self, value):
        if value > MAX_COMBO_DISCOUNT_PERCENT:
            raise serializers.ValidationError(f'Discount cannot exceed {MAX_COMBO_DISCOUNT_PERCENT}%.')
        return value

    def validate_plans(self, value):
        if len({p.product_type for p in value}) != len(value):
            raise serializers.ValidationError('A combo can only include one plan per product type.')
        return value


class SubscriptionSerializer(serializers.ModelSerializer):
    course_name = serializers.CharField(source='course.name', read_only=True)
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    is_current = serializers.BooleanField(read_only=True)

    class Meta:
        model = Subscription
        fields = [
            'id', 'plan', 'plan_name', 'course', 'course_name', 'product_type', 'starts_at', 'expires_at',
            'mock_test_quota', 'mock_test_used', 'auto_renew', 'is_active', 'is_current', 'created_at',
        ]


class CouponSerializer(serializers.ModelSerializer):
    course_names = serializers.SerializerMethodField()

    class Meta:
        model = Coupon
        fields = [
            'id', 'code', 'name', 'courses', 'course_names', 'discount_type', 'discount_value', 'applies_to',
            'start_date', 'expiry_date', 'max_uses', 'max_uses_per_user', 'min_purchase_amount',
            'max_discount_amount', 'first_purchase_only', 'eligibility', 'eligible_emails', 'new_student_days',
            'auto_apply', 'is_active', 'usage_count', 'created_at',
        ]
        read_only_fields = ['usage_count', 'created_at']
        extra_kwargs = {'code': {'validators': []}, 'courses': {'required': False}}

    def get_course_names(self, obj):
        return [c.name for c in obj.courses.all()]

    def validate_code(self, value):
        return value.strip().upper()


class PurchaseComboItemSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    product_type = serializers.CharField(source='plan.product_type', read_only=True)

    class Meta:
        model = PurchaseComboItem
        fields = ['id', 'plan', 'plan_name', 'product_type', 'price']


class GrandTestAccessSerializer(serializers.ModelSerializer):
    test_title = serializers.CharField(source='test.title', read_only=True)

    class Meta:
        model = GrandTestAccess
        fields = ['id', 'test', 'test_title', 'password', 'granted_at', 'email_sent_at']


class PurchaseSerializer(serializers.ModelSerializer):
    order_id = serializers.ReadOnlyField()
    is_expired = serializers.ReadOnlyField()
    user_name = serializers.SerializerMethodField()
    user_email = serializers.CharField(source='user.email', read_only=True)
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    grand_test_title = serializers.CharField(source='grand_test.title', read_only=True)
    teacher_course_title = serializers.CharField(source='teacher_course.title', read_only=True)
    combo_plan_name = serializers.CharField(source='combo_plan.name', read_only=True)
    combo_items = PurchaseComboItemSerializer(many=True, read_only=True)
    coupon_code = serializers.CharField(source='coupon.code', read_only=True)
    grand_test_access = GrandTestAccessSerializer(read_only=True)
    payment_method_detail = PaymentMethodSerializer(source='payment_method', read_only=True)
    has_screenshot = serializers.SerializerMethodField()

    class Meta:
        model = Purchase
        fields = [
            'id', 'order_id', 'user', 'user_name', 'user_email', 'kind', 'plan', 'plan_name', 'grand_test',
            'grand_test_title', 'teacher_course', 'teacher_course_title', 'combo_plan', 'combo_plan_name',
            'combo_items', 'coupon', 'coupon_code', 'currency',
            'original_amount', 'discount_amount', 'final_amount', 'payment_method', 'payment_method_detail',
            'payment_reference', 'has_screenshot', 'status', 'admin_note', 'expires_at', 'is_expired',
            'created_at', 'decided_at', 'grand_test_access',
        ]
        read_only_fields = [
            'user', 'currency', 'original_amount', 'discount_amount', 'final_amount', 'status', 'expires_at',
            'created_at', 'decided_at',
        ]

    def get_user_name(self, obj):
        return f'{obj.user.first_name} {obj.user.last_name}'.strip() or obj.user.email

    def get_has_screenshot(self, obj):
        # Never expose the actual GCS key/bucket or a direct image URL here —
        # the frontend fetches a short-lived signed URL from
        # GET /purchases/{id}/screenshot/ only when it actually needs to
        # display it (see PurchaseViewSet.screenshot).
        return bool(obj.payment_screenshot_key)


class ScholarshipSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()
    user_email = serializers.CharField(source='user.email', read_only=True)
    course_name = serializers.CharField(source='course.name', read_only=True)
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    granted_by_name = serializers.SerializerMethodField()
    subscription_expires_at = serializers.DateTimeField(source='subscription.expires_at', read_only=True)

    class Meta:
        model = Scholarship
        fields = [
            'id', 'user', 'user_name', 'user_email', 'course', 'course_name', 'product_type', 'plan', 'plan_name',
            'subscription', 'subscription_expires_at', 'reason', 'granted_by', 'granted_by_name', 'granted_at',
            'expires_at', 'is_active',
        ]
        read_only_fields = ['granted_by', 'granted_at', 'subscription']

    def get_user_name(self, obj):
        return f'{obj.user.first_name} {obj.user.last_name}'.strip() or obj.user.email

    def get_granted_by_name(self, obj):
        if not obj.granted_by:
            return ''
        return f'{obj.granted_by.first_name} {obj.granted_by.last_name}'.strip() or obj.granted_by.email


class CreatePurchaseSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=Purchase.KIND_CHOICES)
    plan_id = serializers.IntegerField(required=False, allow_null=True)
    grand_test_id = serializers.IntegerField(required=False, allow_null=True)
    teacher_course_id = serializers.IntegerField(required=False, allow_null=True)
    combo_plan_id = serializers.IntegerField(required=False, allow_null=True)
    plan_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    coupon_code = serializers.CharField(required=False, allow_blank=True)


class SubmitPaymentSerializer(serializers.Serializer):
    payment_method = serializers.PrimaryKeyRelatedField(queryset=PaymentMethod.objects.filter(is_active=True))
    payment_reference = serializers.CharField(max_length=150)
    payment_screenshot = serializers.ImageField()

    def validate_payment_reference(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('A transaction reference is required.')
        return value

    def validate_payment_screenshot(self, value):
        if value.size > MAX_SCREENSHOT_BYTES:
            raise serializers.ValidationError('Screenshot must be 5MB or smaller.')
        try:
            detect_image_format(value)
        except InvalidScreenshot as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value


class ApplyCouponSerializer(serializers.Serializer):
    code = serializers.CharField(required=False, allow_blank=True, default='')
    kind = serializers.ChoiceField(choices=Purchase.KIND_CHOICES)
    plan_id = serializers.IntegerField(required=False, allow_null=True)
    grand_test_id = serializers.IntegerField(required=False, allow_null=True)
    teacher_course_id = serializers.IntegerField(required=False, allow_null=True)
