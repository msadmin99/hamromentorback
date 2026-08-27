"""Exam access control — the single place that decides whether a student may
see or start a given Test. Mirrors billing/access.py's role (a shared,
reusable eligibility layer instead of ad hoc checks scattered across views),
but for *academic* eligibility (course/batch/individual assignment) rather
than commercial (subscription/payment) access — the two are deliberately
kept separate, same as the reference spec's "Academic Eligibility + Product
Access + Exam Assignment -> Final Access" split. Payment/subscription checks
still live in billing/access.py and _start_attempt(); this module only
answers "is this student even allowed to know this exam exists."
"""
from django.db.models import Q

from courses.access import eligible_batch_ids, eligible_course_ids


def can_access_test(user, test):
    """Object-level check — used at the point a student actually starts an
    exam (tests_app.views._start_attempt) so a leaked/guessed Test ID can
    never bypass scoping just because it wasn't filtered out of a list
    first. Precedence matches the spec's own IF/ELSE IF/ELSE ordering."""
    if user and user.is_authenticated and (user.is_staff or test.created_by_id == user.id):
        return True
    if test.is_draft:
        return False
    if not (user and user.is_authenticated):
        return False
    if test.assigned_students.filter(pk=user.id).exists():
        return True
    batch_ids = eligible_batch_ids(user)
    if batch_ids and test.assigned_batches.filter(id__in=batch_ids).exists():
        return True
    course_ids = set(test.courses.values_list('id', flat=True))
    if course_ids:
        return bool(course_ids & eligible_course_ids(user))
    # Blank courses + not individually/batch-assigned: only still allowed for
    # exams flagged by the one-time legacy migration (see tests_app/models.py
    # Test.needs_course_review) — every exam created after this feature
    # shipped defaults is_draft=True and must be explicitly assigned.
    return bool(test.needs_course_review)


def visible_test_queryset(user, qs):
    """Queryset-level equivalent of can_access_test, for TestViewSet.get_queryset()
    — always applied for non-staff regardless of whether a ?course= query
    param was sent, so listing can never be widened just by omitting it.

    needs_course_review must only grant visibility when the test has NO
    courses assigned — Q(courses__isnull=True, needs_course_review=True),
    never a bare Q(needs_course_review=True). The bare form was the exact
    root cause of a real production leak: three tests already had 12 real
    courses assigned (the flag was left stale — nothing in the Admin exam
    form clears it, see TestAdminSerializer.update() below) and were still
    showing up in the list/detail endpoints for completely unenrolled
    students, because the OR made the flag an unconditional bypass instead
    of the courses-empty-only fallback can_access_test() above already
    correctly implements. Keep these two functions' precedence identical."""
    if user and user.is_authenticated and user.is_staff:
        return qs
    qs = qs.filter(is_draft=False)
    course_ids = eligible_course_ids(user)
    batch_ids = eligible_batch_ids(user)
    allowed = Q(courses__id__in=course_ids) | Q(courses__isnull=True, needs_course_review=True)
    if batch_ids:
        allowed |= Q(assigned_batches__id__in=batch_ids)
    if user and user.is_authenticated:
        allowed |= Q(assigned_students=user) | Q(created_by=user)
    return qs.filter(allowed).distinct()
