"""Pure aggregation functions for the admin Analytics Dashboard — no new data
collection, everything is derived from existing Purchase/Subscription/Coupon/
StudentProfile rows.

Definitions are honestly reinterpreted for this platform's manual-payment,
no-gateway, no-formal-trial model (each is documented on the function that
computes it, and echoed back in the API response as a `note`):

- MRR/ARR come from *active, non-scholarship* Subscriptions' plan price,
  normalized to a monthly figure. Purchase-derived figures (popular plans,
  coupon usage, LTV, ARPU) already exclude scholarships for free, since
  GrantAccessView never creates a Purchase row for a scholarship grant.
- "Trial-to-paid conversion" -> free-to-paid conversion: no formal trial
  exists, so this is registered users who made >=1 approved purchase.
- "Payment success/failure" -> payment review outcomes: purchases are
  manually verified (bank/eSewa/Khalti reference), not live gateway
  callbacks, so this is the pending/approved/rejected distribution.
- Churn/renewal rate are period-scoped: of the subscriptions that expired
  in the period, how many got a renewal_confirmation notification (i.e. were
  actually renewed) vs not.
- Geographic distribution uses StudentProfile.province, which is free-text
  (not a normalized choice field) — flagged in the response.
"""
from django.db.models import Count, Q, Sum
from django.utils import timezone

from .models import NotificationLog, Purchase, Subscription


def _plan_monthly_price(plan):
    days = plan.duration_timedelta().days or 1
    months = days / 30.0
    return float(plan.price) / months if months else 0.0


def subscription_counts():
    now = timezone.now()
    active = Subscription.objects.filter(is_active=True).filter(Q(expires_at__isnull=True) | Q(expires_at__gte=now))
    expired = Subscription.objects.filter(is_active=True, expires_at__lt=now)
    return {'active': active.count(), 'expired': expired.count()}


def revenue_summary():
    """MRR/ARR from active, non-scholarship subscriptions with a priced plan."""
    now = timezone.now()
    active = (
        Subscription.objects.filter(is_active=True, plan__isnull=False, scholarship__isnull=True)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gte=now))
        .select_related('plan')
    )
    mrr = sum(_plan_monthly_price(sub.plan) for sub in active)
    return {'mrr': round(mrr, 2), 'arr': round(mrr * 12, 2)}


def conversion_metrics():
    from accounts.models import User

    total_students = User.objects.filter(is_staff=False).count()
    paying_users = Purchase.objects.filter(status='approved').values('user').distinct().count()
    free_to_paid = (paying_users / total_students * 100) if total_students else 0.0
    return {
        'free_to_paid_conversion_percent': round(free_to_paid, 2),
        'paying_users': paying_users,
        'total_students': total_students,
    }


def churn_and_renewal(period_start, period_end):
    expired_subs = Subscription.objects.filter(expires_at__gte=period_start, expires_at__lte=period_end)
    expired_count = expired_subs.count()
    renewed_count = NotificationLog.objects.filter(
        notification_type='renewal_confirmation', status='sent',
        subscription__in=expired_subs,
    ).values('subscription').distinct().count()
    renewal_rate = (renewed_count / expired_count * 100) if expired_count else 0.0
    churn_rate = 100 - renewal_rate if expired_count else 0.0
    return {
        'expired_in_period': expired_count,
        'renewed_in_period': renewed_count,
        'renewal_rate_percent': round(renewal_rate, 2),
        'churn_rate_percent': round(churn_rate, 2),
    }


def ltv_and_arpu():
    per_user = (
        Purchase.objects.filter(status='approved')
        .values('user')
        .annotate(total=Sum('final_amount'))
    )
    totals = [float(row['total']) for row in per_user]
    ltv = sum(totals) / len(totals) if totals else 0.0
    arpu = ltv  # same denominator (distinct paying users) as LTV in this all-time view
    return {'ltv': round(ltv, 2), 'arpu': round(arpu, 2)}


def popular_plans(limit=10):
    rows = (
        Purchase.objects.filter(status='approved', kind='subscription', plan__isnull=False)
        .values('plan__id', 'plan__name', 'plan__product_type')
        .annotate(purchase_count=Count('id'), revenue=Sum('final_amount'))
        .order_by('-purchase_count')[:limit]
    )
    return [
        {
            'plan_id': r['plan__id'],
            'plan_name': r['plan__name'],
            'product_type': r['plan__product_type'],
            'purchase_count': r['purchase_count'],
            'revenue': float(r['revenue'] or 0),
        }
        for r in rows
    ]


def coupon_usage(limit=10):
    rows = (
        Purchase.objects.filter(coupon__isnull=False)
        .values('coupon__id', 'coupon__code')
        .annotate(redemption_count=Count('id'), total_discount=Sum('discount_amount'))
        .order_by('-redemption_count')[:limit]
    )
    return [
        {
            'coupon_id': r['coupon__id'],
            'code': r['coupon__code'],
            'redemption_count': r['redemption_count'],
            'total_discount': float(r['total_discount'] or 0),
        }
        for r in rows
    ]


def payment_outcomes():
    rows = Purchase.objects.values('status').annotate(count=Count('id'))
    counts = {r['status']: r['count'] for r in rows}
    total_decided = counts.get('approved', 0) + counts.get('rejected', 0)
    rejection_rate = (counts.get('rejected', 0) / total_decided * 100) if total_decided else 0.0
    return {
        'pending': counts.get('pending', 0),
        'approved': counts.get('approved', 0),
        'rejected': counts.get('rejected', 0),
        'rejection_rate_percent': round(rejection_rate, 2),
    }


def geographic_distribution(limit=20):
    from accounts.models import StudentProfile

    rows = (
        StudentProfile.objects.exclude(province='')
        .values('province')
        .annotate(count=Count('id'))
        .order_by('-count')[:limit]
    )
    return [{'province': r['province'], 'count': r['count']} for r in rows]


def build_analytics(period_days=30):
    now = timezone.now()
    period_start = now - timezone.timedelta(days=period_days)

    return {
        'generated_at': now.isoformat(),
        'period_days': period_days,
        'subscriptions': subscription_counts(),
        'revenue': revenue_summary(),
        'conversion': conversion_metrics(),
        'renewals': churn_and_renewal(period_start, now),
        'ltv_arpu': ltv_and_arpu(),
        'popular_plans': popular_plans(),
        'coupon_usage': coupon_usage(),
        'payment_outcomes': payment_outcomes(),
        'geographic_distribution': geographic_distribution(),
        'notes': {
            'mrr_arr': 'Computed from active, non-scholarship subscriptions only — scholarship-granted access carries zero revenue.',
            'conversion': "No formal trial period exists on this platform, so this is a free-to-paid conversion figure (registered users who made >=1 approved purchase).",
            'renewals': 'Scoped to subscriptions whose expiry fell within the selected period; a "renewal" is one that received a renewal_confirmation notification.',
            'payment_outcomes': 'Purchases are manually verified (bank/eSewa/Khalti reference), not live gateway callbacks — this is the admin review outcome distribution, not a gateway decline rate.',
            'geographic_distribution': 'Based on StudentProfile.province, a free-text field (not normalized) — treat as approximate.',
        },
    }
