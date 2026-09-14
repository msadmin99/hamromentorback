from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from courses.models import Course

from . import email_adapter, push_service, services
from .models import Notification, NotificationPreference
from hamromentor.permissions import IsAdminRoleOrAbove
from .serializers import (
    NotificationPreferenceSerializer,
    NotificationSerializer,
    PushSubscriptionRegisterSerializer,
    PushSubscriptionSerializer,
)


class _NotificationPagination(PageNumberPagination):
    """Same shape as academics._BoundedListPagination — a real page size a
    notification drawer would actually page through, capped so an
    unfiltered request can't materialize a student's entire history."""
    page_size = 20
    max_page_size = 100


class MyNotificationsView(APIView):
    """GET /api/notifications/mine/ — the authenticated student's OWN
    notifications only. Never accepts or trusts a client-supplied user id
    (architecture prompt §39: "user A reading user B notifications" is an
    explicit mandatory security test) — the queryset is always scoped to
    request.user, with no id-based lookup path at all.

    Optional filters: ?course=<id>, ?category=<category>, ?unread=true."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Notification.objects.filter(user=request.user).select_related('course')

        course_id = request.query_params.get('course')
        if course_id:
            qs = qs.filter(course_id=course_id)

        category = request.query_params.get('category')
        if category:
            qs = qs.filter(category=category)

        if request.query_params.get('unread') == 'true':
            qs = qs.filter(read_at__isnull=True)

        paginator = _NotificationPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(NotificationSerializer(page, many=True).data)


class UnreadCountView(APIView):
    """GET /api/notifications/unread-count/ — the header bell badge's data
    source (Phase 2's frontend). Split out from MyNotificationsView so the
    badge can poll cheaply without paginating/serializing a full list on
    every check."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        count = Notification.objects.filter(user=request.user, read_at__isnull=True).count()
        return Response({'unread_count': count})


class MarkNotificationReadView(APIView):
    """POST /api/notifications/<id>/read/ — ownership enforced via the
    queryset itself (get_object_or_404(..., user=request.user)), the same
    IDOR-safe pattern already used by entitlements.MyEntitlementsView:
    a notification belonging to a different user 404s, it never leaks
    "exists but isn't yours" through a 403."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        notification = get_object_or_404(Notification, pk=pk, user=request.user)
        services.mark_read(notification)
        return Response(NotificationSerializer(notification).data)


class MarkAllReadView(APIView):
    """POST /api/notifications/mark-all-read/ — optionally scoped to one
    course via {"course": <id>} in the body, matching the course filter
    already offered on the list endpoint."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        course_id = request.data.get('course')
        course = get_object_or_404(Course, pk=course_id) if course_id else None
        updated = services.mark_all_read(request.user, course=course)
        return Response({'updated': updated})


class NotificationClickView(APIView):
    """POST /api/notifications/<id>/click/ — records the click (and implied
    read) and returns the notification's own structured action_url for the
    frontend to navigate to. Per architecture prompt §16: "Do not make the
    frontend reconstruct the destination from arbitrary notification
    text" — the destination is always this stored field, never derived
    client-side from title/body."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        notification = get_object_or_404(Notification, pk=pk, user=request.user)
        services.mark_clicked(notification)
        return Response({'action_url': notification.action_url})


class MyPreferencesView(APIView):
    """GET/PUT /api/notifications/preferences/ — the authenticated
    student's own preference rows only (same ownership discipline as every
    other view here). PUT replaces the full set the client sends; any
    (category, channel, course) combination not included is left
    untouched — this is a partial-replace-by-key operation, not a
    delete-everything-then-recreate, so an old row this client never
    fetched can't be silently dropped."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = NotificationPreference.objects.filter(user=request.user)
        return Response(NotificationPreferenceSerializer(rows, many=True).data)

    def put(self, request):
        items = request.data if isinstance(request.data, list) else request.data.get('preferences', [])
        saved = []
        errors = []
        for item in items:
            course_id = item.get('course')
            existing = NotificationPreference.objects.filter(
                user=request.user, category=item.get('category'), channel=item.get('channel'), course_id=course_id,
            ).first()
            serializer = NotificationPreferenceSerializer(instance=existing, data=item)
            if not serializer.is_valid():
                errors.append(serializer.errors)
                continue
            serializer.save(user=request.user)
            saved.append(serializer.data)
        if errors:
            return Response({'saved': saved, 'errors': errors}, status=status.HTTP_400_BAD_REQUEST)
        return Response(saved)


def _check_cron_secret(request):
    """Identical guard to billing.views._check_cron_secret /
    courses.views.PruneExpiredPackagesView's inline check /
    tests_app.views.FinalizeExpiredAttemptsView's — re-implemented here
    rather than imported, matching this codebase's existing convention
    (Phase 0 audit, section E) of each app owning its own copy rather than
    a shared cross-app import. Fails closed on an unconfigured secret."""
    if not settings.CRON_SECRET:
        return False
    provided = request.headers.get('X-Cron-Secret') or request.query_params.get('secret')
    return provided == settings.CRON_SECRET


class DispatchScheduledNotificationsView(APIView):
    """POST /api/cron/dispatch-scheduled-notifications/ — meant to be hit
    by an external scheduler (Cloud Scheduler), same shared-secret pattern
    as every other /api/cron/* endpoint in this codebase. Calls
    services.dispatch_due_notifications(), which is itself idempotent, so
    a duplicate scheduler execution is safe."""
    permission_classes = [AllowAny]

    def post(self, request):
        if not _check_cron_secret(request):
            return Response({'detail': 'Invalid or missing cron secret.'}, status=status.HTTP_401_UNAUTHORIZED)
        counts = services.dispatch_due_notifications()
        return Response(counts)


# =====================================================================
# Phase 3 — Web Push
# =====================================================================

class VapidPublicKeyView(APIView):
    """GET /api/notifications/push/vapid-public-key/ — deliberately
    unauthenticated: a VAPID PUBLIC key is not sensitive by design (every
    subscribing browser is handed it) and the frontend needs it before a
    student has necessarily done anything else on this load, to call
    `pushManager.subscribe({applicationServerKey})`. The matching PRIVATE
    key never appears in any view, serializer, or response anywhere in
    this app — see notifications/webpush_adapter.py, the only file that
    reads settings.VAPID_PRIVATE_KEY."""
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({'public_key': settings.VAPID_PUBLIC_KEY})


class PushSubscribeView(APIView):
    """POST /api/notifications/push/subscribe/ — register or re-register
    (P0-02) this browser's push subscription for the authenticated user.
    `user` is always `request.user` (P0-04) — never taken from the body."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = PushSubscriptionRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        subscription = push_service.register_subscription(
            request.user,
            endpoint=data['endpoint'],
            p256dh=data['keys']['p256dh'],
            auth=data['keys']['auth'],
            device_label=data.get('device_label', ''),
            user_agent=request.META.get('HTTP_USER_AGENT', ''),
        )
        return Response(PushSubscriptionSerializer(subscription).data, status=status.HTTP_201_CREATED)


class PushUnsubscribeView(APIView):
    """POST /api/notifications/push/unsubscribe/ — revoke this browser's
    own subscription. Ownership-scoped inside push_service.
    revoke_subscription itself (P0-04) — a different user's endpoint
    simply matches no row, never a cross-user mutation."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        endpoint = request.data.get('endpoint', '')
        if not endpoint:
            return Response({'detail': 'endpoint is required.'}, status=status.HTTP_400_BAD_REQUEST)
        revoked = push_service.revoke_subscription(request.user, endpoint)
        return Response({'revoked': revoked})


class PushDevicesView(APIView):
    """GET /api/notifications/push/devices/ — the authenticated student's
    OWN registered browsers only (P0-04/P1-02) — never raw endpoint/keys
    (PushSubscriptionSerializer excludes them)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        subscriptions = push_service.list_subscriptions(request.user)
        return Response(PushSubscriptionSerializer(subscriptions, many=True).data)


class PushProcessDeliveryView(APIView):
    """POST /api/notifications/push/process/ — the Cloud Tasks callback
    target (push_tasks.enqueue_push_delivery_task), protected by its own
    dedicated secret header, matching this codebase's existing per-queue
    callback convention (X-Media-Processing-Secret, X-Import-Processing-
    Secret, etc. — never the cron-secret convention, which is for
    scheduler-driven sweeps, a different pattern this is not). Returns
    500 on an incomplete run (a temporary failure remains) so Cloud
    Tasks' own retry policy redelivers the task — see push_tasks.py's own
    module docstring for why this reuses Cloud Tasks' native retry rather
    than a bespoke sweep."""
    permission_classes = [AllowAny]

    def post(self, request):
        provided = request.headers.get('X-Push-Processing-Secret')
        if not settings.PUSH_PROCESSING_SECRET or provided != settings.PUSH_PROCESSING_SECRET:
            return Response({'detail': 'Invalid or missing push processing secret.'}, status=status.HTTP_401_UNAUTHORIZED)

        delivery_id = request.data.get('delivery_id')
        if not delivery_id:
            return Response({'detail': 'delivery_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        from . import push_tasks
        complete = push_tasks.process_push_delivery(delivery_id)
        return Response({'complete': complete}, status=status.HTTP_200_OK if complete else status.HTTP_500_INTERNAL_SERVER_ERROR)


# =====================================================================
# Phase 4 — Email
# =====================================================================

class AdminEmailTestThrottle(UserRateThrottle):
    scope = 'admin_email_test_send'

    def get_rate(self):
        return '3/hour'


class AdminEmailTestSendView(APIView):
    """Administrator-only verification endpoint for one real Postmark send.

    Safety controls: admin/super-admin permission, explicit confirmation
    phrase, per-user rate limit, and an optional production allow-list from
    EMAIL_TEST_ALLOWED_RECIPIENTS. The endpoint never exposes the Postmark
    server token.
    """
    permission_classes = [IsAdminRoleOrAbove]
    throttle_classes = [AdminEmailTestThrottle]

    def post(self, request):
        if request.data.get('confirmation') != 'SEND_TEST_EMAIL':
            return Response(
                {'detail': 'confirmation must be exactly SEND_TEST_EMAIL.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        to_email = (request.data.get('to_email') or '').strip().lower()
        if not to_email:
            return Response({'detail': 'to_email is required.'}, status=status.HTTP_400_BAD_REQUEST)

        from django.core.exceptions import ValidationError
        from django.core.validators import validate_email
        try:
            validate_email(to_email)
        except ValidationError:
            return Response({'detail': 'to_email must be a valid email address.'}, status=status.HTTP_400_BAD_REQUEST)

        allowed = getattr(settings, 'EMAIL_TEST_ALLOWED_RECIPIENTS', ())
        if not settings.DEBUG and (not allowed or to_email not in allowed):
            return Response(
                {'detail': 'This recipient is not in EMAIL_TEST_ALLOWED_RECIPIENTS.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        result = email_adapter.send(
            to_email,
            'Dr. Gutka — Postmark email test',
            'This is a controlled administrator test email from Dr. Gutka.\n',
            '<p>This is a controlled administrator test email from <strong>Dr. Gutka</strong>.</p>',
        )
        if result.outcome == email_adapter.EmailSendResult.OUTCOME_SENT:
            return Response(
                {'status': 'sent', 'message_id': result.provider_message_id},
                status=status.HTTP_200_OK,
            )
        return Response(
            {'status': result.outcome, 'error_code': result.error_code, 'detail': result.error_message},
            status=status.HTTP_502_BAD_GATEWAY,
        )


class EmailProcessDeliveryView(APIView):
    """POST /api/notifications/email/process/ — the Cloud Tasks callback
    target (email_tasks.enqueue_email_delivery_task), protected by its
    own dedicated secret header — same per-queue callback convention as
    PushProcessDeliveryView (never the cron-secret convention). Returns
    500 on an incomplete run (a temporary failure remains, retry left)
    so Cloud Tasks' own retry policy redelivers the task — see
    email_tasks.py's own module docstring for the real idempotency
    guarantee this relies on (a conditional-UPDATE claim on
    NotificationDelivery itself, not a child table like push's)."""
    permission_classes = [AllowAny]

    def post(self, request):
        provided = request.headers.get('X-Email-Processing-Secret')
        if not settings.EMAIL_PROCESSING_SECRET or provided != settings.EMAIL_PROCESSING_SECRET:
            return Response({'detail': 'Invalid or missing email processing secret.'}, status=status.HTTP_401_UNAUTHORIZED)

        delivery_id = request.data.get('delivery_id')
        if not delivery_id:
            return Response({'detail': 'delivery_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        from . import email_tasks
        complete = email_tasks.process_email_delivery(delivery_id)
        return Response({'complete': complete}, status=status.HTTP_200_OK if complete else status.HTTP_500_INTERNAL_SERVER_ERROR)
