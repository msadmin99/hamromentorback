"""Question/Subject eligibility — the academics-app equivalent of
courses/access.py and tests_app/access.py. Promoted from academics/views.py
(pure move, no logic change) so other apps (smart_practice) can reuse the
same scoping instead of re-deriving it."""
from django.db.models import Q

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
    starts setting one) still overrides/narrows it."""
    if user and user.is_authenticated and user.is_staff:
        return qs
    course_ids = eligible_course_ids(user)
    return qs.filter(
        Q(courses__id__in=course_ids)
        | Q(courses__isnull=True, subject__courses__id__in=course_ids)
        | Q(courses__isnull=True, subject__courses__isnull=True)
    ).distinct()


def locked_subject_ids(user):
    """Subjects this user can't currently access (Pro subject, no active
    subscription) — staff always see everything. Shared by every Question
    Bank view that needs to scope a queryset to what a student may browse."""
    if user.is_authenticated and user.is_staff:
        return []
    from billing.access import has_qbank_access

    from .models import Subject

    return [s.id for s in Subject.objects.all() if not has_qbank_access(user, s)]
