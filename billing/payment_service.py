"""
Core payment-lifecycle service — the single place that mutates a Purchase's
status. Every transition is: locked (select_for_update inside
transaction.atomic), guarded (re-checks the precondition under the lock, not
just before it), audited (PaymentAuditLog), and notified (student email).

Any future automated verification path (a webhook-driven FonepayProvider,
KhaltiProvider, etc.) is expected to call activate()/reject() here directly,
exactly as ManualQRProvider's admin-click path does today — see
billing/payment_providers.py. Nothing about product activation (Subscription/
GrandTestAccess/CourseEnrollment) or the coupon/referral side-effects should
ever be duplicated outside this module.
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import Coupon, GrandTestAccess, Purchase, Subscription
from .notifications import send_notification, send_payment_notification
from .payment_audit import record_payment_event
from .screenshot_storage import store_screenshot


class PaymentError(ValidationError):
    """A user-facing payment-flow error (already-used reference, expired
    order, already-decided purchase, etc.) — callers translate this to a 400
    response. Distinct from an unexpected exception, which should propagate
    and roll back the transaction like any other bug."""


def _ensure_enrollment(user, course):
    """A Subscription (billing: "which product you paid for") is a
    different concept from an Enrollment (courses: "which course you're a
    member of, used for every catalog visibility check — subjects,
    chapters, questions, tests, videos — across the whole app"). Buying a
    subscription previously created only the former: a student could pay,
    get an approved Purchase, and still see zero content, because
    courses.access.eligible_course_ids() only ever reads Enrollment. Every
    path that grants a Subscription must also ensure the matching
    Enrollment exists — mirrors EnrollmentRequestViewSet.approve()'s own
    Enrollment.objects.update_or_create(...) exactly, so a paid student
    ends up in the identical state as one an admin manually approved."""
    from courses.models import Enrollment

    Enrollment.objects.update_or_create(
        user=user, course=course,
        defaults={'access_type': 'package', 'is_active': True},
    )


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

    _ensure_enrollment(user, course)

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


def _teacher_course_duration(course):
    if course.access_duration_type == 'lifetime':
        return None
    if course.access_duration_type == 'custom':
        return timedelta(days=course.access_duration_days) if course.access_duration_days else None
    return {
        '30_days': timedelta(days=30),
        '90_days': timedelta(days=90),
        '180_days': timedelta(days=180),
        '1_year': timedelta(days=365),
    }.get(course.access_duration_type)


def _activate_product(purchase):
    """Creates/extends the actual access record for this purchase. Returns
    the access object (Subscription / GrandTestAccess / CourseEnrollment)."""
    if purchase.kind == 'subscription':
        plan = purchase.plan
        subscription, was_renewal = _extend_or_create_subscription(
            purchase.user, plan.course, plan.product_type, plan.duration_timedelta(),
            plan=plan, mock_test_quota=plan.mock_test_quota,
        )
        if was_renewal:
            send_notification(purchase.user, 'renewal_confirmation', subscription)
        return subscription

    if purchase.kind == 'grand_test':
        access, _created = GrandTestAccess.objects.get_or_create(
            user=purchase.user, test=purchase.grand_test, defaults={'purchase': purchase},
        )
        access.purchase = purchase
        access.granted_at = timezone.now()
        access.save()
        _send_grand_test_email(access)
        return access

    if purchase.kind == 'combo':
        subscriptions = []
        for item in purchase.combo_items.select_related('plan'):
            plan = item.plan
            subscription, was_renewal = _extend_or_create_subscription(
                purchase.user, plan.course, plan.product_type, plan.duration_timedelta(),
                plan=plan, mock_test_quota=plan.mock_test_quota,
            )
            if was_renewal:
                send_notification(purchase.user, 'renewal_confirmation', subscription)
            subscriptions.append(subscription)
        return subscriptions

    if purchase.kind == 'teacher_course':
        from marketplace.models import CourseEnrollment

        course = purchase.teacher_course
        duration = _teacher_course_duration(course)
        expires_at = (timezone.now() + duration) if duration else None
        enrollment, created = CourseEnrollment.objects.get_or_create(
            user=purchase.user, course=course,
            defaults={'source': 'purchase', 'expires_at': expires_at, 'is_active': True},
        )
        if not created:
            enrollment.source = 'purchase'
            enrollment.expires_at = expires_at
            enrollment.is_active = True
            enrollment.save()
        return enrollment

    raise PaymentError(f'Unknown purchase kind "{purchase.kind}".')


def _maybe_reward_referrer(purchase):
    """First time a referred student's purchase is approved, credit the
    referrer's wallet — via an atomic UPDATE (not a Python read-modify-write)
    so concurrent approvals for different referred users sharing one
    referrer can never lose an increment."""
    user = purchase.user
    if not user.referred_by_id:
        return
    prior_approved = Purchase.objects.filter(user=user, status='approved').exclude(pk=purchase.pk).exists()
    if prior_approved:
        return
    from accounts.models import User

    User.objects.filter(pk=user.referred_by_id).update(
        wallet_balance=F('wallet_balance') + Decimal(settings.REFERRAL_REWARD_AMOUNT),
    )


def submit(purchase_id, *, payment_method, payment_reference, screenshot_file, request=None):
    """Student submits (or resubmits) proof of payment. Locks the row so a
    duplicate-reference check can't race against a concurrent submission on
    a different purchase reusing the exact same reference."""
    with transaction.atomic():
        purchase = Purchase.objects.select_for_update().get(pk=purchase_id)
        if purchase.status not in ('unpaid', 'resubmission_requested'):
            raise PaymentError('This purchase is not awaiting payment submission.')
        if purchase.status == 'unpaid' and purchase.is_expired:
            raise PaymentError('This payment window has expired — please start a new order.')

        reference = (payment_reference or '').strip()
        if not reference:
            raise PaymentError('A transaction reference is required.')
        duplicate = Purchase.objects.filter(
            payment_reference=reference, status__in=('pending', 'approved'),
        ).exclude(pk=purchase.pk).exists()
        if duplicate:
            raise PaymentError('This transaction reference is already used on another purchase.')

        bucket, key = store_screenshot(screenshot_file)

        previous_status = purchase.status
        purchase.payment_method = payment_method
        purchase.payment_reference = reference
        purchase.payment_screenshot_bucket = bucket
        purchase.payment_screenshot_key = key
        purchase.status = 'pending'
        purchase.admin_note = ''
        purchase.save()

    record_payment_event(
        purchase, 'submitted', previous_status, 'pending', request=request,
        metadata={'payment_reference': reference},
    )
    send_payment_notification(purchase.user, 'payment_submitted', purchase)
    return purchase


def activate(purchase_id, *, actor=None, request=None, allow_unpaid=False):
    """Approves a purchase and activates its product. `allow_unpaid` is only
    ever passed True by the free/100%-off auto-approve path in
    PurchaseViewSet.create() — the admin-facing approve action never sets it,
    so a real (nonzero) order can only ever be approved from 'pending', i.e.
    after proof was actually submitted."""
    with transaction.atomic():
        purchase = Purchase.objects.select_for_update().get(pk=purchase_id)
        allowed_statuses = ('pending', 'unpaid') if allow_unpaid else ('pending',)
        if purchase.status not in allowed_statuses:
            raise PaymentError('This purchase has already been decided.')
        if purchase.payment_reference and Purchase.objects.filter(
            payment_reference=purchase.payment_reference, status='approved',
        ).exclude(pk=purchase.pk).exists():
            raise PaymentError('This payment reference has already been approved for another order.')

        previous_status = purchase.status
        _activate_product(purchase)

        purchase.status = 'approved'
        purchase.decided_at = timezone.now()
        purchase.decided_by = actor
        purchase.save()

        if purchase.coupon_id:
            Coupon.objects.filter(pk=purchase.coupon_id).update(usage_count=F('usage_count') + 1)
        _maybe_reward_referrer(purchase)

    record_payment_event(purchase, 'approved', previous_status, 'approved', request=request, actor=actor)
    send_payment_notification(purchase.user, 'payment_approved', purchase)
    return purchase


def reject(purchase_id, reason, *, actor=None, request=None):
    reason = (reason or '').strip()
    if not reason:
        raise PaymentError('A reason is required to reject a purchase.')
    with transaction.atomic():
        purchase = Purchase.objects.select_for_update().get(pk=purchase_id)
        if purchase.status not in ('pending', 'resubmission_requested'):
            raise PaymentError('This purchase has already been decided.')
        previous_status = purchase.status
        purchase.status = 'rejected'
        purchase.admin_note = reason
        purchase.decided_at = timezone.now()
        purchase.decided_by = actor
        purchase.save()

    record_payment_event(purchase, 'rejected', previous_status, 'rejected', request=request, actor=actor, reason=reason)
    send_payment_notification(purchase.user, 'payment_rejected', purchase)
    return purchase


def request_resubmission(purchase_id, reason, *, actor=None, request=None):
    reason = (reason or '').strip()
    if not reason:
        raise PaymentError('A reason is required when requesting new proof.')
    with transaction.atomic():
        purchase = Purchase.objects.select_for_update().get(pk=purchase_id)
        if purchase.status != 'pending':
            raise PaymentError('This purchase is not awaiting verification.')
        previous_status = purchase.status
        purchase.status = 'resubmission_requested'
        purchase.admin_note = reason
        purchase.save()

    record_payment_event(
        purchase, 'resubmission_requested', previous_status, 'resubmission_requested',
        request=request, actor=actor, reason=reason,
    )
    return purchase


def expire(purchase_id):
    """Idempotent — safe to call twice on the same purchase (the cron sweep
    doesn't need to worry about re-processing a row it already handled)."""
    with transaction.atomic():
        purchase = Purchase.objects.select_for_update().get(pk=purchase_id)
        if purchase.status != 'unpaid' or not purchase.is_expired:
            return purchase
        previous_status = purchase.status
        purchase.status = 'expired'
        purchase.save(update_fields=['status'])

    record_payment_event(purchase, 'expired', previous_status, 'expired')
    send_payment_notification(purchase.user, 'payment_expired', purchase)
    return purchase


def cancel(purchase_id, *, actor=None, request=None):
    with transaction.atomic():
        purchase = Purchase.objects.select_for_update().get(pk=purchase_id)
        if purchase.status not in ('unpaid', 'resubmission_requested'):
            raise PaymentError('This purchase can no longer be cancelled.')
        previous_status = purchase.status
        purchase.status = 'cancelled'
        purchase.save(update_fields=['status'])

    record_payment_event(purchase, 'cancelled', previous_status, 'cancelled', request=request, actor=actor)
    return purchase
