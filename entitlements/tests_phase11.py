"""Phase 11 — analytics tests.

Covers the plan's Phase 11 scope: free-tier usage/conversion metrics
(bullet 1), the taxonomy index (bullet 2), plus the one analytics
authorization gap the audit turned up.

The load-bearing tests here are the ones that would catch a *wrong
number*, not just a 200: `EffectiveStatusAgreementTests` (SQL
classification vs the model property) and `FreeStarterMetricsAccuracyTests`
(every figure checked against hand-counted fixture data).
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from billing.models import Purchase
from entitlements.analytics import (
    _effective_status_case,
    free_starter_metrics,
    free_starter_reach,
    free_starter_usage,
    free_to_paid_conversion,
)
from entitlements.models import EntitlementEventLog, FreeStarterEntitlement

User = get_user_model()


def _student(n):
    return User.objects.create_user(username=f'stu{n}', email=f'stu{n}@x.com', password='pw12345!')


def _grant(user, resource_type='mock_test', *, quantity=3, used=0, status='active',
           unlimited=False, expires_at=None):
    return FreeStarterEntitlement.objects.create(
        user=user, resource_type=resource_type, quantity=quantity, used=used,
        status=status, unlimited=unlimited, expires_at=expires_at,
    )


class EffectiveStatusAgreementTests(TestCase):
    """The SQL Case/When in `_effective_status_case()` must classify every
    row exactly as `FreeStarterEntitlement.effective_status` does.

    This is the guard rail for the whole free-tier dashboard: exhaustion,
    reach and the "active" counts are all produced by the SQL branch, while
    the rest of the platform makes decisions from the Python property. If
    they drift, the dashboard silently reports numbers that contradict what
    students actually experience — which is exactly the class of bug a 200
    OK test would never catch.
    """

    def setUp(self):
        past = timezone.now() - timedelta(days=1)
        future = timezone.now() + timedelta(days=30)
        u = _student(1)
        # One row per branch of effective_status, plus the precedence cases
        # where two conditions are true at once.
        self.rows = [
            _grant(u, 'qbank', quantity=5, used=0),                                  # active
            _grant(u, 'mock_test', quantity=5, used=5),                              # exhausted
            _grant(u, 'daily_test', quantity=5, used=0, expires_at=past),            # expired
            _grant(u, 'grand_test', quantity=5, used=0, status='revoked'),           # revoked
            _grant(u, 'pyq', quantity=0, used=0, unlimited=True),                    # active (unlimited)
        ]
        u2 = _student(2)
        # Precedence: revoked outranks expired outranks exhausted.
        self.rows += [
            _grant(u2, 'qbank', quantity=5, used=5, status='revoked', expires_at=past),
            _grant(u2, 'mock_test', quantity=5, used=5, expires_at=past),
            _grant(u2, 'daily_test', quantity=5, used=9),   # used > quantity, not just ==
            _grant(u2, 'pyq', quantity=5, used=0, expires_at=future),  # future expiry = still active
        ]

    def test_sql_classification_matches_the_model_property_for_every_row(self):
        annotated = FreeStarterEntitlement.objects.annotate(eff=_effective_status_case())
        by_id = {row.id: row.eff for row in annotated}
        self.assertEqual(len(by_id), len(self.rows))
        for row in self.rows:
            row.refresh_from_db()
            self.assertEqual(
                by_id[row.id], row.effective_status,
                f'SQL and model disagree for {row.resource_type} '
                f'(quantity={row.quantity} used={row.used} status={row.status} expires_at={row.expires_at})',
            )

    def test_unlimited_is_never_exhausted_in_sql(self):
        """An unlimited grant with used >= quantity (quantity is 0) must not
        be classified exhausted — the property guards this with
        `not self.unlimited`, and the SQL must too."""
        u = _student(3)
        row = _grant(u, 'qbank', quantity=0, used=50, unlimited=True)
        eff = FreeStarterEntitlement.objects.annotate(eff=_effective_status_case()).get(pk=row.pk).eff
        self.assertEqual(eff, 'active')
        self.assertEqual(eff, row.effective_status)


class FreeStarterMetricsAccuracyTests(TestCase):
    """Every figure checked against hand-counted fixture data, not just
    against itself."""

    def setUp(self):
        started = timezone.now()
        self.period_start = started - timedelta(days=30)
        self.old = started - timedelta(days=90)

        self.a, self.b, self.c = _student(1), _student(2), _student(3)
        _grant(self.a, 'mock_test', quantity=3, used=3)   # exhausted
        _grant(self.a, 'qbank', quantity=10, used=4)      # active
        _grant(self.b, 'mock_test', quantity=3, used=1)   # active
        # self.c has no grant at all — registered but never provisioned.

        # Events. created_at is auto_now_add, so it is rewritten after
        # creation for the rows that need to sit outside the period.
        self._event(self.a, 'mock_test', 'created')
        self._event(self.a, 'mock_test', 'consumed')
        self._event(self.a, 'mock_test', 'consumed')
        self._event(self.a, 'mock_test', 'consumed')
        self._event(self.a, 'mock_test', 'exhausted')
        self._event(self.a, 'qbank', 'consumed')
        self._event(self.b, 'mock_test', 'consumed')
        old_event = self._event(self.b, 'qbank', 'consumed')
        EntitlementEventLog.objects.filter(pk=old_event.pk).update(created_at=self.old)

    @property
    def now(self):
        """Read at assertion time, not in setUp. `created_at` is
        auto_now_add, so a timestamp captured before the fixtures are built
        sits *before* every event and a `created_at__lte=now` filter would
        exclude all of them — an artifact of the fixture, not a real
        boundary (in production `now` is taken at request time, after the
        events being counted already exist)."""
        return timezone.now()

    def _event(self, user, resource_type, event):
        return EntitlementEventLog.objects.create(user=user, resource_type=resource_type, event=event)

    def test_reach_counts_provisioned_students_not_all_registered(self):
        reach = free_starter_reach()
        # 3 users exist; only 2 hold grants. The third must not inflate reach.
        self.assertEqual(User.objects.count(), 3)
        self.assertEqual(reach['students_provisioned'], 2)

    def test_reach_classifies_exhausted_and_active_per_resource(self):
        by_type = {r['resource_type']: r for r in free_starter_reach()['by_resource_type']}
        self.assertEqual(by_type['mock_test']['exhausted'], 1)   # student a
        self.assertEqual(by_type['mock_test']['active'], 1)      # student b
        self.assertEqual(by_type['mock_test']['total'], 2)
        self.assertEqual(by_type['qbank']['active'], 1)
        # A resource type nobody holds still appears, at zero — the dashboard
        # shows the full set rather than silently omitting unused tiers.
        self.assertEqual(by_type['grand_test']['total'], 0)

    def test_usage_period_scoping_excludes_older_events(self):
        usage = free_starter_usage(self.period_start, self.now)
        # 6 consumed events total; 1 of them (student b's qbank) is 90 days old.
        self.assertEqual(EntitlementEventLog.objects.filter(event='consumed').count(), 6)
        self.assertEqual(usage['consumption_events_in_period'], 5)
        self.assertEqual(usage['students_who_used_free_all_time'], 2)
        self.assertEqual(usage['students_who_used_free_in_period'], 2)

    def test_usage_reports_quota_exhaustion(self):
        usage = free_starter_usage(self.period_start, self.now)
        self.assertEqual(usage['quota_exhaustion_events_in_period'], 1)
        self.assertEqual(usage['students_hitting_quota_in_period'], 1)

    def test_consumption_breakdown_by_resource_type(self):
        usage = free_starter_usage(self.period_start, self.now)
        by_type = {r['resource_type']: r['consumption_events'] for r in usage['consumption_by_resource_type']}
        self.assertEqual(by_type['mock_test'], 4)   # student a x3, student b x1
        self.assertEqual(by_type['qbank'], 1)       # student b's qbank event is 90 days old
        # Recount independently rather than trusting the literals above.
        expected = EntitlementEventLog.objects.filter(
            event='consumed', resource_type='mock_test', created_at__gte=self.period_start,
        ).count()
        self.assertEqual(by_type['mock_test'], expected)

    def test_free_to_paid_denominator_is_free_users_not_all_users(self):
        conv = free_to_paid_conversion()
        # Students a and b consumed free allowance; c never did.
        self.assertEqual(conv['free_users_considered'], 2)
        self.assertEqual(conv['converted_to_paid'], 0)
        self.assertEqual(conv['conversion_percent'], 0.0)

    def test_free_to_paid_counts_only_approved_purchases(self):
        Purchase.objects.create(user=self.a, status='pending', original_amount=100, final_amount=100)
        self.assertEqual(free_to_paid_conversion()['converted_to_paid'], 0)
        Purchase.objects.create(user=self.a, status='approved', original_amount=100, final_amount=100)
        conv = free_to_paid_conversion()
        self.assertEqual(conv['converted_to_paid'], 1)
        self.assertEqual(conv['conversion_percent'], 50.0)   # 1 of 2 free users

    def test_free_to_paid_is_empty_not_divide_by_zero_when_nobody_used_free(self):
        EntitlementEventLog.objects.all().delete()
        conv = free_to_paid_conversion()
        self.assertEqual(conv['free_users_considered'], 0)
        self.assertEqual(conv['conversion_percent'], 0.0)

    def test_upgrade_clicks_is_reported_as_unavailable_not_as_a_number(self):
        """The plan lists upgrade clicks; nothing on this platform records
        them. The payload must say so rather than invent a proxy."""
        payload = free_starter_metrics(self.period_start, self.now)
        unavailable = {row['metric'] for row in payload['unavailable']}
        self.assertIn('upgrade_clicks', unavailable)
        self.assertNotIn('upgrade_clicks', payload['usage'])
        self.assertNotIn('upgrade_clicks', payload['reach'])


class FreeStarterAnalyticsDoNotMutateTests(TestCase):
    """Analytics is an observation layer: reading the dashboard must not
    provision, consume, or otherwise touch a student's free allowance."""

    def test_building_metrics_writes_nothing(self):
        u = _student(1)
        row = _grant(u, 'mock_test', quantity=3, used=1)
        EntitlementEventLog.objects.create(user=u, resource_type='mock_test', event='consumed')

        before_used = row.used
        before_entitlements = FreeStarterEntitlement.objects.count()
        before_events = EntitlementEventLog.objects.count()

        free_starter_metrics(timezone.now() - timedelta(days=30), timezone.now())

        row.refresh_from_db()
        self.assertEqual(row.used, before_used)
        self.assertEqual(FreeStarterEntitlement.objects.count(), before_entitlements)
        self.assertEqual(EntitlementEventLog.objects.count(), before_events)

    def test_metrics_never_provision_a_missing_entitlement(self):
        """Phase 3 provisions lazily, so an entitlement *check* can create a
        row. Analytics must never trip that path — a student who has never
        been provisioned must still have no row after a dashboard load."""
        u = _student(1)
        self.assertEqual(FreeStarterEntitlement.objects.filter(user=u).count(), 0)
        free_starter_metrics(timezone.now() - timedelta(days=30), timezone.now())
        self.assertEqual(FreeStarterEntitlement.objects.filter(user=u).count(), 0)


class FreeStarterAnalyticsScaleTests(TestCase):
    """Query count must be bounded by the number of *metrics*, not by the
    number of students. A per-student query pattern here would make the
    admin dashboard degrade linearly with platform growth."""

    def _seed(self, n_students):
        for i in range(n_students):
            u = User.objects.create_user(username=f'bulk{i}', email=f'b{i}@x.com', password='pw12345!')
            _grant(u, 'mock_test', quantity=3, used=i % 4)
            EntitlementEventLog.objects.create(user=u, resource_type='mock_test', event='created')
            EntitlementEventLog.objects.create(user=u, resource_type='mock_test', event='consumed')

    def test_query_count_does_not_grow_with_student_count(self):
        start, end = timezone.now() - timedelta(days=30), timezone.now()

        self._seed(5)
        with self.assertNumQueries(11) as small:
            free_starter_metrics(start, end)

        User.objects.filter(username__startswith='bulk').delete()
        self._seed(60)
        with self.assertNumQueries(len(small.captured_queries)):
            free_starter_metrics(start, end)

    def test_results_stay_correct_at_the_larger_size(self):
        """Bounded queries are only worth having if the numbers are still
        right — a fixed query count computed from a truncated result set
        would pass the test above and still be wrong."""
        self._seed(60)
        reach = free_starter_reach()
        self.assertEqual(reach['students_provisioned'], 60)
        usage = free_starter_usage(timezone.now() - timedelta(days=30), timezone.now())
        self.assertEqual(usage['consumption_events_in_period'], 60)


class FreeStarterAnalyticsPrivacyTests(TestCase):
    """Business analytics must be aggregate-only — no student identities."""

    def test_payload_contains_no_identifying_fields(self):
        u = User.objects.create_user(username='identifiable', email='real.person@example.com', password='pw12345!')
        _grant(u, 'mock_test', quantity=3, used=3)
        EntitlementEventLog.objects.create(user=u, resource_type='mock_test', event='consumed')

        import json
        blob = json.dumps(free_starter_metrics(timezone.now() - timedelta(days=30), timezone.now()))

        self.assertNotIn('identifiable', blob)
        self.assertNotIn('real.person@example.com', blob)
        self.assertNotIn(f'"user": {u.id}', blob)
        self.assertNotIn('user_id', blob)


class AnalyticsEndpointAuthorizationTests(APITestCase):
    """The dashboard carries revenue and funnel data — role-gated, not
    merely is_staff-gated."""

    def setUp(self):
        self.url = reverse('billing-analytics')
        self.student = _student(1)
        self.editor = User.objects.create_user(
            username='editor', email='e@x.com', password='pw12345!', is_staff=True,
        )
        self.editor.admin_role = 'editor'
        self.editor.save(update_fields=['admin_role'])
        self.admin = User.objects.create_user(
            username='admin', email='a@x.com', password='pw12345!', is_staff=True, is_superuser=True,
        )

    def test_anonymous_denied(self):
        self.assertIn(self.client.get(self.url).status_code, (401, 403))

    def test_student_denied(self):
        self.client.force_authenticate(self.student)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_editor_role_staff_denied(self):
        """is_staff alone is not analytics authorization — an Editor-role
        account is staff but must not reach revenue/conversion data."""
        self.client.force_authenticate(self.editor)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_admin_allowed_and_free_starter_block_present(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('free_starter', resp.data)
        self.assertIn('reach', resp.data['free_starter'])
        self.assertIn('usage', resp.data['free_starter'])
        self.assertIn('free_to_paid', resp.data['free_starter'])

    def test_existing_analytics_blocks_are_unchanged(self):
        """Additive only — Phase 11 must not remove or rename anything the
        admin dashboard already consumes."""
        self.client.force_authenticate(self.admin)
        resp = self.client.get(self.url)
        for key in (
            'subscriptions', 'revenue', 'conversion', 'renewals', 'ltv_arpu',
            'popular_plans', 'coupon_usage', 'payment_outcomes', 'geographic_distribution', 'notes',
        ):
            self.assertIn(key, resp.data, f'{key} disappeared from the analytics payload')
