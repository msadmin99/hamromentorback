"""Central Access Decision layer — Phase 2 Entitlement Foundation, extended
in Phase 3 (Free Starter consumption) and Phase 4 (the full capability set).

Answers "why does this user have access to X" by composing every EXISTING,
already-correct access source (courses.access, tests_app.access,
billing.access) plus the Free Starter layer into one uniform, explainable
AccessDecision. This module is a DECISION layer, not a new data store —
those three modules remain the sources of truth for academic/commercial
access exactly as before; this module calls them, it does not re-implement
their logic (see docs/ENTITLEMENT_CURRENT_STATE.md and
docs/ACCESS_DECISION_MATRIX.md for the full source-by-source inventory this
composes).

Phase 4 adds the full capability set (CanView/CanPurchase/CanRegister/
CanStart/CanContinue/CanSubmit/CanReview/CanViewSolutions/CanViewRank/
CanViewAnalytics — see docs/ACCESS_DECISION_MATRIX.md for exact semantics)
and a standardized, machine-readable `reason_code` vocabulary, additive to
the existing free-text `reason` field so no Phase 2/3 caller breaks.

`can_start_test`/`can_view_qbank` remain READ-ONLY decision queries — they
never consume Free Starter quota. tests_app._start_attempt and
academics.views.QuestionViewSet.answer own the actual, consuming
enforcement (see docs/FREE_STARTER_IMPLEMENTATION.md for exactly why these
two are not the same code path, and why that's an accepted, documented
tradeoff rather than an oversight).
"""
from dataclasses import dataclass, replace
from typing import Optional

# ---------------------------------------------------------------------
# Source types (Phase 2/3, unchanged)
# ---------------------------------------------------------------------
SOURCE_ADMIN_OVERRIDE = 'admin_override'
SOURCE_INDIVIDUAL_ASSIGNMENT = 'individual_assignment'
SOURCE_BATCH_ASSIGNMENT = 'batch_assignment'
SOURCE_COURSE_ENROLLMENT = 'course_enrollment'
SOURCE_SUBSCRIPTION = 'subscription'
SOURCE_SCHOLARSHIP = 'scholarship'
SOURCE_DIRECT_PURCHASE = 'direct_purchase'
SOURCE_FREE_STARTER = 'free_starter'
SOURCE_NONE = 'none'

# ---------------------------------------------------------------------
# Capabilities (Phase 4) — see docs/ACCESS_DECISION_MATRIX.md for the
# exact semantics of each; every one is independently testable and none
# collapses into a single has_access boolean.
# ---------------------------------------------------------------------
CAN_VIEW = 'CanView'
CAN_PURCHASE = 'CanPurchase'
CAN_REGISTER = 'CanRegister'
CAN_START = 'CanStart'
CAN_CONTINUE = 'CanContinue'
CAN_SUBMIT = 'CanSubmit'
CAN_REVIEW = 'CanReview'
CAN_VIEW_SOLUTIONS = 'CanViewSolutions'
CAN_VIEW_DETAILED_REVIEW = 'CanViewDetailedReview'
CAN_VIEW_RANK = 'CanViewRank'
CAN_VIEW_ANALYTICS = 'CanViewAnalytics'

# ---------------------------------------------------------------------
# Standardized denial reason codes (Phase 4). A small, non-overlapping
# set — each has exactly one meaning. 'free_limit_reached' and
# 'purchase_required' are preserved byte-for-byte from Phase 3's
# tests_app._free_starter_denied_payload()/academics.views.QuestionViewSet.
# answer() response shapes — do not rename either without a compatibility
# plan, per the Phase 4 spec's explicit instruction.
# ---------------------------------------------------------------------
REASON_AUTHENTICATION_REQUIRED = 'authentication_required'
REASON_NOT_ENTITLED = 'not_entitled'
REASON_FREE_LIMIT_REACHED = 'free_limit_reached'
REASON_SUBSCRIPTION_EXPIRED = 'subscription_expired'
REASON_PURCHASE_REQUIRED = 'purchase_required'
REASON_COURSE_ENROLLMENT_EXPIRED = 'course_enrollment_expired'
REASON_SCHOLARSHIP_EXPIRED = 'scholarship_expired'
REASON_ASSIGNMENT_REQUIRED = 'assignment_required'
REASON_EXAM_NOT_OPEN = 'exam_not_open'
REASON_EXAM_CLOSED = 'exam_closed'
REASON_EXAM_MISSED = 'exam_missed'  # Release Candidate — Grand Test: entitled, window passed unattempted
REASON_ATTEMPT_LIMIT_REACHED = 'attempt_limit_reached'
REASON_RESUME_NOT_ALLOWED = 'resume_not_allowed'
REASON_REGISTRATION_REQUIRED = 'registration_required'
REASON_PAYMENT_REQUIRED = 'payment_required'
REASON_PASSWORD_REQUIRED = 'password_required'
REASON_RESOURCE_UNAVAILABLE = 'resource_unavailable'
REASON_SOLUTIONS_NOT_RELEASED = 'solutions_not_released'  # Phase 7
REASON_REVIEW_EXPIRED = 'review_expired'  # GT3-4


@dataclass(frozen=True)
class AccessDecision:
    """allowed/denied plus enough to explain why, without leaking anything
    sensitive (no password, no payment reference, no other user's data —
    just what THIS student is entitled to, from where, and why not
    otherwise). `capability`/`reason_code` are Phase 4 additions, both
    default to '' so every existing Phase 2/3 construction of this class
    (which never passed them) keeps working unchanged."""

    allowed: bool
    capability: str = ''
    source_type: str = SOURCE_NONE
    source_id: Optional[int] = None
    valid_from: Optional[object] = None
    expires_at: Optional[object] = None
    remaining: Optional[int] = None
    reason: str = ''
    reason_code: str = ''
    upgrade_available: bool = False

    def as_dict(self):
        return {
            'allowed': self.allowed,
            'capability': self.capability,
            'source_type': self.source_type,
            'source_id': self.source_id,
            'valid_from': self.valid_from.isoformat() if self.valid_from else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'remaining': self.remaining,
            'reason': self.reason,
            'reason_code': self.reason_code,
            'upgrade_available': self.upgrade_available,
        }


def _deny(reason, reason_code=REASON_NOT_ENTITLED, upgrade_available=False):
    return AccessDecision(allowed=False, reason=reason, reason_code=reason_code, upgrade_available=upgrade_available)


def _tag(decision, capability):
    """Stamps `capability` onto an already-built AccessDecision without
    touching any other field — used so every internal branch of a
    capability function doesn't have to repeat `capability=...` itself."""
    return replace(decision, capability=capability)


# =======================================================================
# Academic / commercial composition (Phase 2, unchanged behavior)
# =======================================================================

def academic_eligibility(user, course):
    """CanView-shaped decision for course-level academic eligibility.
    Wraps courses.access — unmodified logic, exposed through the uniform
    AccessDecision shape. Course enrollment is ACADEMIC eligibility, never
    conflated with commercial entitlement (Step 18)."""
    from courses.access import eligible_course_ids
    from courses.models import Enrollment

    if not user or not user.is_authenticated:
        return _tag(_deny('Not authenticated.', REASON_AUTHENTICATION_REQUIRED), CAN_VIEW)
    if course.id not in eligible_course_ids(user):
        return _tag(
            _deny('Not enrolled in this course (or the enrollment has expired).', REASON_COURSE_ENROLLMENT_EXPIRED),
            CAN_VIEW,
        )
    enrollment = Enrollment.objects.filter(user=user, course=course, is_active=True).first()
    if not enrollment:
        return _tag(_deny('Not enrolled in this course.', REASON_COURSE_ENROLLMENT_EXPIRED), CAN_VIEW)
    return _tag(AccessDecision(
        allowed=True, source_type=SOURCE_COURSE_ENROLLMENT, source_id=enrollment.id,
        valid_from=enrollment.enrolled_at, expires_at=enrollment.expires_at,
        reason='Active course enrollment.',
    ), CAN_VIEW)


def commercial_entitlement(user, product_type, course):
    """CanView/CanStart-shaped decision for one product_type under one
    course. Wraps billing.access._active_subscriptions — unmodified logic
    (the confirmed mock/daily/pyq course-scoping inconsistency is
    deliberately NOT fixed here, see docs/ACCESS_DECISION_MATRIX.md).
    Distinguishes scholarship-origin from paid-origin (Step 17) using the
    same Subscription.scholarship reverse-relation the Phase 2 fix relies
    on."""
    from billing import access as billing_access

    if not user or not user.is_authenticated:
        return _deny('Not authenticated.', REASON_AUTHENTICATION_REQUIRED)

    sub = billing_access._active_subscriptions(user, product_type, course=course).select_related('scholarship').first()
    if not sub:
        return _deny(f'No active {product_type} subscription for this course.', REASON_SUBSCRIPTION_EXPIRED, upgrade_available=True)

    is_scholarship = hasattr(sub, 'scholarship')
    return AccessDecision(
        allowed=True,
        source_type=SOURCE_SCHOLARSHIP if is_scholarship else SOURCE_SUBSCRIPTION,
        source_id=sub.id, valid_from=sub.starts_at, expires_at=sub.expires_at,
        remaining=(sub.mock_test_quota - sub.mock_test_used) if sub.mock_test_quota is not None else None,
        reason='Active scholarship.' if is_scholarship else 'Active subscription.',
    )


def _try_free_starter(user, resource_type):
    """Read-only check — does NOT consume quota (this is a decision query,
    not the actual start/consume action). A caller that actually starts/
    consumes something must separately call
    entitlements.provisioning.consume_free_starter."""
    from .models import FreeStarterEntitlement
    from .provisioning import provision_free_starter

    if not user or not user.is_authenticated:
        return _deny('Not authenticated.', REASON_AUTHENTICATION_REQUIRED)
    if user.is_staff:
        # Phase 3, Step 31: staff/admin accounts must never be treated as
        # normal free users — this decision function must not even suggest
        # free-starter as a viable path for them, matching
        # tests_app._start_attempt's real enforcement, which excludes staff
        # from this fallback entirely.
        return _deny('Free Starter does not apply to staff accounts.', REASON_NOT_ENTITLED)

    row = FreeStarterEntitlement.objects.filter(user=user, resource_type=resource_type).first()
    if not row:
        # Lazy-provision fallback (Step 14): safe to call late for a
        # student who somehow reached an access check without
        # registration-time provisioning having run for them.
        provision_free_starter(user)
        row = FreeStarterEntitlement.objects.filter(user=user, resource_type=resource_type).first()

    if not row or not row.is_currently_valid:
        return _deny('Free Starter allocation exhausted or not available.', REASON_FREE_LIMIT_REACHED, upgrade_available=True)

    return AccessDecision(
        allowed=True, source_type=SOURCE_FREE_STARTER, source_id=row.id,
        valid_from=row.valid_from, expires_at=row.expires_at, remaining=row.remaining,
        reason='Free Starter allocation available.',
    )


# =======================================================================
# CanView / CanStart (Phase 2, unchanged behavior — now capability-tagged)
# =======================================================================

def can_view_qbank(user, subject):
    """CanView for QBank practice on a Subject — free subject, or active
    'qbank' subscription (any course the subject belongs to), or a Free
    Starter fallback."""
    from billing.access import has_qbank_access

    if subject.is_free:
        return _tag(AccessDecision(allowed=True, source_type=SOURCE_NONE, reason='This subject is free.'), CAN_VIEW)
    if not user or not user.is_authenticated:
        return _tag(_deny('Not authenticated.', REASON_AUTHENTICATION_REQUIRED), CAN_VIEW)
    if has_qbank_access(user, subject):
        course = subject.courses.first()
        decision = commercial_entitlement(user, 'qbank', course) if course else None
        if decision and decision.allowed:
            return _tag(decision, CAN_VIEW)
        return _tag(AccessDecision(allowed=True, source_type=SOURCE_SUBSCRIPTION, reason='Active QBank subscription.'), CAN_VIEW)
    return _tag(_try_free_starter(user, 'qbank'), CAN_VIEW)


def can_view_test(user, test):
    """CanView for a Test resource — catalog/detail visibility, entirely
    independent of commercial entitlement (the Phase 4 spec's core
    principle: a locked, unpurchased exam is still supposed to be
    discoverable). Wraps tests_app.access.can_access_test UNMODIFIED — that
    function already is exactly a "can this student reach this Test"
    check with no commercial gate baked in (the commercial layer is
    applied separately, only for CanStart, exactly matching the
    CanView=True/CanStart=False example in the Phase 4 spec)."""
    from tests_app.access import can_access_test

    if not user or not user.is_authenticated:
        return _tag(_deny('Not authenticated.', REASON_AUTHENTICATION_REQUIRED), CAN_VIEW)
    if not can_access_test(user, test):
        return _tag(
            _deny(
                'Not eligible for this exam (course/batch/individual assignment, or the exam is unpublished).',
                REASON_ASSIGNMENT_REQUIRED,
            ),
            CAN_VIEW,
        )
    return _tag(AccessDecision(allowed=True, source_type=SOURCE_NONE, reason='Visible to this student.'), CAN_VIEW)


def can_start_test(user, test, session=None):
    """CanStart for a Test resource — composes the SAME two gates
    tests_app._start_attempt() already enforces (academic via
    tests_app.access.can_access_test, commercial via the matching
    billing.access.has_*_access function), reusing both unmodified, plus a
    Free Starter fallback when the commercial gate fails. Read-only — does
    not consume Free Starter quota, does not start an attempt, does not
    check a password (password remains an additional layer checked only by
    the real start flow, never a substitute for entitlement — Phase 2 spec
    Step "GRAND TEST PASSWORD").

    Phase 4 addition: also checks session window and attempt-limit state
    (mirroring _start_attempt's own checks exactly) — entitlement alone is
    NOT sufficient for CanStart if the exam session is closed/not yet open
    or the student has already used every attempt (Phase 4 spec's own
    "ENTITLEMENT != ATTEMPT != SESSION" principle). `session` is optional
    and defaults to None for full backward compatibility with every
    existing Phase 2/3 caller that checks a plain, unscheduled Test."""
    from django.utils import timezone as _timezone

    from billing.access import get_grand_test_access, has_daily_test_access, has_mock_test_access, has_pyq_access
    from tests_app.access import can_access_test

    if not can_access_test(user, test):
        return _tag(
            _deny('Not eligible for this exam (course/batch/individual assignment, or the exam is unpublished).', REASON_ASSIGNMENT_REQUIRED),
            CAN_START,
        )

    if session:
        if session.status == 'cancelled':
            return _tag(_deny('This session has been cancelled.', REASON_EXAM_CLOSED), CAN_START)
        if session.status == 'draft':
            return _tag(_deny('This session is not yet open.', REASON_EXAM_NOT_OPEN), CAN_START)
        now = _timezone.now()
        if now < session.start_datetime:
            return _tag(_deny('This session has not started yet.', REASON_EXAM_NOT_OPEN), CAN_START)
        if now > session.end_datetime:
            return _tag(_deny('This session has ended.', REASON_EXAM_CLOSED), CAN_START)

    attempt_qs = test.attempts.filter(user=user, session=session) if (user and user.is_authenticated) else test.attempts.none()
    if attempt_qs.filter(status='in_progress').exists():
        # An in-progress attempt already exists — that's CanContinue's
        # territory (see can_continue_attempt), not a fresh CanStart. Still
        # reported as allowed here (matches _start_attempt's own behavior
        # of transparently returning the existing attempt rather than
        # erroring), just distinguishable via reason for a caller that
        # cares.
        return _tag(
            AccessDecision(allowed=True, source_type=SOURCE_NONE, reason='An attempt is already in progress — resume it.'),
            CAN_START,
        )
    max_attempts = session.max_attempts if session else test.max_attempts
    if user and user.is_authenticated and attempt_qs.count() >= max_attempts:
        return _tag(_deny('Maximum attempts reached for this test.', REASON_ATTEMPT_LIMIT_REACHED), CAN_START)

    if not test.is_pro:
        return _tag(AccessDecision(
            allowed=True, source_type=SOURCE_COURSE_ENROLLMENT, reason='Free exam — academic eligibility is sufficient.',
        ), CAN_START)

    if test.exam_type == 'grand':
        access = get_grand_test_access(user, test)
        if access:
            return _tag(AccessDecision(
                allowed=True, source_type=SOURCE_DIRECT_PURCHASE, source_id=access.id,
                reason='Grand Test access granted.',
            ), CAN_START)
        # Phase 10 correction: this branch used to stop at "purchase
        # required", while the real enforcement path
        # (tests_app.views._start_attempt) has always let a Free Starter
        # grant open a Grand Test. CanStart therefore under-reported access
        # — harmless while nothing rendered from it, but Phase 10 makes the
        # student's card render from exactly this decision, so the two must
        # agree or the UI shows a paywall on an exam the student can
        # actually start. Mirrors the mock/daily/pyq fallback directly below.
        starter = _try_free_starter(user, 'grand_test')
        if starter.allowed:
            return _tag(starter, CAN_START)
        return _tag(_deny('This Grand Test requires purchase.', REASON_PURCHASE_REQUIRED, upgrade_available=True), CAN_START)

    checker_by_type = {
        'mock': ('mock_test', has_mock_test_access),
        'qbank': ('mock_test', has_mock_test_access),
        'daily': ('daily_test', has_daily_test_access),
        'pyq': ('pyq', has_pyq_access),
    }
    starter_resource, checker = checker_by_type.get(test.exam_type, (None, None))
    if checker and checker(user, test):
        return _tag(AccessDecision(allowed=True, source_type=SOURCE_SUBSCRIPTION, reason='Active subscription confirmed.'), CAN_START)

    if starter_resource:
        starter = _try_free_starter(user, starter_resource)
        if starter.allowed:
            return _tag(starter, CAN_START)

    return _tag(
        _deny(
            f'This {test.get_exam_type_display()} requires an active subscription or purchase.',
            REASON_PURCHASE_REQUIRED, upgrade_available=True,
        ),
        CAN_START,
    )


# =======================================================================
# CanContinue / CanSubmit / CanReview / CanViewSolutions / CanViewRank
# (Phase 4, new) — all attempt/session-state checks, deliberately NOT
# re-deriving entitlement (entitlement was already checked once, when the
# attempt was created — see docs/ACCESS_DECISION_MATRIX.md's capability
# semantics table for why these are independent of commercial source).
# =======================================================================

def can_continue_attempt(user, attempt):
    """CanContinue — an existing attempt belonging to this user, still
    in_progress, may be resumed. Mirrors tests_app._start_attempt's own
    existing resume check (`attempt_qs.filter(status='in_progress')`)
    exactly, exposed as a named, independently-testable capability rather
    than only living inline inside the start flow.

    Phase 6: also denies once the attempt's effective deadline (session
    window or personal duration, whichever is sooner) has passed, even if
    no request has yet lazily finalized the row to status='submitted' —
    read-only, mirrors is_attempt_expired() exactly, never itself writes.
    A student cannot extend their time by simply not triggering the
    finalize (e.g. never calling submit/answer again) — CanContinue
    already says no the instant the deadline passes, regardless of DB
    write timing."""
    if not user or not user.is_authenticated:
        return _tag(_deny('Not authenticated.', REASON_AUTHENTICATION_REQUIRED), CAN_CONTINUE)
    if attempt.user_id != user.id:
        return _tag(_deny('Not your attempt.', REASON_NOT_ENTITLED), CAN_CONTINUE)
    if attempt.status != 'in_progress':
        return _tag(_deny('This attempt is not in progress.', REASON_RESUME_NOT_ALLOWED), CAN_CONTINUE)
    from tests_app.lifecycle import is_attempt_expired
    if is_attempt_expired(attempt):
        return _tag(_deny('This exam has ended.', REASON_EXAM_CLOSED), CAN_CONTINUE)
    return _tag(AccessDecision(allowed=True, source_type=SOURCE_NONE, reason='Attempt is in progress.'), CAN_CONTINUE)


def can_submit_attempt(user, attempt):
    """CanSubmit — same ownership/status shape as CanContinue (an
    in_progress attempt owned by this user may be finalized), kept as a
    distinct function/capability per the Phase 4 spec's explicit
    "CanStart != CanSubmit" instruction, even though the current
    underlying condition is identical — a future change to one (e.g. a
    submission-specific rule) must not have to be reverse-engineered out
    of a shared function.

    Phase 6: deliberately does NOT deny once expired, unlike CanContinue —
    a late Submit call still finalizes the attempt (using whatever was
    legitimately answered before CanContinue/SubmitAnswerView started
    rejecting new answers), which is exactly what turns an abandoned
    attempt into a properly-scored one. See tests_app/lifecycle.py's
    module docstring."""
    if not user or not user.is_authenticated:
        return _tag(_deny('Not authenticated.', REASON_AUTHENTICATION_REQUIRED), CAN_SUBMIT)
    if attempt.user_id != user.id:
        return _tag(_deny('Not your attempt.', REASON_NOT_ENTITLED), CAN_SUBMIT)
    if attempt.status != 'in_progress':
        return _tag(_deny('This attempt has already been submitted.', REASON_RESUME_NOT_ALLOWED), CAN_SUBMIT)
    return _tag(AccessDecision(allowed=True, source_type=SOURCE_NONE, reason='Attempt may be submitted.'), CAN_SUBMIT)


def can_review_attempt(user, attempt):
    """CanReview — a submitted attempt owned by this user may have its
    review screen opened. Matches tests_app.views.AttemptDetailView's
    existing, correct `attempt.status == 'submitted'` gate exactly —
    reused here as the named capability so TestResultView (Phase 4 fix,
    see docs/ACCESS_DECISION_MATRIX.md discrepancy #1) can be brought into
    line with it instead of duplicating the check a second, slightly
    different way."""
    if not user or not user.is_authenticated:
        return _tag(_deny('Not authenticated.', REASON_AUTHENTICATION_REQUIRED), CAN_REVIEW)
    if attempt.user_id != user.id:
        return _tag(_deny('Not your attempt.', REASON_NOT_ENTITLED), CAN_REVIEW)
    if attempt.status != 'submitted':
        return _tag(_deny('This attempt has not been submitted yet.', REASON_RESUME_NOT_ALLOWED), CAN_REVIEW)
    return _tag(AccessDecision(allowed=True, source_type=SOURCE_NONE, reason='Attempt has been submitted.'), CAN_REVIEW)


def can_view_solutions(user, attempt):
    """CanViewSolutions — Phase 7: now genuinely enforces
    Test.solutions_visibility ('auto'/'manual'), closing the gap this
    function's own docstring used to document (see
    docs/ACCESS_DECISION_MATRIX.md discrepancy #2 and
    docs/PHASE_7_ARCHITECTURE.md for the full design).

    Always starts from CanReview (submitted + owner) — solutions can never
    be visible to someone who couldn't review the attempt at all. On top
    of that:

    - solutions_visibility='auto': released once the exam's window is
      genuinely over. For a session-scoped attempt, that's the session's
      effective_status reaching 'completed' (tests_app.lifecycle.
      compute_effective_session_status — the exact field help_text:
      "Automatically, once the exam window ends"). For a Grand Test
      scheduled via the simpler Test.scheduled_start/scheduled_end pair
      instead (no ExamSession — GT3-2/GT3-4), the identical rule now
      applies there too (GT3-4 fix, see below) — a session-less, non-
      Grand-Test attempt still releases immediately once reviewable,
      matching this codebase's existing, validated behavior for every
      Test that predates this phase.
    - solutions_visibility='manual': released only once an admin has
      explicitly done so — Test.solutions_released_at (session-less
      attempts) or the attempt's own session's solutions_released_at
      (session-scoped attempts; deliberately independent per session, so
      releasing a Daily Test's Session #1 can never leak into a still-open
      Session #2 — see ExamSession.solutions_released_at's own comment).

    GT3-4 fix: previously, a Grand Test with NO real ExamSession (the
    Test.scheduled_start/scheduled_end fallback path GT3-2 made load-
    bearing for MISSED/attempt-deadline enforcement) fell straight through
    to "released immediately once reviewable" — meaning a student who
    submitted at 10:20 on an 08:00-11:00 exam would have seen the correct
    answer/solution instantly, directly violating Grand Test 3.0's own
    'Absolute Solution-Release Rule'. Closed below by reusing
    grand_test_review_window() — the identical schedule source
    _start_attempt()/grand_test_participation_status() already enforce."""
    decision = can_review_attempt(user, attempt)
    if not decision.allowed:
        return _tag(decision, CAN_VIEW_SOLUTIONS)

    test = attempt.test
    if test.solutions_visibility == 'manual':
        released_at = attempt.session.solutions_released_at if attempt.session_id else test.solutions_released_at
        if not released_at:
            return _tag(_deny('Solutions have not been released for this exam yet.', REASON_SOLUTIONS_NOT_RELEASED), CAN_VIEW_SOLUTIONS)
        return _tag(AccessDecision(allowed=True, source_type=SOURCE_NONE, reason='Solutions released.'), CAN_VIEW_SOLUTIONS)

    # 'auto' (or any other/legacy value — fail toward the existing,
    # validated behavior rather than a new denial for data this phase
    # didn't anticipate).
    from django.utils import timezone

    if test.exam_type == 'grand':
        from tests_app.lifecycle import grand_test_review_window

        available_at, _expires_at = grand_test_review_window(
            test, session=attempt.session if attempt.session_id else None,
        )
        if available_at and timezone.now() < available_at:
            return _tag(
                _deny('Solutions are released automatically once the exam window ends.', REASON_SOLUTIONS_NOT_RELEASED),
                CAN_VIEW_SOLUTIONS,
            )
    elif attempt.session_id:
        from tests_app.lifecycle import compute_effective_session_status
        if compute_effective_session_status(attempt.session) != 'completed':
            return _tag(
                _deny('Solutions are released automatically once the exam window ends.', REASON_SOLUTIONS_NOT_RELEASED),
                CAN_VIEW_SOLUTIONS,
            )
    return _tag(AccessDecision(allowed=True, source_type=SOURCE_NONE, reason='Solutions available.'), CAN_VIEW_SOLUTIONS)


def can_view_detailed_review(user, attempt):
    """CanViewDetailedReview — GT3-4: the per-question review list
    (question/your-answer/marks/solution breakdown) can expire
    independently of the attempt's own OVERVIEW (score, rank, percentile,
    accuracy), which remains available permanently — Grand Test 3.0's own
    explicit rule: 'Result history may remain permanently available.
    Detailed review can expire.' Always starts from CanReview (submitted +
    owner); for a Grand Test with a configured review_duration_days,
    additionally denied once review_expires_at has passed. Every other
    exam type (review_duration_days is never set outside Grand Test in
    this phase) is completely unaffected — this always allows once
    CanReview allows, byte-for-byte the pre-GT3-4 behavior."""
    decision = can_review_attempt(user, attempt)
    if not decision.allowed:
        return _tag(decision, CAN_VIEW_DETAILED_REVIEW)

    test = attempt.test
    if test.exam_type == 'grand':
        from django.utils import timezone

        from tests_app.lifecycle import grand_test_review_window

        _available_at, expires_at = grand_test_review_window(
            test, session=attempt.session if attempt.session_id else None,
        )
        if expires_at and timezone.now() >= expires_at:
            return _tag(
                _deny('Detailed review for this Grand Test has expired.', REASON_REVIEW_EXPIRED),
                CAN_VIEW_DETAILED_REVIEW,
            )
    return _tag(
        AccessDecision(allowed=True, source_type=SOURCE_NONE, reason='Detailed review available.'),
        CAN_VIEW_DETAILED_REVIEW,
    )


def can_view_rank(user, attempt):
    """CanViewRank — same shape again (submitted + owner); rank/percentile
    are computed synchronously at submission time (tests_app.views.
    SubmitTestView, unmodified) and are only ever meaningful once an
    attempt has actually been submitted."""
    decision = can_review_attempt(user, attempt)
    return _tag(decision, CAN_VIEW_RANK)


def can_view_analytics(user, target_user):
    """CanViewAnalytics — always self-scoped in this codebase (every
    performance/analytics endpoint already computes purely off
    request.user, confirmed by direct re-read of tests_app.views'
    StudentPerformanceOverviewView/SubjectPerformanceDetailView/
    ExamTypeStatsView/PerformanceCalendarView/AttemptComparativeView — none
    accept a target user id at all). This function exists to make that
    already-correct invariant an explicit, testable capability rather than
    an implicit property of "the view only ever queries request.user"."""
    if not user or not user.is_authenticated:
        return _tag(_deny('Not authenticated.', REASON_AUTHENTICATION_REQUIRED), CAN_VIEW_ANALYTICS)
    if user.id != target_user.id and not user.is_staff:
        return _tag(_deny("Cannot view another student's analytics.", REASON_NOT_ENTITLED), CAN_VIEW_ANALYTICS)
    return _tag(AccessDecision(allowed=True, source_type=SOURCE_NONE, reason='Own analytics.'), CAN_VIEW_ANALYTICS)


# =======================================================================
# CanPurchase / CanRegister (Phase 4, new)
# =======================================================================

def can_purchase_test(user, test):
    """CanPurchase — a signal, not an entitlement: true when the student
    could meaningfully buy access to this pro Test (they don't already
    have it, and there's something to buy). Deliberately does not check
    Purchase/PaymentMethod availability in detail — that's checkout's own
    concern, not the access engine's; this only answers "does an upgrade
    path exist for this resource at all"."""
    if not user or not user.is_authenticated:
        return _tag(_deny('Not authenticated.', REASON_AUTHENTICATION_REQUIRED), CAN_PURCHASE)
    if user.is_staff:
        return _tag(_deny('Staff accounts do not purchase.', REASON_NOT_ENTITLED), CAN_PURCHASE)
    if not test.is_pro:
        return _tag(_deny('This exam is free — nothing to purchase.', REASON_NOT_ENTITLED), CAN_PURCHASE)
    start_decision = can_start_test(user, test)
    if start_decision.allowed:
        return _tag(_deny('Already entitled — nothing to purchase.', REASON_NOT_ENTITLED), CAN_PURCHASE)
    return _tag(
        AccessDecision(allowed=True, source_type=SOURCE_NONE, reason='A purchase would grant access.', upgrade_available=True),
        CAN_PURCHASE,
    )


def can_register():
    """CanRegister — trivial, included for completeness per the Phase 4
    spec's explicit "each of the 10 capabilities must be independently
    testable" instruction. Registration itself has no per-resource
    target — this always returns allowed for an anonymous visitor, since
    /api/auth/register/ (accounts.views.RegisterView) is AllowAny with no
    other precondition."""
    return _tag(AccessDecision(allowed=True, source_type=SOURCE_NONE, reason='Registration is open.'), CAN_REGISTER)
