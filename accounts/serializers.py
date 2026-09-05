from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from billing.models import Purchase
from courses.models import Enrollment, EnrollmentRequest
from tests_app.models import TestAttempt

from .models import Device, RolePermission, StudentProfile

User = get_user_model()


class StudentProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = StudentProfile
        fields = [
            'college', 'district', 'province', 'exam_target', 'batch',
            'photo', 'plan_expires_at', 'preferred_payment_channel',
        ]


class UserSerializer(serializers.ModelSerializer):
    profile = StudentProfileSerializer(read_only=True)
    teacher_profile = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()
    active_course_detail = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'first_name', 'last_name', 'email', 'phone',
            'program', 'course', 'profile', 'teacher_profile', 'is_staff', 'is_superuser', 'admin_role', 'permissions',
            'active_course', 'active_course_detail', 'referral_code', 'wallet_balance',
        ]
        read_only_fields = ['referral_code', 'wallet_balance']

    def get_teacher_profile(self, obj):
        from marketplace.serializers import TeacherProfileSummarySerializer

        profile = getattr(obj, 'teacher_profile', None)
        return TeacherProfileSummarySerializer(profile).data if profile else None

    def get_active_course_detail(self, obj):
        if not obj.active_course_id:
            return None
        c = obj.active_course
        return {'id': c.id, 'name': c.name, 'prefix': c.prefix, 'program_group': c.program_group}

    def get_permissions(self, obj):
        from .models import ALL_FEATURES, EDITOR_ALLOWED_FEATURES, TEACHER_ALLOWED_FEATURES

        if not obj.is_staff:
            return []
        if obj.is_superuser or obj.admin_role in (None, '', 'super_admin'):
            return ALL_FEATURES
        if obj.admin_role == 'teacher':
            # Fixed ceiling — not admin-configurable via RolePermission (see model comment).
            return TEACHER_ALLOWED_FEATURES
        role_permission = RolePermission.objects.filter(role=obj.admin_role).first()
        if role_permission:
            return role_permission.features
        return ALL_FEATURES if obj.admin_role == 'admin' else EDITOR_ALLOWED_FEATURES


class RegisterSerializer(serializers.ModelSerializer):
    name = serializers.CharField(write_only=True)
    password = serializers.CharField(write_only=True, validators=[validate_password])
    referral_code = serializers.CharField(write_only=True, required=False, allow_blank=True)
    college = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = ['name', 'email', 'phone', 'password', 'program', 'course', 'referral_code', 'college']

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError('An account with this email already exists.')
        return value

    def create(self, validated_data):
        name = validated_data.pop('name')
        first_name, _, last_name = name.partition(' ')
        base_username = validated_data['email'].split('@')[0]
        username = base_username
        suffix = 1
        while User.objects.filter(username=username).exists():
            suffix += 1
            username = f'{base_username}{suffix}'

        password = validated_data.pop('password')
        referral_code = validated_data.pop('referral_code', '').strip().upper()
        referred_by = User.objects.filter(referral_code=referral_code).first() if referral_code else None
        college = validated_data.pop('college', '').strip()

        user = User(
            username=username,
            first_name=first_name,
            last_name=last_name,
            referred_by=referred_by,
            **validated_data,
        )
        user.set_password(password)
        user.save()
        StudentProfile.objects.create(user=user, college=college)

        # Unified Exam Catalog Visibility fix (root cause of "free student
        # sees 0 tests"): courses.access.eligible_course_ids() — the single
        # gate behind Test/Question/Video catalog visibility everywhere in
        # the app — reads ONLY courses.Enrollment. Until now, Enrollment was
        # created exactly two ways: an admin manually approving an
        # EnrollmentRequest, or billing.payment_service._ensure_enrollment()
        # on a successful purchase (see that function's own docstring — it
        # exists because "a student could pay... and still see zero
        # content"). Registration created neither. The student picks a
        # course right here on this form (validated_data['course'], a
        # Course.prefix — see User.course's help_text) and nothing ever
        # turned that choice into catalog membership, so every self-
        # registered free student was catalog-blind — 0 Daily/Mock/Grand/
        # PYQ/QBank content — until an admin happened to enroll them, which
        # in practice only ever happened as a side effect of paying.
        #
        # This mirrors _ensure_enrollment exactly (same model, same
        # update_or_create shape) with access_type='free' instead of
        # 'package' — the ACCESS_CHOICES tier that has existed on Enrollment
        # since Phase 2 for exactly this case and was simply never wired to
        # the one path that needed it. It grants catalog visibility only —
        # no commercial entitlement, no Free Starter interaction, no
        # capability change. A student who never bought anything still
        # can't Start a Pro test; they can now see it exists.
        #
        # Best-effort, matching the same "never fail registration" contract
        # this codebase already uses elsewhere: an unmatched/blank course
        # must never fail registration — the student can still pick a
        # course later via ActiveCourseView once an admin (or a future
        # self-service flow) enrolls them.
        course_prefix = (getattr(user, 'course', '') or '').strip()
        if course_prefix:
            try:
                from courses.models import Course

                course = Course.objects.filter(prefix=course_prefix).first()
                if course:
                    Enrollment.objects.update_or_create(
                        user=user, course=course,
                        defaults={'access_type': 'free', 'is_active': True},
                    )
            except Exception:
                pass

        return user


class AdminUserSerializer(serializers.ModelSerializer):
    profile = StudentProfileSerializer(read_only=True)
    device_count = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'first_name', 'last_name', 'email', 'phone', 'program', 'course',
            'is_active', 'is_staff', 'date_joined', 'profile', 'device_count',
        ]
        read_only_fields = ['email', 'date_joined']

    def get_device_count(self, obj):
        return obj.devices.count()


class AdminStudentEnrollmentSummarySerializer(serializers.ModelSerializer):
    """Phase 3 — deliberately smaller than AdminStudentEnrollmentSerializer
    (the Student Detail tab's serializer, Phase 1): only what the Students
    LIST page's existing Courses/Access columns actually render. No
    enrolled_at/expires_at/package/batch — "no full enrollment history in
    the list", per the Phase 3 spec. The richer per-enrollment detail stays
    exactly where it already lives, one click away, on Student Detail."""
    course_prefix = serializers.CharField(source='course.prefix', read_only=True)

    class Meta:
        model = Enrollment
        fields = ['id', 'course_prefix', 'student_code', 'access_type', 'is_active']


class AdminStudentBrowseSerializer(serializers.ModelSerializer):
    """Backs GET /auth/users/browse/ only (Phase 3) — AdminUserSerializer
    above keeps backing list()/retrieve()/partial_update() unchanged.
    `device_count` reads the queryset's own annotation (no per-row query);
    `enrollments` reads the view's bounded Prefetch (to_attr='browse_enrollments') —
    neither field ever triggers a query of its own no matter how this
    serializer is (mis)used, by the same discipline AdminUserDetailSerializer
    (Phase 1) already established for detail_* attributes."""
    profile = StudentProfileSerializer(read_only=True)
    device_count = serializers.IntegerField(read_only=True)
    enrollments = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'first_name', 'last_name', 'email', 'phone', 'program', 'course',
            'is_active', 'is_staff', 'date_joined', 'profile', 'device_count', 'enrollments',
        ]
        read_only_fields = fields

    def get_enrollments(self, obj):
        return AdminStudentEnrollmentSummarySerializer(getattr(obj, 'browse_enrollments', []), many=True).data


class AdminStudentEnrollmentSerializer(serializers.ModelSerializer):
    """Lightweight — deliberately not courses.serializers.EnrollmentSerializer,
    which adds its own obj.user.devices.count() per row (fine on that
    endpoint's own bounded/paginated list, redundant and avoidable here
    since the student detail response already carries one top-level
    device_count)."""
    course_name = serializers.CharField(source='course.name', read_only=True)
    course_prefix = serializers.CharField(source='course.prefix', read_only=True)
    package_name = serializers.CharField(source='package.name', read_only=True, default=None)
    batch_name = serializers.CharField(source='batch.name', read_only=True, default=None)

    class Meta:
        model = Enrollment
        fields = [
            'id', 'course', 'course_name', 'course_prefix', 'package', 'package_name',
            'batch', 'batch_name', 'student_code', 'access_type', 'is_active', 'enrolled_at', 'expires_at',
        ]


class AdminStudentEnrollmentRequestSerializer(serializers.ModelSerializer):
    course_name = serializers.CharField(source='course.name', read_only=True)
    package_name = serializers.CharField(source='package.name', read_only=True, default=None)

    class Meta:
        model = EnrollmentRequest
        fields = ['id', 'course', 'course_name', 'package', 'package_name', 'student_code', 'status', 'submitted_at', 'decided_at']


class AdminStudentPurchaseSerializer(serializers.ModelSerializer):
    """Lightweight sibling of billing.serializers.PurchaseSerializer — that
    one nests combo_items/grand_test_access/payment_method_detail (built for
    the Payments admin screen's own single-purchase workflows); this one
    only carries what a student's Payments tab needs, kept bounded to the
    latest N rows by the view's Prefetch. Never exposes the raw screenshot
    key/bucket/URL — has_screenshot mirrors PurchaseSerializer's own pattern
    of leaving the actual signed-URL fetch to the existing, already-
    authorized GET /purchases/{id}/screenshot/ endpoint."""
    order_id = serializers.ReadOnlyField()
    item_name = serializers.SerializerMethodField()
    decided_by_name = serializers.SerializerMethodField()
    has_screenshot = serializers.SerializerMethodField()

    class Meta:
        model = Purchase
        fields = [
            'id', 'order_id', 'kind', 'item_name', 'final_amount', 'status',
            'payment_reference', 'admin_note', 'created_at', 'decided_at', 'decided_by_name', 'has_screenshot',
        ]

    def get_item_name(self, obj):
        if obj.kind == 'subscription' and obj.plan_id:
            return obj.plan.name
        if obj.kind == 'grand_test' and obj.grand_test_id:
            return obj.grand_test.title
        if obj.kind == 'teacher_course' and obj.teacher_course_id:
            return obj.teacher_course.title
        if obj.kind == 'combo' and obj.combo_plan_id:
            return obj.combo_plan.name
        return ''

    def get_decided_by_name(self, obj):
        if not obj.decided_by_id:
            return None
        return obj.decided_by.first_name or obj.decided_by.email

    def get_has_screenshot(self, obj):
        return bool(obj.payment_screenshot_key)


class AdminStudentTestAttemptSerializer(serializers.ModelSerializer):
    test_title = serializers.CharField(source='test.title', read_only=True)
    exam_type = serializers.CharField(source='test.exam_type', read_only=True)

    class Meta:
        model = TestAttempt
        fields = ['id', 'test', 'test_title', 'exam_type', 'score', 'accuracy', 'rank', 'percentile', 'status', 'start_time', 'end_time']


class AdminUserDetailSerializer(serializers.ModelSerializer):
    """Backs GET /auth/users/<id>/detail/ only — the student LIST/retrieve
    actions keep using the shallow AdminUserSerializer above unchanged, so
    list-page payload size/query cost is unaffected by anything added here.

    Every related-collection field below reads from a `detail_*` attribute
    the view attaches via prefetch_related(Prefetch(..., to_attr=...)) —
    never obj.enrollments.all() etc. directly — so this serializer never
    triggers its own queries no matter how it's called; the view is the
    single place responsible for keeping this bounded and N+1-free (see
    AdminUserViewSet.student_detail's docstring)."""
    profile = StudentProfileSerializer(read_only=True)
    active_course_detail = serializers.SerializerMethodField()
    referred_by = serializers.SerializerMethodField()
    enrollments = serializers.SerializerMethodField()
    enrollment_requests = serializers.SerializerMethodField()
    purchases = serializers.SerializerMethodField()
    devices = serializers.SerializerMethodField()
    device_count = serializers.SerializerMethodField()
    activity_summary = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'username', 'first_name', 'last_name', 'email', 'phone',
            'program', 'course', 'active_course', 'active_course_detail',
            'is_active', 'is_staff', 'date_joined',
            'referral_code', 'wallet_balance', 'referred_by',
            'profile', 'enrollments', 'enrollment_requests', 'purchases',
            'devices', 'device_count', 'activity_summary',
        ]
        read_only_fields = fields

    def get_active_course_detail(self, obj):
        if not obj.active_course_id:
            return None
        c = obj.active_course
        return {'id': c.id, 'name': c.name, 'prefix': c.prefix, 'program_group': c.program_group}

    def get_referred_by(self, obj):
        if not obj.referred_by_id:
            return None
        r = obj.referred_by
        return {'id': r.id, 'name': f'{r.first_name} {r.last_name}'.strip() or r.email, 'email': r.email}

    def get_enrollments(self, obj):
        return AdminStudentEnrollmentSerializer(getattr(obj, 'detail_enrollments', []), many=True).data

    def get_enrollment_requests(self, obj):
        return AdminStudentEnrollmentRequestSerializer(getattr(obj, 'detail_enrollment_requests', []), many=True).data

    def get_purchases(self, obj):
        return AdminStudentPurchaseSerializer(getattr(obj, 'detail_purchases', []), many=True).data

    def get_devices(self, obj):
        return DeviceSerializer(getattr(obj, 'detail_devices', []), many=True).data

    def get_device_count(self, obj):
        # Devices realistically never exceed MAX_DEVICES (3) — LoginView
        # blocks the account once a 4th distinct device tries to log in
        # (accounts/views.py) — so the bounded detail_devices prefetch
        # (latest 20) is definitionally the complete set in every real case.
        return len(getattr(obj, 'detail_devices', []))

    def get_activity_summary(self, obj):
        return getattr(obj, 'detail_activity_summary', {})


class AdminStudentEditSerializer(serializers.Serializer):
    """Phase 2 — the field allowlist for GET.../edit/. Deliberately a plain
    Serializer, not a ModelSerializer bound to `User` or `StudentProfile`:
    the request is one flat body touching fields split across both models,
    and a bare allowlist here is also the single source of truth the view
    uses to reject any key outside it (see AdminUserViewSet.student_edit) —
    a ModelSerializer's Meta.fields would only describe one model's shape.

    Every field the audit + your Phase 2 spec named as editable, and
    NOTHING else — email, password, username, referral_code, wallet_balance,
    active_course, is_active, enrollments/purchases/etc. are all
    unreachable through this serializer by construction, not by convention.
    """
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True, allow_null=True)
    program = serializers.CharField(max_length=50, required=False, allow_blank=True)
    course = serializers.CharField(max_length=50, required=False, allow_blank=True)
    college = serializers.CharField(max_length=255, required=False, allow_blank=True)
    district = serializers.CharField(max_length=100, required=False, allow_blank=True)
    province = serializers.CharField(max_length=100, required=False, allow_blank=True)
    exam_target = serializers.CharField(max_length=100, required=False, allow_blank=True)
    batch = serializers.CharField(max_length=50, required=False, allow_blank=True)

    # Split so the view knows which object to write each validated field to.
    USER_FIELDS = ('first_name', 'last_name', 'phone', 'program', 'course')
    PROFILE_FIELDS = ('college', 'district', 'province', 'exam_target', 'batch')

    def validate_phone(self, value):
        if not value:
            return value
        qs = User.objects.filter(phone=value)
        target_id = self.context.get('user_id')
        if target_id:
            qs = qs.exclude(pk=target_id)
        if qs.exists():
            raise serializers.ValidationError('This phone number is already in use by another account.')
        return value


class LoginSerializer(serializers.Serializer):
    identifier = serializers.CharField(help_text='Email or phone number')
    password = serializers.CharField(write_only=True)
    device_id = serializers.CharField(required=False, allow_blank=True)
    device_label = serializers.CharField(required=False, allow_blank=True)


class DeviceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Device
        fields = ['id', 'device_id', 'device_label', 'last_seen']


class AdminAccountSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = [
            'id', 'name', 'first_name', 'email', 'admin_role', 'can_manage_all_content',
            'is_active', 'date_joined', 'password',
        ]
        read_only_fields = ['date_joined']

    def get_name(self, obj):
        return obj.first_name or obj.email.split('@')[0]

    def validate_email(self, value):
        qs = User.objects.filter(email__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('An account with this email already exists.')
        return value

    def create(self, validated_data):
        password = validated_data.pop('password', None) or User.objects.make_random_password()
        base_username = validated_data['email'].split('@')[0]
        username = base_username
        suffix = 1
        while User.objects.filter(username=username).exists():
            suffix += 1
            username = f'{base_username}{suffix}'
        user = User(username=username, is_staff=True, **validated_data)
        user.set_password(password)
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class RolePermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = RolePermission
        fields = ['id', 'role', 'features']
