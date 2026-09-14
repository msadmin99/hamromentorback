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


def _extend_or_create_subscription(
    user, course, product_type, duration, plan=None, mock_test_quota=None, is_scholarship=False,
    record_grant_for=None,
):
    """Shared by purchase activation and the admin manual-grant/scholarship
    endpoint — extends an existing active subscription for the same
    user+course+product rather than creating a duplicate row, exactly like a
    real renewal. Returns (subscription, was_renewal).

    FIX (Phase 2 Entitlement Foundation, Step 17): the "existing" lookup
    used to consider ANY active subscription for (user, course,
    product_type), regardless of origin — a scholarship grant could
    silently extend/attach to an already-paid subscription row (or vice
    versa), so revoking the scholarship later would deactivate the same
    row backing the student's paid access (the audit's confirmed
    "shared-row merge" risk). Now scoped by origin: a scholarship call only
    extends an existing scholarship-linked row; a non-scholarship call only
    extends an existing non-scholarship row. Cross-origin calls always
    create a NEW, independent row instead of merging — safe by design,
    since every has_*_access() check already only asks "does ANY active
    matching row exist," so two rows for the same (user, course,
    product_type) is a supported, correct state (Step 10: multiple valid
    entitlements coexist; one being revoked never cancels the other).

    Phase 9: `record_grant_for=<Purchase>` additionally writes the
    PurchaseEntitlementGrant row that makes this grant reversible by a later
    refund — recorded here, not in the caller, because this is the only place
    that knows whether the row was created or extended and what its expiry
    was beforehand. Defaults to None (no ledger row), so the scholarship and
    admin-manual-grant callers behave exactly as before."""
    now = timezone.now()
    existing = Subscription.objects.filter(
        user=user, course=course, product_type=product_type, is_active=True,
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gte=now)).filter(
        scholarship__isnull=not is_scholarship,
    ).first()

    start_from = existing.expires_at if (existing and existing.expires_at and existing.expires_at > now) else now
    expires_at = start_from + duration if duration else None

    _ensure_enrollment(user, course)

    if existing:
        previous_expires_at = existing.expires_at
        existing.expires_at = expires_at
        if plan:
            existing.plan = plan
        if mock_test_quota is not None:
            existing.mock_test_quota = (existing.mock_test_quota or 0) + mock_test_quota
        existing.save()
        _record_grant(
            record_grant_for, subscription=existing, was_created=False,
            previous_expires_at=previous_expires_at, granted_expires_at=expires_at,
        )
        return existing, True

    subscription = Subscription.objects.create(
        user=user, plan=plan, course=course, product_type=product_type,
        expires_at=expires_at, mock_test_quota=mock_test_quota,
    )
    _record_grant(
        record_grant_for, subscription=subscription, was_created=True,
        previous_expires_at=None, granted_expires_at=expires_at,
    )
    return subscription, False


def _record_grant(purchase, **fields):
    """Phase 9 — writes one PurchaseEntitlementGrant row, or does nothing when
    `purchase` is None (every non-purchase grant path: scholarships, admin
    manual grants). Always called inside the caller's transaction."""
    if purchase is None:
        return None
    from .models import PurchaseEntitlementGrant

    return PurchaseEntitlementGrant.objects.create(purchase=purchase, **fields)


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


def _send_grand_test_package_email(purchase, accesses):
    """GT3-5 — one consolidated confirmation for a whole package purchase,
    reusing the exact same send_mail pattern _send_grand_test_email above
    already uses (no new email infrastructure, per the GT3-5 spec's own
    'do not make a broad email-system rewrite' instruction). Deliberately
    does NOT mention a password at all — unlike the older per-test email
    above (left untouched, a GT3-3-disclosed, deferred cleanup item), this
    is new code written correctly against GT3-3's actual current behavior
    from the start: an entitled student never needs one.

    Only ever called with the NEWLY-activated accesses for this purchase
    (see _activate_product's own §22 guard) — a test the student already
    owned is never re-announced here."""
    user = accesses[0].user
    lines = [
        f'Hi {user.first_name or user.email},',
        '',
        'Your payment has been confirmed. Your Grand Test package is now active:',
        '',
    ]
    for access in sorted(accesses, key=lambda a: a.test.scheduled_start or timezone.now()):
        test = access.test
        line = f'- {test.title}'
        if test.scheduled_start and test.scheduled_end:
            line += f' — {test.scheduled_start.strftime("%d %b %Y, %I:%M %p")} to {test.scheduled_end.strftime("%I:%M %p")}'
        lines.append(line)
    lines += [
        '',
        'No password is needed — each exam unlocks automatically once you sign in during its scheduled time.',
        'Each exam can only be attempted during its own scheduled window and cannot be retaken once it closes.',
        '',
        'Good luck!',
        'Dr. Gutka Support',
    ]
    send_mail(
        f'Your Grand Test package is active — {purchase.grand_test_package.name if purchase.grand_test_package_id else "Grand Test Package"}',
        '\n'.join(lines), settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=True,
    )
    now = timezone.now()
    for access in accesses:
        access.email_sent_at = now
    GrandTestAccess.objects.bulk_update(accesses, ['email_sent_at'])


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
            plan=plan, mock_test_quota=plan.mock_test_quota, record_grant_for=purchase,
        )
        if was_renewal:
            send_notification(purchase.user, 'renewal_confirmation', subscription)
        return subscription

    if purchase.kind == 'grand_test':
        access, created = GrandTestAccess.objects.get_or_create(
            user=purchase.user, test=purchase.grand_test, defaults={'purchase': purchase},
        )
        access.purchase = purchase
        access.granted_at = timezone.now()
        # Re-granting after an earlier refund of the same (user, test) clears
        # the old revocation — the student paid again, so the row is live again.
        access.revoked_at = None
        access.save()
        _record_grant(purchase, grand_test_access=access, was_created=created)
        _send_grand_test_email(access)
        return access

    if purchase.kind == 'combo':
        subscriptions = []
        for item in purchase.combo_items.select_related('plan'):
            plan = item.plan
            subscription, was_renewal = _extend_or_create_subscription(
                purchase.user, plan.course, plan.product_type, plan.duration_timedelta(),
                plan=plan, mock_test_quota=plan.mock_test_quota, record_grant_for=purchase,
            )
            if was_renewal:
                send_notification(purchase.user, 'renewal_confirmation', subscription)
            subscriptions.append(subscription)
        return subscriptions

    if purchase.kind == 'grand_test_package':
        # GT3-5: one commercial purchase, N independent GrandTestAccess
        # rows — never a single "5 attempts" grant. Runs inside the SAME
        # transaction.atomic()/select_for_update() block activate() already
        # wraps every purchase in, so a failure partway through creating
        # the entitlements rolls back the whole activation — no half-
        # activated package is ever left behind (GT3-5 spec §12/§35;
        # nothing new needed here, the existing activate() lock already
        # provides this for every purchase kind).
        accesses = []
        newly_activated = []
        for item in purchase.grand_test_package_items.select_related('test'):
            # No `purchase` in defaults — GrandTestAccess.purchase is a
            # OneToOneField and this same `purchase` row will be reused
            # across every item in this loop, which that field's own
            # uniqueness constraint cannot hold (see its own docstring).
            # PurchaseEntitlementGrant, written below, is the real link.
            access, created = GrandTestAccess.objects.get_or_create(user=purchase.user, test=item.test)
            # GT3-5 §22: unlike the single grand_test branch above (which
            # unconditionally re-stamps/re-emails even for an already-
            # active entitlement — a harmless no-op there since it's one
            # purchase, one test), a 5-item package makes that same
            # unconditional refresh a real problem: buying a package that
            # happens to include a Grand Test the student already owns
            # individually would otherwise re-email them and write a
            # redundant grant row for that one item. Only a genuinely NEW
            # or previously-REVOKED access is (re)activated here; an
            # already-valid one is left completely untouched — but the
            # PurchaseGrandTestPackageItem row for it (created at order
            # time, see PurchaseViewSet.create()) still exists, so order
            # history is never lost even for the untouched item.
            if created or access.revoked_at is not None:
                access.granted_at = timezone.now()
                access.revoked_at = None
                access.save()
                _record_grant(purchase, grand_test_access=access, was_created=created)
                newly_activated.append(access)
            accesses.append(access)
        if newly_activated:
            _send_grand_test_package_email(purchase, newly_activated)
        return accesses

    if purchase.kind == 'teacher_course':
        from marketplace.models import CourseEnrollment

        course = purchase.teacher_course
        duration = _teacher_course_duration(course)
        expires_at = (timezone.now() + duration) if duration else None
        enrollment, created = CourseEnrollment.objects.get_or_create(
            user=purchase.user, course=course,
            defaults={'source': 'purchase', 'expires_at': expires_at, 'is_active': True},
        )
        previous_expires_at = None if created else enrollment.expires_at
        if not created:
            enrollment.source = 'purchase'
            enrollment.expires_at = expires_at
            enrollment.is_active = True
            enrollment.save()
        _record_grant(
            purchase, course_enrollment=enrollment, was_created=created,
            previous_expires_at=previous_expires_at, granted_expires_at=expires_at,
        )
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

        # Phase 9 — redemption-time coupon enforcement. Until now the only
        # limit checks lived at Purchase *creation* (billing/views.py's
        # _apply_discount), which cannot cap anything: creation and approval
        # are separated by a human review step, so N students could each
        # create an order against a max_uses=1 coupon while usage_count was
        # still 0 and every one of them would later be approved. That is an
        # over-redemption even with zero concurrency, and a classic TOCTOU
        # race on top of it. Redemption is here, so the cap belongs here too.
        #
        # select_for_update() on the Coupon serializes concurrent approvals of
        # the same coupon, so the check below and the increment further down
        # are one atomic unit — the (max_uses + 1)th approval always loses.
        if purchase.coupon_id:
            coupon = Coupon.objects.select_for_update().get(pk=purchase.coupon_id)
            if coupon.max_uses is not None and coupon.usage_count >= coupon.max_uses:
                raise PaymentError(
                    f'Coupon "{coupon.code}" has reached its maximum number of redemptions and can no longer '
                    'be applied to this order.'
                )
            if coupon.max_uses_per_user:
                already_redeemed = Purchase.objects.filter(
                    user_id=purchase.user_id, coupon_id=coupon.pk, status='approved',
                ).exclude(pk=purchase.pk).count()
                if already_redeemed >= coupon.max_uses_per_user:
                    raise PaymentError(
                        f'This student has already redeemed coupon "{coupon.code}" the maximum number of times.'
                    )

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
    from notifications.billing_integration import notify_payment_approved
    notify_payment_approved(purchase)
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


def _grant_contribution(grant, record_start):
    """How much time THIS grant added to the access record — the amount a
    refund has to take back. `record_start` is where its own coverage began
    (the row's start for a grant that created it, the previous expiry for one
    that extended it). None when the grant made the access unlimited, which
    can't be expressed as a subtraction."""
    if grant.granted_expires_at is None:
        return None  # lifetime grant — no finite contribution to subtract
    baseline = grant.previous_expires_at or record_start
    if baseline is None:
        return None
    return grant.granted_expires_at - baseline


def _reverse_access_record(grant, record, other_live_grants, expiry_field='expires_at', record_start=None):
    """Shared reversal for the two time-windowed access records (Subscription,
    marketplace CourseEnrollment).

    The rule, and why it is not simply "deactivate what this purchase
    created": several purchases can accumulate onto ONE row — a combo creates
    it, a later direct purchase extends it, a renewal extends it again.
    Deactivating the row because *this* purchase happened to be the one that
    created it would destroy the other purchases' separately-paid-for time,
    which is exactly what a refund must never do. So:

      * this is the only live grant on the row, and it created it → the row
        exists solely because of this purchase → deactivate it.
      * otherwise → subtract only this grant's own contributed duration from
        the current expiry, leaving every other purchase's time in place. The
        row stays active; it just covers less time.
    """
    label = f'{record._meta.model_name} #{record.pk}'
    if not other_live_grants and grant.was_created:
        record.is_active = False
        record.save(update_fields=['is_active'])
        return f'{label} deactivated'

    current_expiry = getattr(record, expiry_field)
    contribution = _grant_contribution(grant, record_start)
    if contribution is None or current_expiry is None:
        # A lifetime grant, or a row another purchase made unlimited — there
        # is no arithmetic that removes only this purchase's share without
        # taking someone else's access with it. Leave it and say so, rather
        # than guess.
        return f'{label} left unchanged (shared with another purchase and not time-bounded)'

    setattr(record, expiry_field, current_expiry - contribution)
    record.save(update_fields=[expiry_field])
    return f'{label} expiry reduced by {contribution} to {getattr(record, expiry_field)}'


def _reverse_grant(grant):
    """Undo exactly one PurchaseEntitlementGrant. Returns a short description
    of what was reversed (for the audit metadata), or None if it was already
    reversed / there is nothing left to reverse."""
    from .models import PurchaseEntitlementGrant

    if grant.revoked_at:
        return None  # already reversed — idempotent

    def _other_live_grants(**lookup):
        return PurchaseEntitlementGrant.objects.filter(
            revoked_at__isnull=True, **lookup,
        ).exclude(pk=grant.pk).exists()

    if grant.subscription_id and grant.subscription:
        subscription = grant.subscription
        return _reverse_access_record(
            grant, subscription,
            other_live_grants=_other_live_grants(subscription_id=grant.subscription_id),
            record_start=subscription.starts_at,
        )

    if grant.grand_test_access_id and grant.grand_test_access:
        access = grant.grand_test_access
        access.revoked_at = timezone.now()
        access.save(update_fields=['revoked_at'])
        return f'grand test access #{access.pk} revoked'

    if grant.course_enrollment_id and grant.course_enrollment:
        enrollment = grant.course_enrollment
        return _reverse_access_record(
            grant, enrollment,
            other_live_grants=_other_live_grants(course_enrollment_id=grant.course_enrollment_id),
            record_start=enrollment.enrolled_at,
        )

    return None  # the granted object was deleted since — nothing left to undo


def refund(purchase_id, reason, *, actor=None, request=None):
    """Phase 9 — mark an approved purchase refunded and reverse exactly the
    entitlements it granted.

    What it reverses: only this purchase's own PurchaseEntitlementGrant rows
    (see that model). Nothing else is touched — not another purchase's
    subscription, not a scholarship, not a combo the student also bought, not
    Free Starter quota, and not the shared `courses.Enrollment` row
    (deliberately: `_ensure_enrollment` maintains one Enrollment per
    (user, course) shared by every source, so deactivating it here would break
    access the student still legitimately holds through some other purchase or
    scholarship. Enrollment governs catalog visibility, not paid entitlement —
    every has_*_access() check requires a live Subscription, which this
    function does reverse).

    What it never touches: finalized attempts, results, rankings, or Phase 8
    snapshots. Refunding money does not rewrite exam history.

    Idempotent: a second call on an already-refunded purchase is a no-op
    returning the same row, and each grant carries its own `revoked_at` guard
    so no reversal is ever applied twice.

    The coupon's `usage_count` is deliberately NOT decremented — see
    docs/PHASE_9_ARCHITECTURE.md. That keeps the `max_uses` cap a hard
    ceiling that a refund cycle can never be used to farm past; if the
    business would rather return the slot, that's a one-line change there,
    made deliberately rather than by default."""
    reason = (reason or '').strip()
    if not reason:
        raise PaymentError('A reason is required to refund a purchase.')

    reversals = []
    with transaction.atomic():
        purchase = Purchase.objects.select_for_update().get(pk=purchase_id)
        if purchase.status == 'refunded':
            return purchase  # idempotent — already done
        if purchase.status != 'approved':
            raise PaymentError('Only an approved purchase can be refunded.')

        previous_status = purchase.status
        # No extra row lock needed on the grants themselves: they are only
        # ever reached through their purchase, whose row is already locked
        # above, so two concurrent refunds of the same purchase serialize
        # there (the second then returns early on status == 'refunded').
        grants = purchase.entitlement_grants.select_related(
            'subscription', 'grand_test_access', 'course_enrollment',
        )
        for grant in grants:
            if grant.revoked_at:
                continue  # already reversed — never re-stamp or re-apply
            description = _reverse_grant(grant)
            if description:
                reversals.append(description)
            grant.revoked_at = timezone.now()
            grant.save(update_fields=['revoked_at'])

        purchase.status = 'refunded'
        purchase.refunded_at = timezone.now()
        purchase.refunded_by = actor
        purchase.refund_reason = reason
        purchase.save(update_fields=['status', 'refunded_at', 'refunded_by', 'refund_reason'])

    record_payment_event(
        purchase, 'refunded', previous_status, 'refunded', request=request, actor=actor, reason=reason,
        metadata={'reversed': reversals, 'grants_processed': len(reversals)},
    )
    return purchase
