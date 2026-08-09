from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

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
