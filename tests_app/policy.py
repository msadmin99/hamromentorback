"""Phase 5 — Exam Type Policies.

The single read path for "what should a brand-new exam of this category
look like by default." Two callers, and only two:

  1. TestAdminSerializer.create() (tests_app/serializers.py) — applies a
     policy-controlled field's default when the incoming payload omits it,
     making the backend authoritative rather than relying solely on the
     frontend having fetched the right values.
  2. TestViewSet.exam_type_policies (tests_app/views.py) — a read-only
     endpoint so the Create Exam Wizard and the Import & Create Test UI can
     both consume the same canonical template instead of hardcoding it.

Deliberately NOT read anywhere else: no list/detail/attempt/result path
touches ExamTypePolicy, and Test has no FK to it — a policy change must
never retroactively alter an already-created Test.
"""
from .models import ExamTypePolicy, Test

# The exact set of Test fields a category policy may default. Every one of
# these is a genuinely per-instance-overridable field on Test itself — see
# PHASE5_AUDIT_AND_ARCHITECTURE.md §3 for what was deliberately excluded
# and why (title/description/subject/assignment/access_password/is_new).
POLICY_CONTROLLED_FIELDS = (
    'duration_minutes',
    'questions_per_page',
    'negative_marking',
    'shuffle_questions',
    'shuffle_options',
    'max_attempts',
    'solutions_visibility',
    'is_draft',
    'is_pro',
    'free_preview_questions',
    'price',
)

# Hardcoded, model-field-default fallback used only if a category's policy
# row is genuinely missing (e.g. a fresh DB before the seeding data
# migration has run, or a row was deleted). Mirrors Test's own field
# defaults exactly — never a second, independently-drifting source, since
# every value below is read straight off the live model field.
_FIELD_TO_MODEL_DEFAULT = {
    field_name: Test._meta.get_field(field_name).get_default()
    for field_name in POLICY_CONTROLLED_FIELDS
}


def get_exam_type_defaults(exam_type: str) -> dict:
    """Return the effective policy-controlled default values for one exam
    category, as a plain dict keyed by Test field name (not by the model's
    'default_' column names). Always returns every key in
    POLICY_CONTROLLED_FIELDS, even if no policy row exists yet."""
    try:
        policy = ExamTypePolicy.objects.get(pk=exam_type)
    except ExamTypePolicy.DoesNotExist:
        return dict(_FIELD_TO_MODEL_DEFAULT)
    return {field: getattr(policy, f'default_{field}') for field in POLICY_CONTROLLED_FIELDS}


def get_all_exam_type_defaults() -> dict:
    """{exam_type: {field: value, ...}, ...} for every exam category,
    including any category with no policy row yet (falls back per-category,
    same rule as get_exam_type_defaults)."""
    return {choice: get_exam_type_defaults(choice) for choice, _label in Test.EXAM_TYPE_CHOICES}
