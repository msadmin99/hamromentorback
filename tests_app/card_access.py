"""Phase 10 — the student-facing card access contract.

Answers, for one exam card, the questions the Student UX actually needs:
*can I start this, continue it, or review it — and if not, why, and is
there something I can buy?* — so the frontend renders a decision instead
of inventing one. Before this, `ExamCard.js` inferred lockedness from
`is_pro && card_status !== 'completed'`, i.e. from price alone: a student
with an active subscription, a scholarship, a direct purchase, an
individual assignment, or a Free Starter grant still saw a padlock.

## Why this is not a second access engine

The canonical decision is, and remains, `entitlements.services.
can_start_test()` (Phase 4). This module is a **batched projection of the
same rules**, existing only because a catalog page renders ~20 cards and
calling the canonical function per card costs 6-9 queries each.
`resolve_card_access()` evaluates the identical branches in the identical
order against a per-request snapshot, and
`tests_app/tests_phase10.py: CardAccessMatchesCanStartTestTests` asserts
card-by-card agreement with `can_start_test()` across the entitlement
matrix — so a future change to one that isn't mirrored in the other fails
the suite rather than silently drifting.

Two things it deliberately does NOT re-derive:

* **Academic eligibility.** Every list endpoint already runs
  `visible_test_queryset(user, qs)` (`TestViewSet.get_queryset`), the
  queryset form of `can_access_test`, so a Test that reaches a card has
  passed that gate by construction. Re-deriving it per card would be the
  duplication this module exists to avoid.
* **Enforcement.** Nothing here grants anything. The authoritative check
  still runs server-side at `_start_attempt`/`SubmitTestView`/
  `TestResultView` on the actual action. This decides what a *button*
  looks like; it is UX, not security.
"""
from django.utils import timezone

from billing.access import has_daily_test_access, has_mock_test_access, has_pyq_access
from entitlements.services import (
    REASON_ATTEMPT_LIMIT_REACHED,
    REASON_EXAM_CLOSED,
    REASON_EXAM_NOT_OPEN,
    REASON_FREE_LIMIT_REACHED,
    REASON_PURCHASE_REQUIRED,
    SOURCE_COURSE_ENROLLMENT,
    SOURCE_DIRECT_PURCHASE,
    SOURCE_FREE_STARTER,
    SOURCE_NONE,
    SOURCE_SUBSCRIPTION,
)

# Card states, in the priority order the UI resolves them. Deliberately a
# single `state` rather than a pile of independent badges, so the frontend
# renders one primary affordance instead of choosing between several.
STATE_CONTINUE = 'continue'      # an attempt is in progress — resume it
STATE_REVIEW = 'review'          # already completed — go read the result
STATE_START = 'start'            # entitled and open right now
STATE_UPCOMING = 'upcoming'      # scheduled, window hasn't opened
STATE_CLOSED = 'closed'          # window has passed, never attempted
STATE_ATTEMPTS_EXHAUSTED = 'attempts_exhausted'
STATE_LOCKED = 'locked'          # needs an upgrade/purchase

# Which Free Starter bucket each exam type draws on — the exact mapping
# entitlements.services.can_start_test uses (qbank Tests share the
# mock_test allowance), kept in one place so the two cannot disagree.
FREE_STARTER_RESOURCE = {
    'mock': 'mock_test',
    'qbank': 'mock_test',
    'daily': 'daily_test',
    'pyq': 'pyq',
    'grand': 'grand_test',
}


class StudentEntitlementSnapshot:
    """Every commercial fact needed to resolve a whole page of cards,
    fetched once. Bounded: a fixed handful of queries per request
    regardless of how many cards are on the page — not per card.

    Built per request and cached on the serializer context; never cached
    across requests, so a purchase approved seconds ago is reflected on
    the next page load.
    """

    def __init__(self, user, *, read_only=False):
        """`read_only=True` is used by admin "preview as student"
        (tests_app/preview.py): it suppresses the one write a normal
        entitlement check can perform — Phase 3's lazy provisioning of a
        missing FreeStarterEntitlement — so previewing a student can never
        create rows against their account."""
        self.user = user
        self.authenticated = bool(user and user.is_authenticated)
        self.read_only = read_only
        self._free_starter_cache = {}
        self._grand_test_ids = None
        self._product_access_cache = {}

    # -- commercial entitlement ------------------------------------------------

    def has_product_access(self, test):
        """The same `has_*_access` call `can_start_test` makes for this exam
        type — memoized per product type, which is exact rather than an
        approximation: `has_mock_test_access`/`has_daily_test_access`/
        `has_pyq_access` all resolve purely from (user, product_type)
        subscriptions and only touch `test` for the `is_pro` short-circuit
        this resolver has already handled before calling. So every card of
        a given exam type provably shares one answer, and asking once per
        page instead of once per card cannot change it.

        (A query-count test — CardAccessApiTests — pins this: it caught the
        original per-card version costing +1 query per card.)"""
        if not self.authenticated:
            return False
        checker = {
            'mock': ('mock_test', has_mock_test_access),
            'qbank': ('mock_test', has_mock_test_access),
            'daily': ('daily_test', has_daily_test_access),
            'pyq': ('pyq', has_pyq_access),
        }.get(test.exam_type)
        if checker is None:
            return False
        product_type, check = checker
        if product_type not in self._product_access_cache:
            self._product_access_cache[product_type] = check(self.user, test)
        return self._product_access_cache[product_type]

    def grand_test_ids(self):
        """Test ids this student holds a live (non-revoked — Phase 9)
        Grand Test grant for. One query for the whole page."""
        if self._grand_test_ids is None:
            if not self.authenticated:
                self._grand_test_ids = set()
            else:
                from billing.models import GrandTestAccess

                self._grand_test_ids = set(
                    GrandTestAccess.objects.filter(user=self.user, revoked_at__isnull=True)
                    .values_list('test_id', flat=True)
                )
        return self._grand_test_ids

    def has_free_starter(self, resource_type):
        """Phase 3's own non-consuming availability check, memoized per
        resource type — at most one call per bucket per request, never one
        per card. Browsing a catalog must never consume quota, and this
        never does (`has_free_starter_available` is explicitly
        non-consuming)."""
        if not self.authenticated or not resource_type:
            return False
        if resource_type not in self._free_starter_cache:
            if self.read_only:
                # Preview mode: same question, asked without the lazy
                # provisioning that the normal path performs. A student who
                # has never been provisioned simply shows as having no free
                # allowance, which is what they'd see until their own next
                # visit provisions them.
                from entitlements.models import FreeStarterEntitlement

                self._free_starter_cache[resource_type] = any(
                    row.effective_status == 'active' and (row.unlimited or row.remaining > 0)
                    for row in FreeStarterEntitlement.objects.filter(
                        user=self.user, resource_type=resource_type,
                    )
                )
            else:
                from entitlements.provisioning import has_free_starter_available

                self._free_starter_cache[resource_type] = has_free_starter_available(self.user, resource_type)
        return self._free_starter_cache[resource_type]


def _blank(state, *, reason_code='', upgrade_available=False, source=SOURCE_NONE, **extra):
    payload = {
        'state': state,
        'can_start': state == STATE_START,
        'can_continue': state == STATE_CONTINUE,
        'can_review': state == STATE_REVIEW,
        'reason_code': reason_code,
        'upgrade_available': upgrade_available,
        'source': source,
    }
    payload.update(extra)
    return payload


def _session_block(session):
    """Phase 6 session-window state, evaluated against server time — the
    reason a card can say "Opens in 2h" without the browser clock getting
    a vote."""
    from .lifecycle import compute_effective_session_status

    status = compute_effective_session_status(session)
    if status == 'cancelled':
        return _blank(STATE_CLOSED, reason_code=REASON_EXAM_CLOSED)
    if status in ('draft', 'scheduled', 'registration_open'):
        return _blank(STATE_UPCOMING, reason_code=REASON_EXAM_NOT_OPEN)
    if status == 'completed':
        return _blank(STATE_CLOSED, reason_code=REASON_EXAM_CLOSED)
    return None  # 'live' — the window is open, carry on


def resolve_card_access(test, snapshot, attempts, session=None):
    """The card contract for one Test. `attempts` is this student's
    already-prefetched attempts for it (see
    TestListSerializer._user_attempts — one query for the whole page).

    Branch order mirrors `entitlements.services.can_start_test` exactly;
    see this module's docstring for why, and for the test that keeps them
    honest.
    """
    in_progress = next((a for a in attempts if a.status == 'in_progress'), None)
    submitted = [a for a in attempts if a.status == 'submitted']
    max_attempts = session.max_attempts if session else test.max_attempts
    attempts_left = max(0, (max_attempts or 1) - len(attempts))
    latest_review_id = max(submitted, key=lambda a: a.score).id if submitted else None

    def finish(block):
        block.setdefault('attempts_left', attempts_left)
        block.setdefault('latest_attempt_id', latest_review_id)
        block.setdefault('in_progress_attempt_id', in_progress.id if in_progress else None)
        return block

    # An attempt already in flight outranks everything else: whatever the
    # student's entitlement looks like now, they are mid-exam and the only
    # sensible affordance is "resume". Matches can_start_test, which also
    # short-circuits on an existing in-progress attempt before entitlement.
    if in_progress:
        return finish(_blank(STATE_CONTINUE))

    if session is not None:
        blocked = _session_block(session)
        if blocked is not None:
            # A closed window still leaves a completed attempt reviewable —
            # "Missed" is only for a student who never attempted it.
            if blocked['state'] == STATE_CLOSED and submitted:
                return finish(_blank(STATE_REVIEW))
            return finish(blocked)

    if submitted and attempts_left <= 0:
        return finish(_blank(STATE_REVIEW))

    if attempts_left <= 0:
        return finish(_blank(STATE_ATTEMPTS_EXHAUSTED, reason_code=REASON_ATTEMPT_LIMIT_REACHED))

    # --- entitlement, in can_start_test's order -------------------------------
    if not test.is_pro:
        return finish(_blank(STATE_START, source=SOURCE_COURSE_ENROLLMENT))

    if not snapshot.authenticated:
        return finish(_blank(STATE_LOCKED, reason_code=REASON_PURCHASE_REQUIRED, upgrade_available=True))

    if test.exam_type == 'grand':
        if test.id in snapshot.grand_test_ids():
            return finish(_blank(STATE_START, source=SOURCE_DIRECT_PURCHASE))
    elif snapshot.has_product_access(test):
        return finish(_blank(STATE_START, source=SOURCE_SUBSCRIPTION))

    resource = FREE_STARTER_RESOURCE.get(test.exam_type)
    if snapshot.has_free_starter(resource):
        return finish(_blank(STATE_START, source=SOURCE_FREE_STARTER))

    # Exhausted-free reads differently from never-had-access: the student
    # had something and used it, which is the moment an upgrade prompt is
    # actually useful rather than noise.
    had_free_allowance = bool(resource) and _has_any_free_starter_row(snapshot, resource)
    reason = REASON_FREE_LIMIT_REACHED if had_free_allowance else REASON_PURCHASE_REQUIRED
    return finish(_blank(STATE_LOCKED, reason_code=reason, upgrade_available=True))


def _has_any_free_starter_row(snapshot, resource_type):
    """Did this student ever hold a Free Starter allowance for this bucket
    (i.e. is 'locked' really 'you used yours up')? Read-only; never
    provisions, so a catalog view can't manufacture an allowance."""
    if not snapshot.authenticated:
        return False
    cache = snapshot.__dict__.setdefault('_free_starter_rows', {})
    if resource_type not in cache:
        from entitlements.models import FreeStarterEntitlement

        cache[resource_type] = FreeStarterEntitlement.objects.filter(
            user=snapshot.user, resource_type=resource_type,
        ).exists()
    return cache[resource_type]
