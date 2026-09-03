from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from courses.models import Enrollment

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
