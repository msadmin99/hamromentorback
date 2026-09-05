from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db.models import Count, Prefetch, Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
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


class _StudentBrowsePagination(PageNumberPagination):
    """Real, enveloped pagination ({count, next, previous, results}) for
    AdminUserViewSet.browse only — mirrors academics.views._BrowsePagination
    exactly (same page_size/max_page_size), the established precedent for
    "a paginated variant exists alongside an untouched bare-array list()"."""
    page_size = 20
    max_page_size = 50
    page_size_query_param = 'page_size'


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

    @action(detail=False, methods=['get'], url_path='browse')
    def browse(self, request):
        """Admin Student List (Phase 3) — a separate, real-pagination view.
        Deliberately NOT built on list()/get_queryset(): list() stays
        exactly as it was (bare array, whatever page size the global
        default gives it) because Admin/src/app/scholarships/page.js
        already calls plain GET /auth/users/?search=... and reads the
        response directly as an array — switching list()'s own response
        shape to an envelope would silently break that unrelated page.
        `browse` is new, additive, and is the only endpoint the Students
        list page (Phase 3) now calls — same split QuestionViewSet.browse
        already established for exactly this reason (see _BrowsePagination
        above in academics/views.py).

        Query count is flat regardless of page size or total student
        count: 1 (COUNT for pagination) + 1 (the page's rows, with
        select_related('profile') + an annotated device_count — a single
        JOIN+GROUP BY, not a per-row query) + 1 (one bounded Prefetch for
        every visible student's enrollments, `user_id IN (<=50 ids)`, not
        per-row) = 3. Verified in accounts/tests.py via CaptureQueriesContext
        at 20-row and 50-row pages and against a 500-student dataset.

        Enrollment summary is deliberately minimal (course_prefix,
        student_code, access_type, is_active only) — no enrolled_at/
        expires_at/package/batch — matching "no full enrollment history in
        the list" while still covering exactly what the existing Courses/
        Access UI columns render.

        `access` and `status` filters (added after initial release):
        - `access`: matches courses.Enrollment.access_type, the only two
          real values in the system ('free'/'package' — confirmed against
          Enrollment.ACCESS_CHOICES, nothing invented). Semantics
          deliberately mirror what the UI already displays per student
          (AdminStudentBrowseSerializer/StudentsTable's AccessBadge): a
          student with ANY package enrollment counts as "package"; a
          student with zero enrollments, or only free ones, counts as
          "free". This is a per-STUDENT classification, not "does this one
          enrollment match" — consistent with how `course` already works
          (does this student have *a* matching enrollment, independent of
          any other filter), not a single correlated enrollment satisfying
          every active filter at once.
        - `status`: matches the existing User.is_active — the exact same
          field Block/Unblock already reads and writes. No second status
          system introduced.
        Both are applied as `pk__in`/`exclude(pk__in=...)` against a
        separately-evaluated Enrollment id subquery, same as `course`
        above and for the same reason — never a `.filter(enrollments__...)`
        join, which would corrupt the device_count annotate.
        """
        from .serializers import AdminStudentBrowseSerializer

        qs = (
            User.objects.filter(is_staff=False)
            .select_related('profile')
            .annotate(device_count=Count('devices', distinct=True))
            .order_by('-date_joined')
        )

        # Same filter semantics as get_queryset() above, deliberately
        # duplicated rather than shared — get_queryset() (and list(), which
        # scholarships/page.js depends on) must stay byte-for-byte
        # untouched; this is a separate, independent code path.
        search = request.query_params.get('search')
        course = request.query_params.get('course')
        access = request.query_params.get('access')
        account_status = request.query_params.get('status')
        date_from = request.query_params.get('from')
        date_to = request.query_params.get('to')
        if search:
            qs = qs.filter(
                Q(first_name__icontains=search) | Q(last_name__icontains=search)
                | Q(email__icontains=search) | Q(phone__icontains=search)
            )
        if course:
            # A plain `.filter(enrollments__course_id=course)` join, combined
            # with the device_count annotate above, is the classic Django
            # "annotate + multi-row join" footgun — it can inflate the
            # Count() once a student has more than one enrollment. Filtering
            # by a separately-computed id list instead adds no join at all,
            # so the annotate stays correct regardless of how many
            # enrollments any given student has.
            matching_user_ids = Enrollment.objects.filter(course_id=course).values_list('user_id', flat=True)
            qs = qs.filter(pk__in=matching_user_ids)
        if access == 'package':
            package_user_ids = Enrollment.objects.filter(access_type='package').values_list('user_id', flat=True)
            qs = qs.filter(pk__in=package_user_ids)
        elif access == 'free':
            package_user_ids = Enrollment.objects.filter(access_type='package').values_list('user_id', flat=True)
            qs = qs.exclude(pk__in=package_user_ids)
        if account_status == 'active':
            qs = qs.filter(is_active=True)
        elif account_status == 'blocked':
            qs = qs.filter(is_active=False)
        if date_from:
            qs = qs.filter(date_joined__date__gte=date_from)
        if date_to:
            qs = qs.filter(date_joined__date__lte=date_to)

        # Instantiated directly (not self.paginate_queryset(), which would
        # need a viewset-level pagination_class — and that would also
        # change list()'s pagination, the exact thing this action exists to
        # avoid). Mirrors QuestionViewSet.browse's own pattern.
        paginator = _StudentBrowsePagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        target = list(page)

        # Attach each visible student's enrollments as a plain Python list
        # via prefetch_related's cache — one extra query total for the
        # whole page (`user_id IN (<=50 ids)`), never per student. Runs
        # against the exact instances being serialized (prefetch_related_objects),
        # not a fresh queryset, since those are what the serializer reads.
        from django.db.models import prefetch_related_objects

        prefetch_related_objects(
            target,
            Prefetch(
                'enrollments',
                queryset=Enrollment.objects.select_related('course').order_by('course__prefix'),
                to_attr='browse_enrollments',
            ),
        )

        serializer = AdminStudentBrowseSerializer(target, many=True)
        return paginator.get_paginated_response(serializer.data)


class AdminAccountViewSet(viewsets.ModelViewSet):
    """Super-Admin-only: manage other admin-panel accounts (Super Admin/Admin/Editor)."""
    queryset = User.objects.filter(is_staff=True).order_by('-date_joined')
    serializer_class = AdminAccountSerializer
    permission_classes = [IsSuperAdmin]

    def destroy(self, request, *args, **kwargs):
        """Permanent delete — blocked if this account owns marketplace
        courses (TeacherCourse.teacher is CASCADE: deleting the account
        would silently destroy the course catalog and every enrolled
        student's access) or has payment history (Purchase.user is
        CASCADE — financial records must not be destroyed this way, see
        item 13). Deactivate the account (is_active=False, already
        supported by the existing PATCH endpoint) instead."""
        from core.deletion_audit import record_deletion

        account = self.get_object()
        label = account.email

        if account.taught_courses.exists():
            msg = 'This account owns marketplace course(s) — deleting it would destroy those courses and enrolled students’ access. Transfer ownership or remove the courses first.'
            record_deletion(request, 'AdminAccount', account.id, label, result='failure', failure_reason=msg)
            return Response({'detail': msg}, status=status.HTTP_400_BAD_REQUEST)

        if account.purchases.exists():
            msg = 'This account has payment/purchase history and cannot be permanently deleted — deactivate it instead to preserve financial records.'
            record_deletion(request, 'AdminAccount', account.id, label, result='failure', failure_reason=msg)
            return Response({'detail': msg}, status=status.HTTP_400_BAD_REQUEST)

        try:
            response = super().destroy(request, *args, **kwargs)
        except Exception as exc:
            record_deletion(request, 'AdminAccount', account.id, label, result='failure', failure_reason=str(exc)[:500])
            return Response({'detail': 'Deletion failed. No partial deletion should remain.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        record_deletion(request, 'AdminAccount', account.id, label, result='success')
        return response


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
