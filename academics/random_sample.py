"""Scalable random row selection — shared by the QBank Practice Session
Builder (academics/views.py) and Smart Practice candidate selection
(smart_practice/services.py), the two production/student-facing places
that used to pick random questions via `.order_by('?')`.

`ORDER BY RAND()` forces the database to generate a random sort key for,
and fully sort, every row matching the filter before any LIMIT is applied
— an O(pool size log pool size) cost paid on every single call, regardless
of how few rows are actually requested. That's fine for a pool of a few
hundred rows; it stops being fine as the question bank grows toward the
100,000+ MCQ target, where a broad, lightly-filtered pool could span tens
of thousands of rows.

random_sample() instead: fetches only the (lightweight, id-only) primary
keys of every matching row — a projection query that never hydrates full
Question/Option objects for the whole pool — samples `count` of those ids
in Python (cheap: a list of plain ints, not ORM instances), then fetches
full rows for just that sample. The entire filtered pool's *ids* are still
read once, but the expensive parts (DB-side sort, full-row hydration) only
ever happen for the `count` rows actually needed.
"""
import random


def random_sample(queryset, count):
    """Returns up to `count` rows from `queryset`, in random order, without
    ORDER BY RAND(). Reuses whatever select_related/prefetch_related/
    filters are already on `queryset` for the final fetch — only the id
    projection step ignores them (harmless; a values_list() query doesn't
    join anything it doesn't need). Returns fewer than `count` rows if the
    queryset itself has fewer; returns [] for an empty queryset."""
    ids = list(queryset.values_list('id', flat=True))
    if not ids:
        return []
    if len(ids) > count:
        ids = random.sample(ids, count)
    else:
        random.shuffle(ids)
    rows_by_id = {obj.id: obj for obj in queryset.filter(id__in=ids)}
    return [rows_by_id[i] for i in ids if i in rows_by_id]
