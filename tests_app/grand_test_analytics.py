"""Grand Test 3.0 / GT3-6 — motivation bands, series analytics, and the
missed-vs-appeared recommendation split.

Pure aggregation only (no new TestAttempt-adjacent model) — reuses GT3-2's
grand_test_participation_status() and GT3-1's corrected score/accuracy
fields, matching this engagement's own established 'derive, don't
persist' philosophy already used throughout Grand Test 3.0 (MISSED state,
review windows). The actual weak-topic/subject diagnosis and concrete
practice recommendations for an APPEARED student live in
smart_practice.grand_test_bridge (reusing the existing smart_practice
engine, per this phase's own absolute rule) — this module only supplies
the score-band motivational copy and the series-level view, plus the
narrower, evidence-safe recommendation for a MISSED student who has no
performance data to diagnose."""
from .lifecycle import grand_test_participation_status
from .models import GrandTestMotivationBand, Test, TestAttempt


def motivation_for_score(score_percent):
    """GT3-6 §16 — the admin-configurable score-band lookup
    (GrandTestMotivationBand is a plain, admin-editable table, seeded
    with the approved defaults — see migration 0027). Falls back to a
    safe, generic message if no band matches (an empty or gap-ridden
    table must never crash a result page)."""
    band = (
        GrandTestMotivationBand.objects.filter(min_percent__lte=score_percent, max_percent__gte=score_percent)
        .order_by('-min_percent').first()
    )
    if band:
        return {'title': band.title, 'message': band.message, 'recommended_practice_hint': band.recommended_practice_hint}
    return {
        'title': 'Result Available',
        'message': 'Review your performance below to see where you can improve.',
        'recommended_practice_hint': '',
    }


def _next_grand_test(user, after_test):
    """The next scheduled Grand Test (by scheduled_start) the user holds
    a live entitlement for, strictly after `after_test`'s own schedule —
    reuses the existing GrandTestAccess/scheduled_start data, no new
    query concept."""
    from billing.access import get_grand_test_access
    from billing.models import GrandTestAccess

    if not after_test.scheduled_start:
        return None
    candidates = (
        GrandTestAccess.objects.filter(user=user, revoked_at__isnull=True, test__scheduled_start__gt=after_test.scheduled_start)
        .select_related('test').order_by('test__scheduled_start')
    )
    for access in candidates:
        if get_grand_test_access(user, access.test):  # re-confirms non-revoked, matches existing access pattern
            return access.test
    return None


def _accessible_mock_test(user, can_access_test, has_mock_test_access):
    """The single shared 'find one real, currently-accessible Mock Test'
    lookup — used by both the missed-student path and the appeared-
    student timing recommendation below, so there is exactly one
    definition of 'accessible' (has_mock_test_access already checks
    test.is_pro internally, so a plain free test always passes)."""
    for candidate in Test.objects.filter(exam_type='mock', is_draft=False).order_by('-created_at')[:20]:
        if can_access_test(user, candidate) and has_mock_test_access(user, candidate):
            return candidate
    return None


def missed_student_recommendation(user, test):
    """GT3-6 §18-20 — for a MISSED student, NEVER fabricate current-test
    weakness data (none exists — there is no attempt). Draws only on
    real, verifiable signals: a genuinely accessible Mock Test (the exact
    eligibility rule tests_app._start_attempt/entitlements.services
    already enforce, re-checked here, not re-implemented) and the next
    scheduled Grand Test in this student's own entitlements, if any."""
    from billing.access import has_mock_test_access
    from tests_app.access import can_access_test

    mock_test = _accessible_mock_test(user, can_access_test, has_mock_test_access)
    next_test = _next_grand_test(user, test)

    return {
        'title': f'{test.title} — Missed',
        'message': (
            'You missed the scheduled examination window, so this Grand Test can no longer be attempted. '
            'But your preparation does not have to stop here.'
        ),
        'recommended_action': (
            {
                'type': 'mock_test', 'test_id': mock_test.id, 'title': mock_test.title,
                'reason': (
                    'Practising under a fixed time limit can help you build the discipline needed for the '
                    'next official Grand Test.'
                ),
            }
            if mock_test else None
        ),
        'next_grand_test': (
            {
                'test_id': next_test.id, 'title': next_test.title,
                'scheduled_start': next_test.scheduled_start, 'scheduled_end': next_test.scheduled_end,
            }
            if next_test else None
        ),
    }


def appeared_student_recommendations(user, attempt):
    """GT3-6 §5-15 — top-3 ranked recommendations for an APPEARED Grand
    Test result. Each item carries WHAT/WHY/HOW/FUTURE-BENEFIT text
    plus an access-aware CTA — deliberately cautious language throughout
    ('can help improve', never 'will increase your score by X'), and
    never a CTA for something the student cannot actually do right now
    (a mock-test CTA is only included when one is genuinely accessible).

    Diagnosis and the one safe practice-session mode both come from
    smart_practice.grand_test_bridge (the single, shared bridge into the
    existing engine, per this phase's own absolute rule) — nothing here
    re-implements weakness detection or scoring."""
    from billing.access import has_mock_test_access
    from smart_practice.grand_test_bridge import (
        GrandTestRecommendationError, diagnose_grand_test_performance, resolve_grand_test_source_scope,
    )
    from tests_app.access import can_access_test

    test = attempt.test
    try:
        ctx = resolve_grand_test_source_scope(user, test)
    except GrandTestRecommendationError:
        # No performance data to diagnose from (e.g. called on a stale/
        # non-completed attempt) — never fabricate a recommendation.
        return []
    diagnosis = diagnose_grand_test_performance(ctx)

    recommendations = []

    if diagnosis.weak_subject:
        subject_name = diagnosis.weak_subject['subject_name'] or 'this subject'
        recommendations.append({
            'type': 'smart_practice',
            'what': f'Targeted practice on {subject_name}',
            'why': (
                f'Your accuracy in {subject_name} was {diagnosis.weak_subject["accuracy"]}% in this Grand Test — '
                'your lowest among the subjects you attempted enough questions in to judge.'
            ),
            'how': 'A short Smart Practice set mixing the questions you missed with new questions on your weaker topics.',
            'future_benefit': f'Strengthening {subject_name} can help improve your overall score in future Grand Tests.',
            'cta': {'action': 'practice_now', 'label': 'Practice Now', 'source_test_id': test.id},
        })
    elif diagnosis.weak_topics:
        top_topic = diagnosis.weak_topics[0]
        recommendations.append({
            'type': 'smart_practice',
            'what': f'Targeted practice on {top_topic["topic_name"] or "your weakest topic"}',
            'why': f'Your accuracy on this topic was {top_topic["accuracy"]}% in this Grand Test.',
            'how': 'A short Smart Practice set built from your missed questions plus new ones on this topic.',
            'future_benefit': 'Reinforcing this topic can help improve your consistency in future Grand Tests.',
            'cta': {'action': 'practice_now', 'label': 'Practice Now', 'source_test_id': test.id},
        })

    if diagnosis.unanswered_count > 0:
        recommendations.append({
            'type': 'timing',
            'what': 'Work on completing the full paper within the time limit',
            'why': f'You left {diagnosis.unanswered_count} question(s) unanswered in this Grand Test.',
            'how': 'Practice with a timed Mock Test to build pacing and time management before your next Grand Test.',
            'future_benefit': (
                'Answering more questions within the time limit can help you capture marks you are currently leaving unattempted.'
            ),
            'cta': _mock_test_cta(user, can_access_test, has_mock_test_access),
        })

    if diagnosis.incorrect_count > 0:
        recommendations.append({
            'type': 'revision',
            'what': 'Review your incorrect answers in detail',
            'why': f'You answered {diagnosis.incorrect_count} question(s) incorrectly in this Grand Test.',
            'how': 'Go through the detailed solution review for each missed question, then revisit the underlying concept.',
            'future_benefit': 'Understanding why an answer was wrong can help prevent the same mistake in future exams.',
            'cta': {'action': 'view_review', 'label': 'View Detailed Review', 'test_id': test.id},
        })

    # Never pad with a filler item — fewer than 3 is correct when there
    # isn't a real, distinct signal left to report.
    return recommendations[:3]


def _mock_test_cta(user, can_access_test, has_mock_test_access):
    mock_test = _accessible_mock_test(user, can_access_test, has_mock_test_access)
    if not mock_test:
        return None
    return {'action': 'view_exam', 'label': 'View Mock Test', 'test_id': mock_test.id, 'title': mock_test.title}


def grand_test_series_summary(user):
    """GT3-6 §28-35 — score trend / attendance / personal best across
    every Grand Test this student holds a live (non-revoked) entitlement
    for. Missed tests are NEVER counted as zero-score attempts (§65's
    explicit rule) — average/best/latest/trend are computed over
    COMPLETED (appeared, submitted) tests only.

    Attendance denominator = completed + missed (tests whose window has
    already closed) — an UPCOMING test is deliberately excluded from
    both attendance and trend, since a student cannot yet have 'attended'
    something that hasn't happened."""
    from billing.models import GrandTestAccess

    accesses = (
        GrandTestAccess.objects.filter(user=user, revoked_at__isnull=True)
        .select_related('test').order_by('test__scheduled_start')
    )
    rows = []
    for access in accesses:
        test = access.test
        status_value = grand_test_participation_status(test=test, user=user)
        row = {
            'test_id': test.id, 'test_title': test.title,
            'scheduled_start': test.scheduled_start, 'scheduled_end': test.scheduled_end,
            'status': status_value, 'score_percentage': None,
        }
        if status_value == 'completed':
            attempt = (
                TestAttempt.objects.filter(user=user, test=test, status='submitted')
                .order_by('-start_time').first()
            )
            if attempt and test.total_marks:
                row['score_percentage'] = round(float(attempt.score) / float(test.total_marks) * 100, 2)
        rows.append(row)

    completed = [r for r in rows if r['status'] == 'completed' and r['score_percentage'] is not None]
    missed_count = sum(1 for r in rows if r['status'] == 'missed')
    upcoming_count = sum(1 for r in rows if r['status'] in ('upcoming', 'live', 'in_progress', 'not_scheduled'))
    scores = [r['score_percentage'] for r in completed]

    trend = 'insufficient_data'
    if len(scores) >= 2:
        diffs = [scores[i + 1] - scores[i] for i in range(len(scores) - 1)]
        if all(d >= 0 for d in diffs) and any(d > 0 for d in diffs):
            trend = 'improving'
        elif all(d <= 0 for d in diffs) and any(d < 0 for d in diffs):
            trend = 'declining'
        elif (max(scores) - min(scores)) <= 5:
            trend = 'stable'
        else:
            trend = 'variable'

    attendance_denominator = len(completed) + missed_count
    attendance_percentage = (
        round(len(completed) / attendance_denominator * 100, 1) if attendance_denominator else None
    )

    return {
        'tests': rows,
        'completed_count': len(completed),
        'missed_count': missed_count,
        'upcoming_count': upcoming_count,
        'average_score_percentage': round(sum(scores) / len(scores), 2) if scores else None,
        'best_score_percentage': max(scores) if scores else None,
        'latest_score_percentage': scores[-1] if scores else None,
        'attendance_percentage': attendance_percentage,
        'trend': trend,
    }
