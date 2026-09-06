"""Duplicate detection for bulk import — normalized-text similarity via
stdlib difflib (no AI/new dependency needed). Bounded to the same subject so
this never becomes an O(n²) full-table scan: a batch of thousands of Physics
questions only ever compares against existing Physics questions, not the
entire question bank.

Two questions are only flagged as duplicates of each other when BOTH their
stem text AND their answer-choice set substantially match. Stem-only
matching produces real false positives: a question bank commonly reuses a
template ("Normality of X M solution of Y is?", "Molecular weight of a
Z-basic acid is W...") across many genuinely different questions that only
differ in the specific acid/value and, correspondingly, in their options —
those must never be flagged just because the wording around the blank is
similar. Matching is symmetric: a pair is only a duplicate if it clears the
threshold on both dimensions, not either one alone."""
import re
from difflib import SequenceMatcher

SIMILARITY_THRESHOLD = 0.85
_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')

# Phase 2 (dedup performance audit): a cheap, mathematically lossless
# pre-filter on normalized text length, applied before the expensive
# SequenceMatcher.ratio() call in find_duplicate() below.
#
# difflib.SequenceMatcher.ratio() is defined as 2*M / (len(a)+len(b)),
# where M is the number of matching characters found — and M can never
# exceed the length of the shorter string (you can't match more characters
# than the shorter sequence has). So for ANY two strings, always, without
# exception:
#
#     ratio(a, b) <= 2 * min(len(a), len(b)) / (len(a) + len(b))
#
# This is exactly SequenceMatcher's own documented real_quick_ratio() upper
# bound. Solving that inequality for the length ratio (let m = the shorter
# length, M = the longer length) gives an equivalent, purely arithmetic
# condition:
#
#     2m/(m+M) >= SIMILARITY_THRESHOLD   <=>   M/m <= (2 - SIMILARITY_THRESHOLD) / SIMILARITY_THRESHOLD
#
# So if the longer of two normalized texts is more than this ratio times
# the length of the shorter one, ratio() is GUARANTEED to fall below
# SIMILARITY_THRESHOLD — it is mathematically impossible for the real,
# expensive comparison to reach the threshold in that case. Skipping
# SequenceMatcher for such a pair can never turn a true duplicate (by this
# module's own existing definition) into a missed one. This is an exact
# algebraic restatement of the same inequality difflib itself uses for its
# own fast-path, not an approximation or a new similarity heuristic — the
# options-similarity check, the threshold, and every score this module
# returns are completely unchanged.
_MAX_LENGTH_RATIO = (2 - SIMILARITY_THRESHOLD) / SIMILARITY_THRESHOLD


def _length_could_match(len_a, len_b):
    """True if two normalized texts of these lengths could possibly reach
    SIMILARITY_THRESHOLD under SequenceMatcher.ratio() — see the proof
    above. False means ratio() is provably below threshold for this pair,
    so it's safe to skip the SequenceMatcher call entirely."""
    if len_a == 0 or len_b == 0:
        return len_a == len_b  # two empty strings trivially match; one empty and one not, never
    shorter, longer = (len_a, len_b) if len_a <= len_b else (len_b, len_a)
    return longer <= shorter * _MAX_LENGTH_RATIO


def normalize_text(html_value):
    text = _TAG_RE.sub(' ', html_value or '')
    text = _WS_RE.sub(' ', text).strip().lower()
    return text


def normalize_option_set(raw_texts):
    """Order-independent set of normalized option text — two questions
    whose options are listed in a different order still compare equal."""
    return frozenset(t for t in (normalize_text(x) for x in raw_texts) if t)


def _options_from_parsed(options):
    return normalize_option_set((o.get('text_html') for o in (options or [])))


def _options_similarity(set_a, set_b):
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def find_duplicate(pq, existing_by_id, batch_by_index=None, self_index=None):
    """Returns (duplicate_question_id, similarity) for the closest match that
    crosses the similarity threshold on BOTH the question stem and the
    option set — checking `existing_by_id` (existing DB questions, already
    scoped to the batch's selected Subject by the caller) first, then
    earlier rows already parsed in this same batch. `existing_by_id` and
    `batch_by_index` map id -> {'text': normalized_str, 'options': frozenset}
    (see existing_texts_for_subject below for the DB side, and
    import_views._run_dedup for the in-batch side). Returns (None, 0) if
    nothing crosses both thresholds."""
    candidate_text = normalize_text(pq.get('text_html'))
    if not candidate_text:
        return None, 0.0
    candidate_options = _options_from_parsed(pq.get('options'))

    best_id, best_score = None, 0.0

    def consider(other_id, other_text, other_options):
        nonlocal best_id, best_score
        if not other_text:
            return
        # Cheap, lossless pre-filter (see _length_could_match's docstring
        # and the proof above _MAX_LENGTH_RATIO) — skips the expensive
        # SequenceMatcher call for any pair whose length difference alone
        # already proves ratio() cannot reach SIMILARITY_THRESHOLD.
        if not _length_could_match(len(candidate_text), len(other_text)):
            return
        text_score = SequenceMatcher(None, candidate_text, other_text).ratio()
        if text_score < SIMILARITY_THRESHOLD:
            return
        options_score = _options_similarity(candidate_options, other_options)
        if options_score < SIMILARITY_THRESHOLD:
            return
        # The weaker of the two dimensions is the actual confidence this is
        # the same question — a 0.99 stem match paired with a 0.85 options
        # match is an 0.85-confidence duplicate, not a 0.99 one.
        combined = min(text_score, options_score)
        if combined > best_score:
            best_id, best_score = other_id, combined

    for question_id, data in existing_by_id.items():
        consider(question_id, data['text'], data['options'])

    if batch_by_index:
        for idx, data in batch_by_index.items():
            if idx == self_index:
                continue
            consider(f'row:{idx}', data['text'], data['options'])

    if best_id is not None:
        return best_id, round(best_score, 3)
    return None, 0.0


def existing_texts_for_subject(subject):
    """Bounded query — only questions in the target subject, never the whole
    table. Returns {question_id: {'text': normalized_str, 'options': frozenset}}."""
    from .models import Question

    questions = Question.objects.filter(subject=subject).prefetch_related('options').only('id', 'text')
    return {
        q.id: {
            'text': normalize_text(q.text),
            'options': normalize_option_set(o.text for o in q.options.all()),
        }
        for q in questions
    }
