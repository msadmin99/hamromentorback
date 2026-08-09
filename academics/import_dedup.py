"""Duplicate detection for bulk import — normalized-text similarity via
stdlib difflib (no AI/new dependency needed). Bounded to the same subject so
this never becomes an O(n²) full-table scan: a batch of thousands of Physics
questions only ever compares against existing Physics questions, not the
entire question bank."""
import re
from difflib import SequenceMatcher

SIMILARITY_THRESHOLD = 0.85
_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')


def normalize_text(html_value):
    text = _TAG_RE.sub(' ', html_value or '')
    text = _WS_RE.sub(' ', text).strip().lower()
    return text


def find_duplicate(pq, existing_texts_by_id, batch_texts_by_index=None, self_index=None):
    """Returns (duplicate_question_id, similarity) for the closest match at or
    above the threshold, checking `existing_texts_by_id` (existing DB
    questions, already scoped to the batch's selected Subject by the caller)
    first, then earlier rows already parsed in this same batch. Returns
    (None, 0) if nothing crosses the threshold."""
    candidate = normalize_text(pq.get('text_html'))
    if not candidate:
        return None, 0.0

    best_id, best_score = None, 0.0
    for question_id, existing_text in existing_texts_by_id.items():
        score = SequenceMatcher(None, candidate, existing_text).ratio()
        if score > best_score:
            best_id, best_score = question_id, score

    if batch_texts_by_index:
        for idx, other_text in batch_texts_by_index.items():
            if idx == self_index:
                continue
            score = SequenceMatcher(None, candidate, other_text).ratio()
            if score > best_score:
                best_id, best_score = f'row:{idx}', score

    if best_score >= SIMILARITY_THRESHOLD:
        return best_id, round(best_score, 3)
    return None, 0.0


def existing_texts_for_subject(subject):
    """Bounded query — only questions in the target subject, never the whole table."""
    from .models import Question

    return {
        q.id: normalize_text(q.text)
        for q in Question.objects.filter(subject=subject).only('id', 'text')
    }
