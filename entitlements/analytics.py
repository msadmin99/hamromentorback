"""Phase 11 — free-tier usage and conversion metrics.

`implementation-plan.md` Phase 11, bullet 1: *"Extend Analytics ... with
the new conversion/usage metrics the spec lists (free registrations, free
usage, free-to-paid conversion, upgrade clicks, quota exhaustion, etc.),
sourced from Phase 3's `FreeStarterEntitlement` and Phase 2's entitlement
layer."*

Pure aggregation. Nothing here writes, and nothing here consumes quota:
every function reads `FreeStarterEntitlement` / `EntitlementEventLog` /
`Purchase` and returns numbers. In particular it never calls
`has_free_starter_available()` or `provision_free_starter()`, so running
the admin dashboard cannot provision or consume a single student's free
allowance (`FreeStarterAnalyticsDoNotMutateTests` pins this).

## Where each number actually comes from

Two authoritative sources, used for two different questions:

* **`FreeStarterEntitlement` rows** answer *"what is true right now"* —
  how many students hold a grant, how much is left, how many are
  exhausted. This is current state.
* **`EntitlementEventLog` rows** (`created` / `consumed` / `exhausted`,
  written by `entitlements.provisioning`) answer *"what happened during
  this period"* — the only source with a timestamp per event, so every
  period-scoped figure below is derived from it, never from the
  entitlement row's running `used` counter (which has no history).

## The one metric the plan lists that does not exist

**Upgrade clicks are not tracked anywhere on this platform.** There is no
click/impression event model, no analytics beacon, and no endpoint that
records an upgrade-CTA interaction — Phase 10 rendered upgrade prompts
but deliberately added no telemetry. Rather than invent a proxy and
label it "upgrade clicks", `free_starter_metrics()` returns an explicit
`unavailable` entry naming what would have to be built. A fabricated
number here would be worse than a missing one: it would be read as real
funnel data.

## Exhaustion is classified in SQL, and checked against the model

`FreeStarterEntitlement.effective_status` is a Python property, so it
cannot be filtered in the database. The `_effective_status_case()`
annotation below mirrors it branch-for-branch in the identical
precedence (revoked -> expired -> exhausted -> active), and
`EffectiveStatusAgreementTests` asserts the SQL classification equals the
Python property for every row across the matrix — so a change to one that
isn't mirrored in the other fails the suite instead of quietly skewing a
dashboard.
"""
from django.db.models import Case, CharField, Count, F, Q, Value, When
from django.utils import timezone

from .models import EntitlementEventLog, FreeStarterEntitlement

# The order resource types are reported in — the plan's own listing order,
# so the dashboard doesn't reshuffle between requests.
from .models import RESOURCE_TYPE_CHOICES

_RESOURCE_ORDER = [code for code, _label in RESOURCE_TYPE_CHOICES]


def _effective_status_case(now=None):
    """SQL mirror of `FreeStarterEntitlement.effective_status`, in the same
    precedence order the property uses. Kept adjacent to the property it
    mirrors; agreement is enforced by test, not by comment."""
    now = now or timezone.now()
    return Case(
        When(status='revoked', then=Value('revoked')),
        When(Q(expires_at__isnull=False) & Q(expires_at__lt=now), then=Value('expired')),
        When(Q(unlimited=False) & Q(used__gte=F('quantity')), then=Value('exhausted')),
        default=Value('active'),
        output_field=CharField(),
    )


def free_starter_reach():
    """How far the free tier reaches right now — current state, from the
    entitlement rows themselves.

    "Free registrations" in the plan's wording is reported here as
    `students_provisioned`: a student is counted once they actually hold a
    Free Starter grant, not merely once they register. That distinction is
    deliberate and matters, because Phase 3 provisions **lazily** (on the
    student's first capability check), so registered-but-never-active
    students have no rows at all. Reporting registrations as if they were
    free-tier reach would overstate it.
    """
    rows = (
        FreeStarterEntitlement.objects.annotate(eff=_effective_status_case())
        .values('resource_type', 'eff')
        .annotate(n=Count('id'))
    )
    per_resource = {code: {'active': 0, 'exhausted': 0, 'expired': 0, 'revoked': 0} for code in _RESOURCE_ORDER}
    for row in rows:
        bucket = per_resource.setdefault(
            row['resource_type'], {'active': 0, 'exhausted': 0, 'expired': 0, 'revoked': 0},
        )
        bucket[row['eff']] = bucket.get(row['eff'], 0) + row['n']

    return {
        'students_provisioned': FreeStarterEntitlement.objects.values('user').distinct().count(),
        'by_resource_type': [
            {'resource_type': code, **per_resource[code], 'total': sum(per_resource[code].values())}
            for code in _RESOURCE_ORDER
        ],
    }


def free_starter_usage(period_start, period_end):
    """Actual free-tier consumption, from the append-only event log — the
    only source that knows *when* something was used.

    `consumption_events` counts consume operations, not questions: Phase 3
    consumes one unit per QBank question and one per exam start, so this is
    "units drawn down", which is what the quota itself is denominated in.
    """
    in_period = Q(created_at__gte=period_start, created_at__lte=period_end)
    consumed = EntitlementEventLog.objects.filter(event='consumed')
    exhausted = EntitlementEventLog.objects.filter(event='exhausted')

    by_resource = {
        row['resource_type']: row['n']
        for row in consumed.filter(in_period).values('resource_type').annotate(n=Count('id'))
    }
    return {
        'students_who_used_free_all_time': consumed.values('user').distinct().count(),
        'students_who_used_free_in_period': consumed.filter(in_period).values('user').distinct().count(),
        'consumption_events_in_period': consumed.filter(in_period).count(),
        'consumption_by_resource_type': [
            {'resource_type': code, 'consumption_events': by_resource.get(code, 0)}
            for code in _RESOURCE_ORDER
        ],
        'provisioned_in_period': (
            EntitlementEventLog.objects.filter(event='created').filter(in_period).values('user').distinct().count()
        ),
        'quota_exhaustion_events_in_period': exhausted.filter(in_period).count(),
        'students_hitting_quota_in_period': exhausted.filter(in_period).values('user').distinct().count(),
    }


def free_to_paid_conversion():
    """Of the students who actually used their free allowance, how many
    went on to pay.

    This is a **different and narrower** figure than
    `billing.analytics.conversion_metrics()`'s
    `free_to_paid_conversion_percent`, which divides paying users by *all*
    registered non-staff users. Both are kept: the existing one is a
    whole-platform figure, this one is the free-tier funnel the plan asked
    for. They are reported side by side under different names rather than
    one silently replacing the other, because they answer different
    questions and will legitimately disagree.

    Scholarship grants never create a `Purchase` row (`GrantAccessView`
    creates the Subscription directly), so a scholarship student is
    correctly not counted as a conversion here.
    """
    from billing.models import Purchase

    used_free_ids = set(
        EntitlementEventLog.objects.filter(event='consumed')
        .exclude(user__isnull=True)
        .values_list('user_id', flat=True)
        .distinct()
    )
    if not used_free_ids:
        return {
            'free_users_considered': 0,
            'converted_to_paid': 0,
            'conversion_percent': 0.0,
        }
    converted = (
        Purchase.objects.filter(status='approved', user_id__in=used_free_ids)
        .values('user').distinct().count()
    )
    return {
        'free_users_considered': len(used_free_ids),
        'converted_to_paid': converted,
        'conversion_percent': round(converted / len(used_free_ids) * 100, 2),
    }


def free_starter_metrics(period_start, period_end):
    """The whole free-tier block for the admin dashboard.

    Aggregate-only by construction: no user id, name, email, or any other
    identifying field appears in this payload — only counts. That is what
    makes it safe to expose on a business dashboard, and
    `FreeStarterAnalyticsPrivacyTests` asserts it.
    """
    return {
        'reach': free_starter_reach(),
        'usage': free_starter_usage(period_start, period_end),
        'free_to_paid': free_to_paid_conversion(),
        'unavailable': [
            {
                'metric': 'upgrade_clicks',
                'reason': (
                    'Not tracked. This platform records no UI interaction events — there is no click/'
                    'impression model and no telemetry endpoint. Reporting a proxy under this name would '
                    'misrepresent it as real funnel data. Capturing it would require a new event model '
                    'plus a client beacon, which is not in this phase\'s scope.'
                ),
            },
        ],
        'notes': {
            'students_provisioned': (
                'Counts students who hold a Free Starter grant, not all registered users — Phase 3 '
                'provisions lazily on first capability check, so a registered student who never browsed '
                'has no row yet.'
            ),
            'consumption_events': (
                'Units drawn down (one per QBank question, one per exam start), which is how the quota '
                'itself is denominated — not a count of questions viewed.'
            ),
            'free_to_paid': (
                'Denominator is students who actually consumed free allowance. This is deliberately '
                'narrower than the platform-wide conversion figure reported under "conversion", which '
                'divides paying users by every registered student. The two will differ.'
            ),
            'period_scoped': (
                'All "in_period" figures come from the append-only EntitlementEventLog; current-state '
                'figures come from the entitlement rows. The running `used` counter has no history and '
                'is never used for period figures.'
            ),
        },
    }
