from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db.models import Avg, Count, Prefetch, Q, Sum
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from billing.models import Purchase
from courses.models import Enrollment, EnrollmentRequest
from hamromentor.permissions import IsAdminRoleOrAbove, IsSuperAdmin
from tests_app.models import TestAttempt

from . import verification_storage
from .models import Device, RolePermission, StudentProfile, VerificationDocument
from .serializers import (
    AdminAccountSerializer,
    AdminUserDetailSerializer,
    AdminUserSerializer,
    LoginSerializer,
    RegisterSerializer,
    RolePermissionSerializer,
    StudentProfileSerializer,
    UserSerializer,
    VerificationDocumentSerializer,
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
    # 'post' added for the verification/approve and verification/reject
    # @action routes below. That alone would also silently re-enable the
    # router's list-route POST -> create() (a plain ModelViewSet wires
    # 'post' to create() at the collection root regardless of
    # http_method_names — the old list only blocked it at dispatch time),
    # so create() is explicitly disabled again just below to preserve this
    # viewset's original "no account creation here" contract.
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def create(self, request, *args, **kwargs):
        return Response({'detail': 'Method "POST" not allowed.'}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

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

    # Bulk-import-style bound: how many rows of each related collection the
    # detail page gets. Keeps the endpoint's cost flat regardless of how
    # long a student's history is — see the `detail` action's own docstring.
    DETAIL_RELATED_LIMIT = 20

    @action(detail=True, methods=['get'], url_path='detail')
    def student_detail(self, request, pk=None):
        """Admin Student Detail (Phase 1) — a separate, richer read view.
        Deliberately NOT built on top of get_queryset()/get_object(): this
        method builds its own queryset with select_related + bounded,
        select_related'd Prefetch()es for every related collection, so nothing
        here ever touches list()/retrieve()'s query shape or cost. Total
        query count for one call: 1 (user + select_related joins) + 4
        (enrollments/enrollment_requests/purchases/devices, each one bounded
        query) + 1 (recent test attempts) + 2 (QuestionAttempt aggregate,
        TestAttempt aggregate) = 8, independent of how much history the
        student has — verified in accounts/tests.py via CaptureQueriesContext.
        """
        user = self._detail_queryset().filter(pk=pk).first()
        if user is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        user.detail_activity_summary = self._activity_summary(user, self.DETAIL_RELATED_LIMIT)
        return Response(AdminUserDetailSerializer(user).data)

    @classmethod
    def _detail_queryset(cls):
        """The exact select_related/Prefetch shape student_detail uses —
        factored out so student_edit (Phase 2) can build the same
        bounded, N+1-free response after a save without duplicating it."""
        limit = cls.DETAIL_RELATED_LIMIT
        return (
            User.objects.filter(is_staff=False)
            .select_related('profile', 'active_course', 'referred_by')
            .prefetch_related(
                Prefetch(
                    'enrollments',
                    queryset=Enrollment.objects.select_related('course', 'package', 'batch').order_by('-enrolled_at')[:limit],
                    to_attr='detail_enrollments',
                ),
                Prefetch(
                    'enrollment_requests',
                    queryset=EnrollmentRequest.objects.select_related('course', 'package').order_by('-submitted_at')[:limit],
                    to_attr='detail_enrollment_requests',
                ),
                Prefetch(
                    'purchases',
                    queryset=Purchase.objects.select_related(
                        'plan', 'grand_test', 'teacher_course', 'combo_plan', 'decided_by',
                    ).order_by('-created_at')[:limit],
                    to_attr='detail_purchases',
                ),
                Prefetch('devices', queryset=Device.objects.order_by('-last_seen')[:limit], to_attr='detail_devices'),
                Prefetch(
                    'verification_documents',
                    queryset=VerificationDocument.objects.select_related('reviewed_by').order_by('-uploaded_at')[:limit],
                    to_attr='detail_verification_documents',
                ),
            )
        )

    @staticmethod
    def _activity_summary(user, limit):
        """Two aggregate-only queries (no row-per-attempt loading) plus one
        bounded, select_related'd list for 'recent activity' — never
        `.all()` over QuestionAttempt/TestAttempt, both of which are
        platform-wide, unboundedly-growing tables."""
        from academics.models import QuestionAttempt

        qa_stats = QuestionAttempt.objects.filter(user=user).aggregate(
            questions_attempted=Count('id'),
            total_attempts=Sum('attempts_count'),
            total_correct=Sum('correct_count'),
            mastered=Count('id', filter=Q(mastery_status='mastered')),
            weak=Count('id', filter=Q(mastery_status='weak')),
            need_practice=Count('id', filter=Q(mastery_status='need_practice')),
            learning=Count('id', filter=Q(mastery_status='learning')),
            new=Count('id', filter=Q(mastery_status='new')),
        )
        total_attempts = qa_stats['total_attempts'] or 0
        total_correct = qa_stats['total_correct'] or 0
        overall_accuracy_pct = round((total_correct / total_attempts) * 100, 1) if total_attempts else None

        ta_stats = TestAttempt.objects.filter(user=user, status='submitted').aggregate(
            tests_taken=Count('id'),
            avg_score=Avg('score'),
            avg_accuracy=Avg('accuracy'),
        )
        recent_attempts = (
            TestAttempt.objects.filter(user=user).select_related('test').order_by('-start_time')[:limit]
        )

        from .serializers import AdminStudentTestAttemptSerializer

        return {
            'questions_attempted': qa_stats['questions_attempted'] or 0,
            'total_attempts': total_attempts,
            'total_correct': total_correct,
            'overall_accuracy_pct': overall_accuracy_pct,
            'mastery_breakdown': {
                'new': qa_stats['new'] or 0,
                'learning': qa_stats['learning'] or 0,
                'need_practice': qa_stats['need_practice'] or 0,
                'weak': qa_stats['weak'] or 0,
                'mastered': qa_stats['mastered'] or 0,
            },
            'tests_taken': ta_stats['tests_taken'] or 0,
            'avg_score': round(ta_stats['avg_score'], 2) if ta_stats['avg_score'] is not None else None,
            'avg_accuracy': round(ta_stats['avg_accuracy'], 2) if ta_stats['avg_accuracy'] is not None else None,
            'recent_test_attempts': AdminStudentTestAttemptSerializer(recent_attempts, many=True).data,
        }

    @action(detail=True, methods=['patch'], url_path='edit')
    def student_edit(self, request, pk=None):
        """Admin Student Edit (Phase 2) — a dedicated endpoint, deliberately
        separate from list()/retrieve()/partial_update()'s generic PATCH
        (which stays exactly as it was: is_active toggle only). Every
        request key not in AdminStudentEditSerializer's declared fields is
        rejected outright — this is the actual enforcement mechanism behind
        "email/password/username/referral_code/wallet_balance/active_course/
        is_active/enrollments/purchases cannot be edited here", not just a
        convention.

        Concurrency: both rows are locked with select_for_update() inside
        one transaction, so two admins editing the same student at once
        serialize at the DB level (second request's read happens after the
        first's commit) instead of racing on a stale in-Python read —
        see accounts/tests.py's threaded concurrent-edit test.

        Only ever writes the specific columns that actually changed
        (update_fields=[...]) — never a blanket full-row save. Audit log
        entry is written only on genuine success (see core.edit_audit).
        """
        from django.db import transaction

        from core.edit_audit import record_admin_edit

        from .serializers import AdminStudentEditSerializer

        allowed_keys = set(AdminStudentEditSerializer().fields.keys())
        disallowed = set(request.data.keys()) - allowed_keys
        if disallowed:
            return Response(
                {'detail': f"These fields cannot be edited here: {', '.join(sorted(disallowed))}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            user = User.objects.select_for_update().filter(is_staff=False, pk=pk).first()
            if user is None:
                return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
            profile = StudentProfile.objects.select_for_update().filter(user=user).first()
            if profile is None:
                profile = StudentProfile.objects.create(user=user)

            serializer = AdminStudentEditSerializer(data=request.data, partial=True, context={'user_id': user.id})
            serializer.is_valid(raise_exception=True)
            validated = serializer.validated_data

            changed = {}
            user_update_fields = []
            for f in AdminStudentEditSerializer.USER_FIELDS:
                if f not in validated:
                    continue
                old = getattr(user, f)
                new = validated[f]
                if old != new:
                    changed[f] = {'old': old, 'new': new}
                    setattr(user, f, new)
                    user_update_fields.append(f)
            if user_update_fields:
                user.save(update_fields=user_update_fields)

            profile_update_fields = []
            for f in AdminStudentEditSerializer.PROFILE_FIELDS:
                if f not in validated:
                    continue
                old = getattr(profile, f)
                new = validated[f]
                if old != new:
                    changed[f] = {'old': old, 'new': new}
                    setattr(profile, f, new)
                    profile_update_fields.append(f)
            if profile_update_fields:
                profile.save(update_fields=profile_update_fields)

        if changed:
            record_admin_edit(
                request, resource_type='Student', resource_id=user.id, resource_label=user.email,
                changed_fields=changed,
            )

        return Response({
            'id': user.id, 'first_name': user.first_name, 'last_name': user.last_name, 'phone': user.phone,
            'program': user.program, 'course': user.course,
            'college': profile.college, 'district': profile.district, 'province': profile.province,
            'exam_target': profile.exam_target, 'batch': profile.batch,
            'changed_fields': list(changed.keys()),
        })

    @action(detail=True, methods=['post'], url_path='verification/approve')
    def verify_profile(self, request, pk=None):
        """Sets the STUDENT'S OWN profile-level verification_status to
        'verified' — always an explicit admin decision, never automatic
        (see StudentProfile.verification_status's own help_text). This is
        the only write in this whole feature that touches exam-access-
        adjacent code paths at all, and it doesn't: nothing in
        tests_app/access.py, billing/access.py, or courses/access.py reads
        this field — see accounts/tests_verification.py's regression suite."""
        user = User.objects.filter(is_staff=False, pk=pk).select_related('profile').first()
        if user is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        profile, _ = StudentProfile.objects.get_or_create(user=user)
        profile.verification_status = 'verified'
        profile.verification_reviewed_at = timezone.now()
        profile.verification_reviewed_by = request.user
        profile.verification_rejection_reason = ''
        profile.save(update_fields=[
            'verification_status', 'verification_reviewed_at', 'verification_reviewed_by',
            'verification_rejection_reason',
        ])
        return Response(StudentProfileSerializer(profile).data)

    @action(detail=True, methods=['post'], url_path='verification/reject')
    def reject_profile(self, request, pk=None):
        reason = (request.data.get('reason') or '').strip()
        if not reason:
            return Response({'detail': 'A rejection reason is required.'}, status=status.HTTP_400_BAD_REQUEST)
        user = User.objects.filter(is_staff=False, pk=pk).select_related('profile').first()
        if user is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        profile, _ = StudentProfile.objects.get_or_create(user=user)
        profile.verification_status = 'rejected'
        profile.verification_reviewed_at = timezone.now()
        profile.verification_reviewed_by = request.user
        profile.verification_rejection_reason = reason
        profile.save(update_fields=[
            'verification_status', 'verification_reviewed_at', 'verification_reviewed_by',
            'verification_rejection_reason',
        ])
        return Response(StudentProfileSerializer(profile).data)


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


class ProfilePhotoUploadView(APIView):
    """POST /auth/me/photo/ — student uploads/replaces their own profile
    photo. Uses StudentProfile.photo (a plain Django ImageField already
    backed by the project's GCS storage — see media_library/django_storage.py's
    own docstring, which lists 'profile photos' as an intended use case).
    Content-validated the same way payment screenshots are (real image
    decode, not trusting the extension/Content-Type) before ever touching
    storage. The previous photo is only deleted after the new one has
    saved successfully — never the other way around, so a failed upload
    never leaves the student with no photo at all."""
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        file_obj = request.FILES.get('photo')
        if not file_obj:
            return Response({'detail': 'No photo uploaded.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            verification_storage.validate_photo(file_obj)
        except verification_storage.InvalidUpload as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        profile, _ = StudentProfile.objects.get_or_create(user=request.user)
        old_photo = profile.photo if profile.photo else None

        file_obj.seek(0)
        profile.photo.save(file_obj.name, file_obj, save=True)

        if old_photo and old_photo.name != profile.photo.name:
            old_photo.delete(save=False)

        return Response(StudentProfileSerializer(profile).data)


class MyVerificationView(APIView):
    """GET /auth/me/verification/ — the student's own verification status
    and document list (metadata only — no signed URLs; see
    VerificationDocumentViewView for actually viewing one). POST on the
    same path submits a new document. Deliberately NOT folded into
    UserSerializer/MeView: the student app fetches this independently, on
    the Profile page only, so a slow/failed verification fetch can never
    affect login or any exam-access page — see this feature's own
    architecture note in StudentProfile.verification_status's help_text."""
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request):
        profile, _ = StudentProfile.objects.get_or_create(user=request.user)
        documents = VerificationDocument.objects.filter(user=request.user).select_related('reviewed_by')
        return Response({
            'verification_status': profile.verification_status,
            'verification_rejection_reason': profile.verification_rejection_reason,
            'has_photo': bool(profile.photo),
            'documents': VerificationDocumentSerializer(documents, many=True).data,
        })

    def post(self, request):
        document_type = request.data.get('document_type')
        valid_types = dict(VerificationDocument.DOCUMENT_TYPE_CHOICES)
        if document_type not in valid_types:
            return Response({'detail': f'Unknown document_type "{document_type}".'}, status=status.HTTP_400_BAD_REQUEST)
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({'detail': 'No file uploaded.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            bucket, key, content_type, size = verification_storage.store_verification_document(
                file_obj, request.user.id, document_type,
            )
        except verification_storage.InvalidUpload as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        document = VerificationDocument.objects.create(
            user=request.user, document_type=document_type, storage_bucket=bucket, storage_key=key,
            original_filename=file_obj.name[:255], mime_type=content_type, file_size=size,
        )

        # The ONLY automatic transitions in this feature: a submission moves
        # 'unverified' -> 'pending' (first-ever submission) and
        # 'rejected' -> 'pending' (a resubmission after rejection puts the
        # student back in the review queue, since the admin's rejection
        # reason no longer describes what's currently on file). 'pending'
        # stays 'pending' (an additional document while already under
        # review doesn't need a second transition) and 'verified' is left
        # untouched (an already-verified student adding another document
        # is not a resubmission-after-failure — reaching 'rejected' from
        # here is still always a separate, explicit admin action, never
        # automatic, per StudentProfile.verification_status's own contract).
        profile, _ = StudentProfile.objects.get_or_create(user=request.user)
        if profile.verification_status in ('unverified', 'rejected'):
            profile.verification_status = 'pending'
            profile.save(update_fields=['verification_status'])

        return Response(VerificationDocumentSerializer(document).data, status=status.HTTP_201_CREATED)


def _can_view_verification_document(user, document):
    if document.user_id == user.id:
        return True
    return bool(user.is_staff and (user.is_superuser or getattr(user, 'admin_role', None) in (None, '', 'super_admin', 'admin')))


class VerificationDocumentViewView(APIView):
    """GET /auth/verification-documents/{id}/view/ — a short-lived signed
    URL for one document. Owner or admin only (same dual-audience,
    manual-check pattern as billing.views.PurchaseViewSet.screenshot — a
    plain APIView has no per-object DRF permission hook, so the check is
    explicit here, same as that established precedent). Never a redirect
    (avoids the URL landing in access/referrer logs); the URL itself is
    never persisted anywhere, generated fresh on every authorized call."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        document = VerificationDocument.objects.filter(pk=pk).first()
        if document is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not _can_view_verification_document(request.user, document):
            return Response({'detail': 'Not allowed.'}, status=status.HTTP_403_FORBIDDEN)
        url = verification_storage.verification_document_view_url(document.storage_bucket, document.storage_key)
        if not url:
            return Response({'detail': 'This document is not available.'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'url': url})


class VerificationDocumentApproveView(APIView):
    """POST /auth/verification-documents/{id}/approve/ — admin-only,
    per-document. Does not by itself change StudentProfile.verification_status
    (see AdminUserViewSet.verify_profile for the separate, explicit overall
    decision) — approving one document is not the same as verifying the
    whole profile, matching "Do not mark verified just because a file was
    uploaded" applied one level further: approving one file isn't
    verifying the profile either."""
    permission_classes = [IsAdminRoleOrAbove]

    def post(self, request, pk):
        document = VerificationDocument.objects.filter(pk=pk).first()
        if document is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        document.status = 'approved'
        document.rejection_reason = ''
        document.reviewed_at = timezone.now()
        document.reviewed_by = request.user
        document.save(update_fields=['status', 'rejection_reason', 'reviewed_at', 'reviewed_by'])
        return Response(VerificationDocumentSerializer(document).data)


class VerificationDocumentRejectView(APIView):
    """POST /auth/verification-documents/{id}/reject/ — admin-only,
    per-document, reason required (never a silent rejection)."""
    permission_classes = [IsAdminRoleOrAbove]

    def post(self, request, pk):
        reason = (request.data.get('reason') or '').strip()
        if not reason:
            return Response({'detail': 'A rejection reason is required.'}, status=status.HTTP_400_BAD_REQUEST)
        document = VerificationDocument.objects.filter(pk=pk).first()
        if document is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        document.status = 'rejected'
        document.rejection_reason = reason
        document.reviewed_at = timezone.now()
        document.reviewed_by = request.user
        document.save(update_fields=['status', 'rejection_reason', 'reviewed_at', 'reviewed_by'])
        return Response(VerificationDocumentSerializer(document).data)
