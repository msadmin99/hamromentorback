"""Rule-based structural validation for a parsed question — no AI needed for
any of this; missing/duplicate options, blank questions, multiple-correct
detection, missing explanations, and bad answer keys are all mechanically
checkable. Returns (errors, warnings) — errors block import, warnings don't."""
import re
from typing import NamedTuple

_TAG_RE = re.compile(r'<[^>]+>')

# Stable identifiers for each validation error. The human-readable message
# can change wording freely; the "Skip (import anyway)" decision in the bulk
# import flow (import_bypass.py) is keyed on the CODE, never on message text.
ISSUE_BLANK_QUESTION = 'blank_question'
ISSUE_TOO_FEW_OPTIONS = 'too_few_options'
ISSUE_DUPLICATE_OPTION = 'duplicate_option'
ISSUE_NO_CORRECT_OPTION = 'no_correct_option'

# Errors an admin may explicitly choose to ignore ("Skip") and still import
# the question. Only an error that leaves a fully answerable, gradable
# question qualifies: a duplicated option's text is ambiguous but the
# question still has a stem, >= 2 options and a correct answer. The other
# three (no stem, < 2 options, no correct answer) each produce a question a
# student cannot meaningfully answer or that can never be graded correct, so
# they are NEVER bypassable — not by the UI and not by a direct API call.
BYPASSABLE_ISSUE_CODES = frozenset({ISSUE_DUPLICATE_OPTION})


class ValidationIssue(NamedTuple):
    code: str
    message: str

    @property
    def bypassable(self):
        return self.code in BYPASSABLE_ISSUE_CODES

# Characters that mark an option as math/LaTeX notation (a backslash command
# or delimiter, `$`, or a `^`/`_` script). For such options letter CASE is
# meaningful — \frac{G}{g} and \frac{g}{G}, or R_E^2 and r_e^2, are different
# expressions — so they are compared case-sensitively.
_MATH_NOTATION_RE = re.compile(r'[\\$^_]')


def _plain_text(html_value):
    return _TAG_RE.sub('', html_value or '').strip()


def _has_content(text_html, image_path):
    """True if this item (question stem / option / explanation) has
    anything a student could actually see — real caption text, OR a
    standalone diagram/image with no caption at all. Real reported bug:
    a Venn-diagram MCQ (MAT-4.docx Q16) has 4 options that are each
    nothing but a picture (no letter/caption text beyond "a."/"b."/...,
    which the parser strips as the option marker) — treating that as
    "blank" flagged the whole question as "Only 0 option(s) found",
    blocking it from import entirely, even though every option correctly
    parsed with its own image attached."""
    return bool(_plain_text(text_html)) or bool(image_path)


def _duplicate_key(plain_text):
    """Comparison key for the duplicate-option check: ordinary prose keeps
    the long-standing case-insensitive comparison; math/LaTeX options are
    compared exactly (case preserved) because case changes their meaning.
    Tag stripping and trimming (done by the caller) are unchanged."""
    return plain_text if _MATH_NOTATION_RE.search(plain_text) else plain_text.lower()


def collect_validation_issues(pq):
    """Structured form of validate_parsed_question: returns
    (issues: list[ValidationIssue], warnings: list[str]). Each issue carries
    a stable `code` and a `bypassable` flag (see BYPASSABLE_ISSUE_CODES)."""
    issues = []
    warnings = []

    if not _has_content(pq.get('text_html'), pq.get('question_image_path')):
        issues.append(ValidationIssue(ISSUE_BLANK_QUESTION, 'Question text is blank.'))

    options = pq.get('options') or []
    non_blank_options = [o for o in options if _has_content(o.get('text_html'), o.get('image_path'))]
    if len(non_blank_options) < 2:
        issues.append(ValidationIssue(
            ISSUE_TOO_FEW_OPTIONS, f'Only {len(non_blank_options)} option(s) found — at least 2 are required.',
        ))

    seen_texts = {}
    for i, opt in enumerate(non_blank_options):
        plain = _plain_text(opt.get('text_html'))
        if not plain:
            # An image-only option has no caption text to compare — never
            # flagged as a "duplicate" of another image-only option just
            # because both happen to normalize to the same empty string.
            continue
        key = _duplicate_key(plain)
        if key in seen_texts:
            # Shows the option's ORIGINAL text (case preserved), not the
            # lower-cased comparison key.
            issues.append(ValidationIssue(
                ISSUE_DUPLICATE_OPTION, f'Option {i + 1} duplicates option {seen_texts[key] + 1} ("{plain[:40]}").',
            ))
        else:
            seen_texts[key] = i

    # Multiple correct options are valid (the "Multiple Correct MCQ" type is
    # explicitly supported) — flagged as a warning, not blocked, since it's
    # ambiguous from source data alone whether it's intentional or an
    # answer-key parsing slip. The admin resolves it in the preview step.
    correct_count = sum(1 for o in non_blank_options if o.get('is_correct'))
    if correct_count == 0:
        issues.append(ValidationIssue(ISSUE_NO_CORRECT_OPTION, 'No option is marked correct — check the answer key.'))
    elif correct_count > 1:
        warnings.append(f'{correct_count} options are marked correct — confirm this is intentionally a Multiple Correct MCQ.')

    if not _has_content(pq.get('explanation_html'), pq.get('explanation_image_path')):
        warnings.append('No explanation provided.')
    return issues, warnings


def validate_parsed_question(pq):
    issues, warnings = collect_validation_issues(pq)
    return [issue.message for issue in issues], warnings
