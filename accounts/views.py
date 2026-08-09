from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from courses.models import Enrollment
from hamromentor.permissions import IsAdminRoleOrAbove, IsSuperAdmin

from .models import Device, RolePermission, StudentProfile
from .serializers import (
    AdminAccountSerializer,
    AdminUserSerializer,
    LoginSerializer,
    RegisterSerializer,
    RolePermissionSerializer,
    UserSerializer,
)

User = get_user_model()

MAX_DEVICES = 3


def tokens_for_user(user):
    refresh = RefreshToken.for_user(user)
    return {'access': str(refresh.access_token), 'refresh': str(refresh)}


class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            {'user': UserSerializer(user).data, 'tokens': tokens_for_user(user)},
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        identifier = data['identifier']

        try:
            user = User.objects.get(Q(email__iexact=identifier) | Q(phone=identifier))
        except User.DoesNotExist:
            return Response({'detail': 'Invalid credentials.'}, status=status.HTTP_401_UNAUTHORIZED)

        if not user.check_password(data['password']):
            return Response({'detail': 'Invalid credentials.'}, status=status.HTTP_401_UNAUTHORIZED)

        if not user.is_active:
            return Response(
                {'detail': 'This account has been blocked. Contact support.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        device_id = data.get('device_id')
        if device_id:
            known = Device.objects.filter(user=user, device_id=device_id).first()
            if known:
                known.device_label = data.get('device_label', known.device_label)
                known.save()
            else:
                device_count = Device.objects.filter(user=user).count()
                if device_count >= MAX_DEVICES:
                    user.is_active = False
                    user.save(update_fields=['is_active'])
                    return Response(
                        {'detail': 'Login limit of 3 devices reached. Account blocked.'},
                        status=status.HTTP_403_FORBIDDEN,
                    )
                Device.objects.create(
                    user=user, device_id=device_id, device_label=data.get('device_label', ''),
                )

        return Response({'user': UserSerializer(user).data, 'tokens': tokens_for_user(user)})


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if not user.is_staff and not user.active_course_id:
            first_enrollment = Enrollment.objects.filter(user=user, is_active=True).select_related('course').first()
            if first_enrollment:
                user.active_course = first_enrollment.course
                user.save(update_fields=['active_course'])
        return Response(UserSerializer(user).data)


class ActiveCourseView(APIView):
    """Switches the student's 'current subcourse' — everything the dashboard
    shows (QBank, Mock/Daily/Grand Test, PYQ, performance) is scoped to this."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        course_id = request.data.get('course_id')
        if not course_id:
            return Response({'detail': 'course_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not Enrollment.objects.filter(user=request.user, course_id=course_id, is_active=True).exists():
            return Response({'detail': 'You are not enrolled in that course.'}, status=status.HTTP_403_FORBIDDEN)
        request.user.active_course_id = course_id
        request.user.save(update_fields=['active_course'])
        return Response(UserSerializer(request.user).data)


class AdminUserViewSet(viewsets.ModelViewSet):
    """Staff-only user management: list students, toggle active/staff status."""
    queryset = User.objects.filter(is_staff=False).select_related('profile').order_by('-date_joined')
    serializer_class = AdminUserSerializer
    permission_classes = [IsAdminRoleOrAbove]
    http_method_names = ['get', 'patch', 'head', 'options']

    def get_queryset(self):
        qs = super().get_queryset()
        search = self.request.query_params.get('search')
        course = self.request.query_params.get('course')
        date_from = self.request.query_params.get('from')
        date_to = self.request.query_params.get('to')
        if search:
            qs = qs.filter(
                Q(first_name__icontains=search) | Q(last_name__icontains=search)
                | Q(email__icontains=search) | Q(phone__icontains=search)
            )
        if course:
            qs = qs.filter(enrollments__course_id=course)
        if date_from:
            qs = qs.filter(date_joined__date__gte=date_from)
        if date_to:
            qs = qs.filter(date_joined__date__lte=date_to)
        return qs.distinct()


class AdminAccountViewSet(viewsets.ModelViewSet):
    """Super-Admin-only: manage other admin-panel accounts (Super Admin/Admin/Editor)."""
    queryset = User.objects.filter(is_staff=True).order_by('-date_joined')
    serializer_class = AdminAccountSerializer
    permission_classes = [IsSuperAdmin]


class TeacherListView(APIView):
    """Lightweight, any-staff-readable list of Teacher-role accounts — used to
    populate the 'Teacher' filter on Question Entry without requiring the
    Super-Admin-only /admin-accounts/ endpoint. Editors have Question Entry
    access too, so this intentionally allows any staff account, not just Admin+."""
    permission_classes = [IsAdminUser]

    def get(self, request):
        teachers = User.objects.filter(is_staff=True, admin_role='teacher').order_by('first_name', 'email')
        data = [{'id': t.id, 'name': t.first_name or t.email, 'email': t.email} for t in teachers]
        return Response(data)


class RolePermissionViewSet(viewsets.ModelViewSet):
    queryset = RolePermission.objects.all()
    serializer_class = RolePermissionSerializer
    permission_classes = [IsSuperAdmin]
    http_method_names = ['get', 'post', 'put', 'patch', 'head', 'options']


class AccountSettingsView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        name = request.data.get('name', '').strip()
        if name:
            first_name, _, last_name = name.partition(' ')
            request.user.first_name = first_name
            request.user.last_name = last_name
            request.user.save(update_fields=['first_name', 'last_name'])

        payment_channel = request.data.get('preferred_payment_channel')
        if payment_channel is not None:
            valid_channels = dict(StudentProfile.PAYMENT_CHANNEL_CHOICES)
            if payment_channel and payment_channel not in valid_channels:
                return Response({'detail': 'Invalid preferred_payment_channel.'}, status=400)
            profile, _ = StudentProfile.objects.get_or_create(user=request.user)
            profile.preferred_payment_channel = payment_channel
            profile.save(update_fields=['preferred_payment_channel'])

        return Response(UserSerializer(request.user).data)


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        current = request.data.get('current_password', '')
        new = request.data.get('new_password', '')
        confirm = request.data.get('confirm_password', '')

        if not request.user.check_password(current):
            return Response({'detail': 'Current password is incorrect.'}, status=status.HTTP_400_BAD_REQUEST)
        if new != confirm:
            return Response({'detail': 'New passwords do not match.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            validate_password(new, user=request.user)
        except Exception as exc:
            return Response({'detail': ' '.join(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)

        request.user.set_password(new)
        request.user.save()
        return Response({'detail': 'Password changed.'})
