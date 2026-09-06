"""Question/Subject eligibility — the academics-app equivalent of
courses/access.py and tests_app/access.py. Promoted from academics/views.py
(pure move, no logic change) so other apps (smart_practice) can reuse the
same scoping instead of re-deriving it."""
from django.db.models import Q, Subquery

from courses.access import eligible_course_ids


def question_course_scoped(qs, user):
    """Question-specific eligibility filter — NOT the same as a plain
    course-M2M filter. Question.courses is an optional, admin-facing
    narrowing field ("a question can be shared across courses") that is,
    in real production data, unpopulated on every single question —
    confirmed via CourseSerializer.get_question_count returning 0 for
    every course after this was live. Treating a blank Question.courses
    as unconditionally 'shared' (the same rule that's correct for
    Subject, which IS populated for every real subject) would mean this
    filter restricts nothing at all: every question in the platform
    would remain visible to every student regardless of course, exactly
    the residual "Physics/Chemistry still appear in CEE-PG practice" leak
    this whole audit exists to close. The actually-populated, reliable
    per-course signal for a question is its Subject's `courses` — so a
    question with no explicit tag of its own inherits its subject's
    scope; an explicit Question.courses tag (if a future admin workflow
    starts setting one) still overrides/narrows it.

    Scalability audit Fix 2: was a JOIN to Question.courses and (via
    subject__courses) Subject.courses, OR'd across three branches, wrapped
    in .distinct() because either M2M join can match more than one row per
    Question (fan-out) — e.g. a question tagged to two eligible courses.
    That .distinct(), with no restricted .values(), forced MySQL to
    materialize every matching row's full column set just to deduplicate
    (confirmed 2.7-5.3s at 100K questions — see the Fix 1/Fix 2 audit
    reports). First rewritten as EXISTS subqueries (no fan-out, no
    .distinct() needed at all), which fixed the FETCH but regressed the
    unfiltered COUNT (185.9ms -> 440.9ms at 100K, 716.5ms -> 871.7ms at
    200K) — MySQL evaluates a correlated Exists(...OuterRef...) as a
    DEPENDENT SUBQUERY, re-run once per candidate row, instead of folding
    it into a single materialized semi-join.

    Scalability audit Finding A fix: each EXISTS(...OuterRef...) replaced
    with the logically identical id__in=Subquery(...)/subject_id__in=
    Subquery(...) — "this row's id/subject_id is a member of the set of
    ids that have a matching course row" is the exact same predicate as
    "a matching course row exists for this id/subject_id", just expressed
    as set membership instead of correlated existence. MySQL then
    classifies each subquery as a plain (non-correlated) SUBQUERY,
    materialized once rather than per row — confirmed via direct
    A/B measurement: ~7x faster on the unfiltered case at both 100K and
    200K, with no regression on the already-fast filtered case. Still no
    .distinct() needed: IN/NOT IN against a subquery is exactly as
    fan-out-free as EXISTS/NOT EXISTS — every M2M through-table FK column
    involved (question_id, subject_id, course_id) is NOT NULL by Django's
    M2M implementation, so the classic "NOT IN a subquery containing NULL
    silently matches nothing" SQL pitfall can't occur here."""
    if user and user.is_authenticated and user.is_staff:
        return qs
    course_ids = eligible_course_ids(user)

    from .models import Question, Subject

    question_courses = Question.courses.through
    subject_courses = Subject.courses.through

    eligible_via_question_course = question_courses.objects.filter(course_id__in=course_ids).values('question_id')
    any_question_course = question_courses.objects.values('question_id')
    eligible_via_subject_course = subject_courses.objects.filter(course_id__in=course_ids).values('subject_id')
    any_subject_course = subject_courses.objects.values('subject_id')

    has_eligible_question_course = Q(id__in=Subquery(eligible_via_question_course))
    has_any_question_course = Q(id__in=Subquery(any_question_course))
    has_eligible_subject_course = Q(subject_id__in=Subquery(eligible_via_subject_course))
    has_any_subject_course = Q(subject_id__in=Subquery(any_subject_course))

    return qs.filter(
        has_eligible_question_course
        | (~has_any_question_course & has_eligible_subject_course)
        | (~has_any_question_course & ~has_any_subject_course)
    )


def locked_subject_ids(user):
    """Subjects this user can't currently access (Pro subject, no active
    subscription) — staff always see everything. Shared by every Question
    Bank view that needs to scope a queryset to what a student may browse.

    Scalability audit Phase 3: used to call has_qbank_access(user, subject)
    once per Subject — has_qbank_access() itself issues its own 1-2 queries
    (subject.courses, then an .exists() subscription check), so this was an
    O(subject count) query pattern on a call site hit on nearly every
    Question Bank/Smart Practice request. Rewritten to batch: one query for
    every non-free subject's course ids, one for the user's active QBank
    subscriptions, then a plain in-memory set intersection per subject —
    flat query count regardless of catalog size. Exactly reproduces
    has_qbank_access's per-subject logic (is_free bypass; an unauthenticated
    user or a subject with no courses assigned is always locked; otherwise
    locked unless the user's accessible course ids intersect the subject's)."""
    if user.is_authenticated and user.is_staff:
        return []
    from billing.access import active_qbank_course_ids

    from .models import Subject

    accessible_course_ids = active_qbank_course_ids(user)

    # Phase 3 Free Starter: a subject a student can still cover with
    # remaining free-starter qbank quota must not be locked either —
    # mirrors entitlements.services.can_view_qbank's own free-starter
    # fallback, so list-level visibility and the real
    # QuestionViewSet.answer() consumption gate never disagree (a subject
    # this returned as "locked" would 404 on get_object() before the
    # answer() gate is ever reached, regardless of remaining free-starter
    # quota — confirmed the hard way, by this phase's own tests, before
    # this fix). Deliberately a plain read, no lazy-provisioning here
    # (this is a hot, scalability-audited path — see this function's own
    # docstring; a student who has never been provisioned yet simply
    # isn't treated as free-starter-eligible for LISTING until their
    # first real consumption attempt lazily provisions them).
    has_free_starter_qbank = False
    if user.is_authenticated:
        from entitlements.models import FreeStarterEntitlement

        row = FreeStarterEntitlement.objects.filter(user=user, resource_type='qbank').first()
        has_free_starter_qbank = bool(row and row.is_currently_valid)

    if has_free_starter_qbank:
        return []

    locked = []
    for subject in Subject.objects.filter(is_free=False).prefetch_related('courses'):
        subject_course_ids = {c.id for c in subject.courses.all()}
        if not subject_course_ids or not (subject_course_ids & accessible_course_ids):
            locked.append(subject.id)
    return locked
