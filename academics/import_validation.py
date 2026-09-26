"""Rule-based structural validation for a parsed question — no AI needed for
any of this; missing/duplicate options, blank questions, multiple-correct
detection, missing explanations, and bad answer keys are all mechanically
checkable. Returns (errors, warnings) — errors block import, warnings don't."""
import re

_TAG_RE = re.compile(r'<[^>]+>')


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


def validate_parsed_question(pq):
    errors = []
    warnings = []

    if not _has_content(pq.get('text_html'), pq.get('question_image_path')):
        errors.append('Question text is blank.')

    options = pq.get('options') or []
    non_blank_options = [o for o in options if _has_content(o.get('text_html'), o.get('image_path'))]
    if len(non_blank_options) < 2:
        errors.append(f'Only {len(non_blank_options)} option(s) found — at least 2 are required.')

    seen_texts = {}
    for i, opt in enumerate(non_blank_options):
        norm = _plain_text(opt.get('text_html')).lower()
        if not norm:
            # An image-only option has no caption text to compare — never
            # flagged as a "duplicate" of another image-only option just
            # because both happen to normalize to the same empty string.
            continue
        if norm in seen_texts:
            errors.append(f'Option {i + 1} duplicates option {seen_texts[norm] + 1} ("{norm[:40]}").')
        else:
            seen_texts[norm] = i

    # Multiple correct options are valid (the "Multiple Correct MCQ" type is
    # explicitly supported) — flagged as a warning, not blocked, since it's
    # ambiguous from source data alone whether it's intentional or an
    # answer-key parsing slip. The admin resolves it in the preview step.
    correct_count = sum(1 for o in non_blank_options if o.get('is_correct'))
    if correct_count == 0:
        errors.append('No option is marked correct — check the answer key.')
    elif correct_count > 1:
        warnings.append(f'{correct_count} options are marked correct — confirm this is intentionally a Multiple Correct MCQ.')

    if not _has_content(pq.get('explanation_html'), pq.get('explanation_image_path')):
        warnings.append('No explanation provided.')

    return errors, warnings
