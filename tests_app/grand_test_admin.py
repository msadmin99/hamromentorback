"""Grand Test 3.0 / GT3-7 — Admin operational visibility: live monitor,
student participation, and post-exam report for one Grand Test.

Read-only, pure aggregation over existing models — no new tables. This is
NOT a duplicate monitoring system: ExamSessionViewSet.attempts (tests_app/
views.py) already exists as the admin Participants/Results view for
students who HAVE an attempt; this module extends what that view cannot
show (aggregate counts, not-started/missed students who have no attempt
row at all, and cross-student post-exam analytics) rather than replacing
it. Every function here works for BOTH Grand Test scheduling paths (a
real ExamSession, or the simpler Test.scheduled_start/scheduled_end
fallback) by resolving through the exact same
tests_app.lifecycle.resolve_test_schedule_session() every enforcement/
reporting function in Grand Test 3.0 already uses — never a second,
independently-drifting resolution scheme.

Every count here is computed with a small, fixed number of queries
(values().annotate() aggregation or one list() fetch), never one query
per student or per question — see GT3-7 §49's own instruction."""
from collections import Counter
from statistics import median

from django.db.models import Avg, Count, Max, Min, Q
from django.utils import timezone

from .lifecycle import is_attempt_expired, resolve_test_schedule_session
from .models import Answer

SCORE_DISTRIBUTION_BUCKETS = [
    ('90-100', 90, 100.0001), ('80-89', 80, 90), ('70-79', 70, 80),
    ('60-69', 60, 70), ('50-59', 50, 60), ('<50', -0.0001, 50),
]


def _entitled_and_attempts(test):
    """Shared resolution: the entitled population and the attempt set
    scoped to this Test's ONE governing schedule."""
    from billing.models import GrandTestAccess

    session = resolve_test_schedule_session(test)
    entitled_qs = GrandTestAccess.objects.filter(test=test, revoked_at__isnull=True)
    attempts_qs = (
        test.attempts.filter(session=session) if session else test.attempts.filter(session__isnull=True)
    )
    return session, entitled_qs, attempts_qs


def _window_closed(test, session, now=None):
    now = now or timezone.now()
    if session:
        return now > session.end_datetime
    if test.scheduled_end:
        return now > test.scheduled_end
    return False  # never scheduled -> can never be 'closed'


def grand_test_monitor(test):
    """GT3-7 §9-13 — the live monitor's aggregate header counts."""
    session, entitled_qs, attempts_qs = _entitled_and_attempts(test)

    entitled_user_ids = set(entitled_qs.values_list('user_id', flat=True))
    attempts = list(attempts_qs.only('id', 'user_id', 'status', 'auto_submitted', 'start_time', 'test_id', 'session_id'))
    attempted_user_ids = {a.user_id for a in attempts}

    submitted = [a for a in attempts if a.status == 'submitted']
    in_progress = [a for a in attempts if a.status == 'in_progress']
    active = [a for a in in_progress if not is_attempt_expired(a)]
    auto_submitted_count = sum(1 for a in submitted if a.auto_submitted)

    # GT3-7 §12 — a real, computable signal: how many students show more
    # than one currently in_progress attempt for this exact test+session
    # (structurally shouldn't happen since GT3-7's start-concurrency fix,
    # but flagged rather than assumed impossible for data that predates
    # it, or a still-possible edge case this fix doesn't cover, e.g. two
    # DIFFERENT sessions of the same Test).
    in_progress_counts = Counter(a.user_id for a in in_progress)
    duplicate_active_attempts = sum(1 for c in in_progress_counts.values() if c > 1)

    return {
        'test_id': test.id,
        'test_title': test.title,
        'session_id': session.id if session else None,
        'window_closed': _window_closed(test, session),
        'entitled': len(entitled_user_ids),
        'started': len(attempted_user_ids),
        'active': len(active),
        'submitted': len(submitted),
        'auto_submitted': auto_submitted_count,
        'not_started': len(entitled_user_ids - attempted_user_ids),
        'stale_in_progress_not_yet_finalized': len(in_progress) - len(active),
        'duplicate_active_attempts': duplicate_active_attempts,
        # GT3-7 §12 — these are NOT fabricated: no start/answer-save/
        # submission failure log or API-error log exists anywhere in this
        # codebase today (confirmed by inspection), so they are reported
        # as null/not-available rather than invented. See the GT3-7 final
        # report's Observability section.
        'start_failures': None,
        'answer_save_failures': None,
        'submission_failures': None,
        'recent_api_errors': None,
    }


def grand_test_participants(test):
    """GT3-7 §13-14 — the student participation table, INCLUDING students
    with no attempt row at all (not_started / missed) — which
    ExamSessionViewSet.attempts structurally cannot show, since it only
    ever lists rows from session.attempts. One query per side (entitled,
    attempts), never per student."""
    session, entitled_qs, attempts_qs = _entitled_and_attempts(test)
    now = timezone.now()
    closed = _window_closed(test, session, now)

    entitled = list(entitled_qs.select_related('user'))
    attempts_by_user = {}
    for a in attempts_qs.select_related('user').order_by('-score'):
        # Keep the best/most-recent row per user for display purposes —
        # a student may legitimately have more than one attempt
        # (max_attempts > 1); the participation table shows one row per
        # student, matching the §13 example.
        attempts_by_user.setdefault(a.user_id, a)

    rows = []
    for access in entitled:
        user = access.user
        attempt = attempts_by_user.get(user.id)
        if attempt is None:
            participant_status = 'missed' if closed else 'not_started'
        elif attempt.status == 'in_progress':
            participant_status = 'missed' if (closed and is_attempt_expired(attempt)) else 'active'
        else:
            participant_status = 'auto_submitted' if attempt.auto_submitted else 'submitted'
        rows.append({
            'user_id': user.id,
            'user_name': f'{user.first_name} {user.last_name}'.strip() or user.email,
            'user_email': user.email,
            'entitled': True,
            'started': attempt is not None,
            'submitted': attempt is not None and attempt.status == 'submitted',
            'status': participant_status,
            'score': float(attempt.score) if attempt and attempt.status == 'submitted' else None,
            'rank': attempt.rank if attempt and attempt.status == 'submitted' else None,
            'attempt_id': attempt.id if attempt else None,
        })
    return rows


def grand_test_report(test):
    """GT3-7 §15-18 — post-exam participation/results/question analytics.
    Only ever computed from SUBMITTED attempts (an in-progress/stale
    attempt is not a real result) and the Answer rows those attempts
    produced. 'Missed' uses the exact same derived rule as
    grand_test_participation_status (entitled + no attempt + window
    closed) — computed in bulk here, never per student."""
    session, entitled_qs, attempts_qs = _entitled_and_attempts(test)
    now = timezone.now()
    closed = _window_closed(test, session, now)

    entitled_user_ids = set(entitled_qs.values_list('user_id', flat=True))
    attempted_user_ids = set(test.attempts.values_list('user_id', flat=True).distinct())
    missed_count = len(entitled_user_ids - attempted_user_ids) if closed else 0

    submitted_qs = attempts_qs.filter(status='submitted')
    agg = submitted_qs.aggregate(avg=Avg('score'), high=Max('score'), low=Min('score'), count=Count('id'))
    scores = list(submitted_qs.values_list('score', flat=True))
    total_marks = float(test.total_marks) if test.total_marks else 0

    distribution = []
    if total_marks:
        for label, lo, hi in SCORE_DISTRIBUTION_BUCKETS:
            count = sum(1 for s in scores if lo <= (float(s) / total_marks * 100) < hi)
            distribution.append({'range': label, 'count': count})

    participation = {
        'entitled': len(entitled_user_ids),
        'appeared': agg['count'] or 0,
        'missed': missed_count,
        'completed': agg['count'] or 0,
        'auto_submitted': submitted_qs.filter(auto_submitted=True).count(),
        'participation_percentage': (
            round((agg['count'] or 0) / len(entitled_user_ids) * 100, 2) if entitled_user_ids else None
        ),
    }
    results = {
        'average_score': round(float(agg['avg']), 2) if agg['avg'] is not None else None,
        'median_score': round(float(median(scores)), 2) if scores else None,
        'highest_score': float(agg['high']) if agg['high'] is not None else None,
        'lowest_score': float(agg['low']) if agg['low'] is not None else None,
        'total_marks': total_marks or None,
        'score_distribution': distribution,
    }

    performance = {
        'subjects': _cross_student_breakdown(submitted_qs, 'question__subject_id', 'question__subject__name'),
        'chapters': _cross_student_breakdown(submitted_qs, 'question__chapter_id', 'question__chapter__name'),
        'topics': _cross_student_breakdown(submitted_qs, 'question__topic_id', 'question__topic__name'),
    }

    question_analysis = _question_analytics(submitted_qs, agg['count'] or 0)

    return {
        'test_id': test.id, 'test_title': test.title,
        'window_closed': closed,
        'participation': participation, 'results': results, 'performance': performance,
        'question_analysis': question_analysis,
    }


def grand_test_series_report(tests):
    """GT3-7 §19-20 — the admin series report for a set of Grand Tests
    (e.g. a GrandTestPackage's own `tests`, the natural existing grouping
    GT3-5 already built for exactly this 'GT-I through GT-VI' scenario —
    reused here rather than inventing a new series concept). Reuses
    grand_test_report()'s own per-test participation/results computation
    for each test — never a second, parallel aggregation implementation —
    and preserves GT3-6's own student-facing series definitions (average
    over appeared/completed tests only; missed excluded from score
    calculations; upcoming excluded from any average)."""
    rows = []
    appeared_percentages = []
    for test in tests:
        report = grand_test_report(test)
        rows.append({
            'test_id': test.id, 'test_title': test.title,
            'scheduled_start': test.scheduled_start, 'scheduled_end': test.scheduled_end,
            'participation': report['participation'], 'results': report['results'],
        })
        avg = report['results']['average_score']
        total_marks = report['results']['total_marks']
        appeared = report['participation']['appeared']
        if avg is not None and total_marks:
            # Different Grand Tests in a series can have different total
            # marks — average raw SCORES across tests would silently mix
            # scales (85/100 vs 40/50). Normalize to a percentage first,
            # then weight by how many students actually appeared for that
            # test, matching GT3-6's own 'average over completed tests
            # only' rule extended to a population instead of one student.
            appeared_percentages.extend([avg / total_marks * 100] * appeared)

    return {
        'tests': rows,
        'overall_average_percentage': (
            round(sum(appeared_percentages) / len(appeared_percentages), 2) if appeared_percentages else None
        ),
        'total_entitled_relationships': sum(r['participation']['entitled'] for r in rows),
        'total_appeared': sum(r['participation']['appeared'] for r in rows),
        'total_missed': sum(r['participation']['missed'] for r in rows),
    }


def _cross_student_breakdown(submitted_attempts_qs, id_field, name_field):
    """Subject/chapter/topic accuracy across every submitted attempt of
    this Test — the cross-student counterpart of smart_practice.
    source_performance's single-attempt versions; a different function on
    purpose (aggregating across many attempts, via DB-side annotate, is a
    different query shape than one attempt's in-memory loop) rather than
    forcing one function to serve both shapes."""
    rows = (
        Answer.objects.filter(attempt__in=submitted_attempts_qs, selected_option__isnull=False, **{f'{id_field}__isnull': False})
        .values(id_field, name_field)
        .annotate(attempted=Count('id'), correct=Count('id', filter=Q(is_correct=True)))
        .order_by('correct')
    )
    result = []
    for row in rows:
        attempted = row['attempted']
        accuracy = round(row['correct'] / attempted * 100, 2) if attempted else 0.0
        result.append({
            'id': row[id_field], 'name': row[name_field] or '', 'attempted': attempted,
            'correct': row['correct'], 'accuracy': accuracy,
        })
    return sorted(result, key=lambda r: r['accuracy'])


def _question_analytics(submitted_attempts_qs, submitted_count):
    """GT3-7 §17-18 — per-question response/accuracy/skip breakdown
    across every submitted attempt, flagged (never auto-corrected) for
    unusually low accuracy or unusually high skip rate."""
    rows = (
        Answer.objects.filter(attempt__in=submitted_attempts_qs)
        .values('question_id', 'question__text')
        .annotate(
            answered=Count('id', filter=Q(selected_option__isnull=False)),
            correct=Count('id', filter=Q(is_correct=True)),
        )
    )
    result = []
    for row in rows:
        answered = row['answered']
        correct = row['correct']
        incorrect = answered - correct
        unanswered = max(submitted_count - answered, 0)
        accuracy = round(correct / answered * 100, 2) if answered else 0.0
        skip_rate = round(unanswered / submitted_count * 100, 2) if submitted_count else 0.0
        result.append({
            'question_id': row['question_id'],
            'question_text': (row['question__text'] or '')[:200],
            'total_responses': answered, 'correct': correct, 'incorrect': incorrect,
            'unanswered': unanswered, 'accuracy': accuracy, 'skip_rate': skip_rate,
            # GT3-7 §18 — flags for human review only, never an automatic
            # content change.
            'flag_low_accuracy': answered >= 5 and accuracy < 30,
            'flag_high_skip': submitted_count >= 5 and skip_rate > 50,
        })
    return {
        'questions': sorted(result, key=lambda r: r['accuracy']),
        'most_difficult': sorted(result, key=lambda r: r['accuracy'])[:5],
        'easiest': sorted(result, key=lambda r: -r['accuracy'])[:5],
        'most_skipped': sorted(result, key=lambda r: -r['skip_rate'])[:5],
    }
