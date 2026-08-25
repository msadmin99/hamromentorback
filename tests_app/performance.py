"""Pure aggregation functions for the student 'My Performance' dashboard —
read-only, derived entirely from existing TestAttempt/Answer/QuestionAttempt/
QuestionEvent/VideoProgress rows. No new models.

Deliberately NOT computed here, because the underlying data doesn't exist on
this platform: XP/points/badges and in-app notifications. `question_analytics()`
reports its own remaining gaps explicitly via a `not_tracked` key rather than
silently returning zero. Per-question difficulty, reattempt history, and a
real daily-questions count (`kpi_overview`'s `questions_today`, added once
academics.QuestionEvent started logging every answer) now exist — see
academics.services.record_question_result.
"""
from collections import defaultdict
from datetime import datetime, timedelta

from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

from academics.models import Chapter, QuestionAttempt, QuestionEvent, Subject, Topic

from .access import visible_test_queryset
from .models import Answer, Test, TestAttempt

MIN_ATTEMPTS_FOR_SUBJECT_RANK = 5


def _effective_course_ids(user, course=None):
    """The course scope to filter CATALOG reads by (Subject/Test/Video rows
    not already scoped to `user=user`) — never trusts a client-supplied
    `course` blindly. If a specific course is requested, it must be one the
    student is actually enrolled in, else it's simply not honored (empty
    set — 'return zero results for unauthorized content', not a silent
    fallback to something wider). If no course is requested, defaults to
    every course the student is enrolled in — never 'unfiltered
    platform-wide', which is what every function below did before this
    fix whenever its `course` argument was left unset."""
    from courses.access import eligible_course_ids

    eligible = eligible_course_ids(user)
    if course:
        return {course} if course in eligible else set()
    return eligible


def _attempts_qs(user, course=None, date_from=None, date_to=None, exam_types=None):
    qs = TestAttempt.objects.filter(user=user, status='submitted').select_related('test')
    if course:
        qs = qs.filter(Q(test__courses__id=course) | Q(test__courses__isnull=True))
    if date_from:
        qs = qs.filter(start_time__gte=date_from)
    if date_to:
        qs = qs.filter(start_time__lte=date_to)
    if exam_types:
        qs = qs.filter(test__exam_type__in=exam_types)
    return qs.distinct()


def _answer_stats_by_attempt(attempt_ids):
    rows = (
        Answer.objects.filter(attempt_id__in=attempt_ids)
        .values('attempt_id')
        .annotate(answered=Count('id', filter=Q(selected_option__isnull=False)), correct=Count('id', filter=Q(is_correct=True)))
    )
    return {row['attempt_id']: row for row in rows}


def _activity_streak(user, course=None):
    dates = set()

    qa_qs = QuestionAttempt.objects.filter(user=user)
    if course:
        qa_qs = qa_qs.filter(question__courses__id=course)
    dates.update(qa_qs.values_list('answered_at__date', flat=True))

    ta_qs = TestAttempt.objects.filter(user=user, status='submitted')
    if course:
        ta_qs = ta_qs.filter(Q(test__courses__id=course) | Q(test__courses__isnull=True))
    dates.update(ta_qs.values_list('start_time__date', flat=True))

    from videos_app.models import VideoProgress

    vp_qs = VideoProgress.objects.filter(user=user)
    if course:
        vp_qs = vp_qs.filter(video__courses__id=course)
    dates.update(vp_qs.values_list('last_watched_at__date', flat=True))

    dates.discard(None)
    if not dates:
        return {'current': 0, 'longest': 0}

    sorted_dates = sorted(dates)
    longest = run = 1
    for i in range(1, len(sorted_dates)):
        if (sorted_dates[i] - sorted_dates[i - 1]).days == 1:
            run += 1
            longest = max(longest, run)
        else:
            run = 1

    date_set = set(sorted_dates)
    today = timezone.localdate()
    cursor = today if today in date_set else today - timedelta(days=1)
    current = 0
    while cursor in date_set:
        current += 1
        cursor -= timedelta(days=1)

    return {'current': current, 'longest': longest}


def kpi_overview(user, course=None, date_from=None, date_to=None):
    attempts = list(_attempts_qs(user, course, date_from, date_to))
    attempt_ids = [a.id for a in attempts]
    answer_stats = _answer_stats_by_attempt(attempt_ids)

    question_count_cache = {}
    total_questions = total_answered = total_correct = 0
    total_score = 0.0
    attempt_seconds = 0.0
    for a in attempts:
        if a.test_id not in question_count_cache:
            question_count_cache[a.test_id] = a.test.question_count
        total_questions += question_count_cache[a.test_id]
        stats = answer_stats.get(a.id, {'answered': 0, 'correct': 0})
        total_answered += stats['answered']
        total_correct += stats['correct']
        total_score += float(a.score)
        if a.end_time:
            attempt_seconds += (a.end_time - a.start_time).total_seconds()

    total_incorrect = total_answered - total_correct
    total_unanswered = max(total_questions - total_answered, 0)
    overall_accuracy = round(total_correct / total_answered * 100, 2) if total_answered else 0.0

    from videos_app.models import VideoProgress

    video_qs = VideoProgress.objects.filter(user=user)
    if course:
        video_qs = video_qs.filter(video__courses__id=course)
    video_seconds = video_qs.aggregate(total=Sum('max_position_seconds'))['total'] or 0

    total_study_seconds = int(attempt_seconds + video_seconds)
    avg_seconds_per_question = round(attempt_seconds / total_answered, 1) if total_answered else 0.0
    streak = _activity_streak(user, course)

    # Distinct questions answered today (QBank practice + every test type,
    # since QuestionEvent is written platform-wide) — powers the Home page's
    # Daily Goal widget. Independent of this function's own date_from/date_to
    # window, which "today" isn't subject to.
    today_qs = QuestionEvent.objects.filter(user=user, created_at__date=timezone.localdate())
    if course:
        today_qs = today_qs.filter(question__courses__id=course)
    questions_today = today_qs.values('question').distinct().count()

    return {
        'total_attempts': len(attempts),
        'questions_attempted': total_answered,
        'questions_correct': total_correct,
        'questions_incorrect': total_incorrect,
        'questions_unanswered': total_unanswered,
        'overall_accuracy': overall_accuracy,
        'overall_score': round(total_score, 2),
        'total_study_seconds': total_study_seconds,
        'avg_seconds_per_question': avg_seconds_per_question,
        'current_streak_days': streak['current'],
        'longest_streak_days': streak['longest'],
        'questions_today': questions_today,
    }


def trend_series(user, course=None, date_from=None, date_to=None, granularity='day'):
    attempts = list(_attempts_qs(user, course, date_from, date_to).order_by('start_time'))
    answer_stats = _answer_stats_by_attempt([a.id for a in attempts])

    buckets = defaultdict(lambda: {'scores': [], 'accuracies': [], 'seconds_per_q': [], 'ranks': [], 'percentiles': []})
    for a in attempts:
        if granularity == 'month':
            key = a.start_time.strftime('%Y-%m')
        elif granularity == 'week':
            iso = a.start_time.isocalendar()
            key = f'{iso[0]}-W{iso[1]:02d}'
        else:
            key = a.start_time.strftime('%Y-%m-%d')

        b = buckets[key]
        b['scores'].append(float(a.score))
        b['accuracies'].append(float(a.accuracy))
        if a.end_time:
            stats = answer_stats.get(a.id, {'answered': 0})
            if stats['answered']:
                b['seconds_per_q'].append((a.end_time - a.start_time).total_seconds() / stats['answered'])
        if a.rank:
            b['ranks'].append(a.rank)
        if a.percentile is not None:
            b['percentiles'].append(float(a.percentile))

    def _avg(values, digits=2):
        return round(sum(values) / len(values), digits) if values else None

    return [
        {
            'date': key,
            'attempts': len(buckets[key]['scores']),
            'avg_score': _avg(buckets[key]['scores']) or 0,
            'avg_accuracy': _avg(buckets[key]['accuracies']) or 0,
            'avg_seconds_per_question': _avg(buckets[key]['seconds_per_q'], 1),
            'avg_rank': _avg(buckets[key]['ranks'], 1),
            'avg_percentile': _avg(buckets[key]['percentiles']),
        }
        for key in sorted(buckets.keys())
    ]


def _combined_question_state(user, course=None):
    """A student can solve the same question two ways: ad-hoc QBank practice
    (QuestionAttempt) or taking a formal Test — including exam_type='qbank'
    Subject Tests, which are just as real (Answer, via a submitted
    TestAttempt). Subject/chapter/topic mastery must reflect both, or a
    student who only ever takes Subject Tests would show 0% everywhere
    despite having real activity. Merges to one row per distinct question —
    the most recent record (by timestamp) wins if solved via both paths.
    Returns {question_id: is_correct}.
    """
    state = {}  # question_id -> {'is_correct': bool, 'when': datetime}

    qa_qs = QuestionAttempt.objects.filter(user=user).values('question_id', 'is_correct', 'answered_at')
    if course:
        qa_qs = qa_qs.filter(question__courses__id=course)
    for row in qa_qs:
        state[row['question_id']] = {'is_correct': row['is_correct'], 'when': row['answered_at']}

    ans_qs = (
        Answer.objects.filter(attempt__user=user, attempt__status='submitted', selected_option__isnull=False)
        .values('question_id', 'is_correct', 'attempt__end_time', 'attempt__start_time')
    )
    if course:
        ans_qs = ans_qs.filter(question__courses__id=course)
    for row in ans_qs:
        when = row['attempt__end_time'] or row['attempt__start_time']
        existing = state.get(row['question_id'])
        if not existing or when > existing['when']:
            state[row['question_id']] = {'is_correct': row['is_correct'], 'when': when}

    return {qid: rec['is_correct'] for qid, rec in state.items()}


def _subject_rank(user, subject):
    """Cross-student ranking. Unlike _combined_question_state (which dedupes
    to one 'current mastery' state per question for the owning student's own
    dashboard), ranking sums every solve-event from both sources per user —
    a coarse, defensible 'how much have you engaged with this subject and
    how accurately' figure, without needing a latest-wins merge across every
    student's data in one query."""
    combined = defaultdict(lambda: {'attempted': 0, 'correct': 0})

    for row in (
        QuestionAttempt.objects.filter(question__subject=subject)
        .values('user_id')
        .annotate(attempted=Count('id'), correct=Count('id', filter=Q(is_correct=True)))
    ):
        combined[row['user_id']]['attempted'] += row['attempted']
        combined[row['user_id']]['correct'] += row['correct']

    for row in (
        Answer.objects.filter(question__subject=subject, selected_option__isnull=False, attempt__status='submitted')
        .values('attempt__user_id')
        .annotate(attempted=Count('id'), correct=Count('id', filter=Q(is_correct=True)))
    ):
        combined[row['attempt__user_id']]['attempted'] += row['attempted']
        combined[row['attempt__user_id']]['correct'] += row['correct']

    eligible = {uid: v for uid, v in combined.items() if v['attempted'] >= MIN_ATTEMPTS_FOR_SUBJECT_RANK}
    ranked = sorted(eligible.items(), key=lambda kv: kv[1]['correct'] / kv[1]['attempted'], reverse=True)
    for idx, (uid, _v) in enumerate(ranked, start=1):
        if uid == user.id:
            return {'rank': idx, 'out_of': len(ranked)}
    return None


def subject_breakdown(user, course=None):
    from academics.models import Question

    state = _combined_question_state(user, course)
    questions = Question.objects.filter(id__in=state.keys()).values('id', 'subject_id', 'marks', 'negative_marks')
    q_by_id = {q['id']: q for q in questions}

    per_subject = defaultdict(lambda: {'attempted': 0, 'correct': 0, 'score': 0.0})
    for qid, is_correct in state.items():
        q = q_by_id.get(qid)
        if not q:
            continue
        bucket = per_subject[q['subject_id']]
        bucket['attempted'] += 1
        if is_correct:
            bucket['correct'] += 1
            bucket['score'] += float(q['marks'])
        else:
            bucket['score'] -= float(q['negative_marks'])

    # Was Subject.objects.all() (unfiltered when course omitted) — listed
    # every subject platform-wide, across every unrelated program, in a
    # student's own subject breakdown (with an honest attempted=0 for ones
    # they could never have touched, but still disclosing subject names/
    # curriculum from courses they aren't enrolled in).
    from django.db.models import Q as _Q

    course_ids = _effective_course_ids(user, course)
    subjects_qs = Subject.objects.filter(_Q(courses__isnull=True) | _Q(courses__id__in=course_ids))

    rows = []
    for subject in subjects_qs.distinct():
        agg = per_subject.get(subject.id, {'attempted': 0, 'correct': 0, 'score': 0.0})
        attempted = agg['attempted']
        correct = agg['correct']
        accuracy = round(correct / attempted * 100, 2) if attempted else 0.0
        total_questions = subject.questions.count()
        completion = round(attempted / total_questions * 100, 2) if total_questions else 0.0

        if attempted == 0 and total_questions == 0:
            continue
        rows.append({
            'subject_id': subject.id,
            'subject_name': subject.name,
            'attempted': attempted,
            'correct': correct,
            'incorrect': attempted - correct,
            'accuracy': accuracy,
            'score': round(agg['score'], 2),
            'completion_percent': completion,
            'total_questions': total_questions,
            'rank': _subject_rank(user, subject),
        })
    rows.sort(key=lambda r: r['accuracy'])
    return rows


def topic_mastery(user, subject_id, state=None):
    if state is None:
        state = _combined_question_state(user)

    from academics.models import Question

    topics = list(Topic.objects.filter(chapter__subject_id=subject_id))
    topic_of_question = dict(
        Question.objects.filter(topic__in=topics).values_list('id', 'topic_id')
    )

    per_topic = defaultdict(lambda: {'attempted': 0, 'correct': 0})
    for qid, is_correct in state.items():
        topic_id = topic_of_question.get(qid)
        if topic_id is None:
            continue
        bucket = per_topic[topic_id]
        bucket['attempted'] += 1
        if is_correct:
            bucket['correct'] += 1

    result = []
    for topic in topics:
        agg = per_topic.get(topic.id, {'attempted': 0, 'correct': 0})
        attempted = agg['attempted']
        correct = agg['correct']
        accuracy = round(correct / attempted * 100, 2) if attempted else 0.0

        if attempted == 0:
            bucket = 'not_started'
        elif accuracy >= 90:
            bucket = 'mastered'
        elif accuracy >= 75:
            bucket = 'strong'
        elif accuracy >= 50:
            bucket = 'average'
        else:
            bucket = 'weak'

        result.append({
            'topic_id': topic.id,
            'topic_name': topic.name,
            'chapter_id': topic.chapter_id,
            'attempted': attempted,
            'accuracy': accuracy,
            'mastery': bucket,
        })
    return result


def chapter_breakdown(user, subject_id):
    from academics.models import Question

    state = _combined_question_state(user)
    chapters = list(Chapter.objects.filter(subject_id=subject_id))
    chapter_of_question = dict(
        Question.objects.filter(chapter__in=chapters).values_list('id', 'chapter_id')
    )

    per_chapter = defaultdict(lambda: {'attempted': 0, 'correct': 0})
    for qid, is_correct in state.items():
        chapter_id = chapter_of_question.get(qid)
        if chapter_id is None:
            continue
        bucket = per_chapter[chapter_id]
        bucket['attempted'] += 1
        if is_correct:
            bucket['correct'] += 1

    rows = []
    for chapter in chapters:
        agg = per_chapter.get(chapter.id, {'attempted': 0, 'correct': 0})
        attempted = agg['attempted']
        correct = agg['correct']
        accuracy = round(correct / attempted * 100, 2) if attempted else 0.0
        total_questions = chapter.questions.count()
        completion = round(attempted / total_questions * 100, 2) if total_questions else 0.0

        if attempted == 0:
            confidence = 'not_started'
        elif accuracy >= 70:
            confidence = 'strong'
        elif accuracy >= 50:
            confidence = 'moderate'
        else:
            confidence = 'needs_improvement'

        rows.append({
            'chapter_id': chapter.id,
            'chapter_name': chapter.name,
            'attempted': attempted,
            'correct': correct,
            'accuracy': accuracy,
            'completion_percent': completion,
            'questions_remaining': max(total_questions - attempted, 0),
            'total_questions': total_questions,
            'confidence': confidence,
        })
    rows.sort(key=lambda r: r['accuracy'])
    return {'chapters': rows, 'topics': topic_mastery(user, subject_id, state=state)}


def mock_test_analytics(user, course=None):
    attempts = list(_attempts_qs(user, course, exam_types=['mock']).order_by('start_time'))
    if not attempts:
        return {'total_taken': 0, 'avg_score': 0, 'best_score': 0, 'worst_score': 0, 'trend': []}
    scores = [float(a.score) for a in attempts]
    return {
        'total_taken': len(attempts),
        'avg_score': round(sum(scores) / len(scores), 2),
        'best_score': max(scores),
        'worst_score': min(scores),
        'trend': [
            {'date': a.start_time.date().isoformat(), 'score': float(a.score), 'test_id': a.test_id, 'test_title': a.test.title}
            for a in attempts
        ],
    }


def exam_type_stats(user, exam_type, course=None):
    """Generalization of mock_test_analytics for any exam_type — powers the
    'Your <Exam Type> Stats' panel on each test-listing page. Reports
    accuracy (%) rather than raw score, since raw score can go negative
    (negative marking) and isn't comparable across tests with different
    question counts — a percentage is what a "78% Average Score" style
    stat actually means. Also adds total_time, which mock_test_analytics
    doesn't compute."""
    attempts = list(_attempts_qs(user, course, exam_types=[exam_type]))
    if not attempts:
        return {'total_taken': 0, 'avg_accuracy': 0, 'best_accuracy': 0, 'total_time_seconds': 0}
    accuracies = [float(a.accuracy) for a in attempts]
    total_seconds = sum(
        (a.end_time - a.start_time).total_seconds() for a in attempts if a.end_time
    )
    return {
        'total_taken': len(attempts),
        'avg_accuracy': round(sum(accuracies) / len(accuracies), 2),
        'best_accuracy': max(accuracies),
        'total_time_seconds': round(total_seconds),
    }


def question_analytics(user, course=None):
    attempt_ids = list(_attempts_qs(user, course).values_list('id', flat=True))
    answers = Answer.objects.filter(attempt_id__in=attempt_ids)

    qa_qs = QuestionAttempt.objects.filter(user=user)
    if course:
        qa_qs = qa_qs.filter(question__courses__id=course)

    return {
        'exam_answered': answers.filter(selected_option__isnull=False).count(),
        'exam_correct': answers.filter(is_correct=True).count(),
        'exam_incorrect': answers.filter(is_correct=False, selected_option__isnull=False).count(),
        'exam_skipped': answers.filter(selected_option__isnull=True).count(),
        'exam_flagged_for_review': answers.filter(is_marked_for_review=True).count(),
        'qbank_solved': qa_qs.count(),
        'qbank_correct': qa_qs.filter(is_correct=True).count(),
        'qbank_bookmarked': qa_qs.filter(is_bookmarked=True).count(),
        'not_tracked': ['reattempts', 'correct_after_revision', 'difficulty_breakdown'],
    }


def strengths_and_weaknesses(user, course=None):
    from academics.models import Question

    subjects = subject_breakdown(user, course)
    attempted_subjects = [s for s in subjects if s['attempted'] >= MIN_ATTEMPTS_FOR_SUBJECT_RANK]

    strong = sorted([s for s in attempted_subjects if s['accuracy'] >= 75], key=lambda s: -s['accuracy'])[:3]
    weak = sorted([s for s in attempted_subjects if s['accuracy'] < 50], key=lambda s: s['accuracy'])[:3]

    state = _combined_question_state(user, course)
    wrong_question_ids = [qid for qid, is_correct in state.items() if not is_correct]
    wrong_questions = Question.objects.filter(id__in=wrong_question_ids).values('subject_id', 'negative_marks')
    marks_lost_by_subject = defaultdict(float)
    for q in wrong_questions:
        marks_lost_by_subject[q['subject_id']] += float(q['negative_marks'])

    subject_names = {s['subject_id']: s['subject_name'] for s in subjects}
    negative_impact = [
        {'subject_id': sid, 'subject_name': subject_names.get(sid, ''), 'marks_lost': round(lost, 2)}
        for sid, lost in marks_lost_by_subject.items() if lost > 0
    ]
    negative_impact.sort(key=lambda r: -r['marks_lost'])

    mock = mock_test_analytics(user, course)
    recent_scores = [t['score'] for t in mock['trend'][-5:]]
    consistent_scores = False
    if len(recent_scores) >= 3:
        mean = sum(recent_scores) / len(recent_scores)
        variance = sum((x - mean) ** 2 for x in recent_scores) / len(recent_scores)
        consistent_scores = mean > 0 and (variance ** 0.5) < (mean * 0.15)

    recent_attempts = list(_attempts_qs(user, course).order_by('-start_time')[:10])
    answer_stats = _answer_stats_by_attempt([a.id for a in recent_attempts])
    fast_count = slow_count = 0
    for a in recent_attempts:
        if not a.end_time:
            continue
        answered = answer_stats.get(a.id, {'answered': 0})['answered']
        if not answered:
            continue
        actual = (a.end_time - a.start_time).total_seconds() / answered
        budget = (a.test.duration_minutes * 60) / max(a.test.question_count, 1)
        if actual < budget * 0.7:
            fast_count += 1
        elif actual > budget * 1.1:
            slow_count += 1

    return {
        'strong_subjects': strong,
        'weak_subjects': weak,
        'high_negative_marking_subjects': negative_impact[:3],
        'consistent_scores': consistent_scores,
        'fast_response': fast_count > 0 and fast_count > slow_count,
        'slow_response': slow_count > 0 and slow_count > fast_count,
    }


def recommendations(user, course=None):
    from tests_app.models import Test as TestModel
    from videos_app.models import Video

    subjects = subject_breakdown(user, course)
    attempted = [s for s in subjects if s['attempted'] >= 3]
    weakest = sorted(attempted, key=lambda s: s['accuracy'])[:3]

    # `subjects`/`weakest` are already course-eligible (subject_breakdown is
    # fixed above), but a shared subject (e.g. Anatomy under both CEE-PG and
    # NMCLE) can have a Test/Video scoped to only ONE of those courses —
    # `course` being optional here previously meant these were unfiltered by
    # course whenever omitted, so a CEE-PG student could still be handed a
    # suggested_test_id/suggested_video_id from an NMCLE-only resource.
    course_ids = _effective_course_ids(user, course)
    suggestions = []
    for s in weakest:
        video_qs = Video.objects.filter(subject_id=s['subject_id']).filter(
            Q(courses__isnull=True) | Q(courses__id__in=course_ids)
        )
        video = video_qs.first()

        test_qs = visible_test_queryset(user, TestModel.objects.filter(subject_id=s['subject_id']))
        if course:
            test_qs = test_qs.filter(courses__id=course)
        practice_test = test_qs.filter(exam_type='qbank').first() or test_qs.first()

        suggestions.append({
            'type': 'revise_subject',
            'subject_id': s['subject_id'],
            'subject_name': s['subject_name'],
            'accuracy': s['accuracy'],
            'message': f"Your accuracy in {s['subject_name']} is {s['accuracy']}% — revise this subject.",
            'suggested_video_id': video.id if video else None,
            'suggested_test_id': practice_test.id if practice_test else None,
        })

    last_mock = _attempts_qs(user, course, exam_types=['mock']).order_by('-start_time').first()
    if not last_mock:
        suggestions.append({'type': 'take_mock_test', 'message': "You haven't taken a mock test yet — try one to see where you stand."})
    else:
        days_since = (timezone.now() - last_mock.start_time).days
        if days_since >= 14:
            suggestions.append({
                'type': 'take_mock_test',
                'message': f"It's been {days_since} days since your last mock test — take one to check your progress.",
            })

    week_ago = timezone.now() - timedelta(days=7)
    recent_qa = QuestionAttempt.objects.filter(user=user, answered_at__gte=week_ago)
    if course:
        recent_qa = recent_qa.filter(question__courses__id=course)
    avg_daily = recent_qa.count() / 7
    suggested_daily_goal = max(10, round(avg_daily * 1.2))

    return {
        'suggestions': suggestions,
        'suggested_daily_question_goal': suggested_daily_goal,
        'note': "Rule-based suggestions from your own performance data — not AI-generated.",
    }


def comparative(user, attempt_id):
    attempt = TestAttempt.objects.select_related('test').get(pk=attempt_id, user=user)
    same_test = TestAttempt.objects.filter(user=user, test=attempt.test, status='submitted')
    previous = same_test.filter(attempt_number__lt=attempt.attempt_number).order_by('-attempt_number').first()
    # Best attempt INCLUDES the current one — if the current attempt is itself
    # the best, that's the correct, honest answer (not a stale earlier score).
    best = same_test.order_by('-score').first()
    test_avg = TestAttempt.objects.filter(test=attempt.test, status='submitted').aggregate(avg=Avg('score'))['avg']

    def _shape(a):
        if not a:
            return None
        return {'attempt_id': a.id, 'score': float(a.score), 'accuracy': float(a.accuracy), 'attempt_number': a.attempt_number}

    return {
        'current': _shape(attempt),
        'previous_attempt': _shape(previous),
        'best_attempt': _shape(best),
        'is_best_attempt': bool(best and best.pk == attempt.pk),
        'test_average_score': round(float(test_avg), 2) if test_avg is not None else None,
    }


def activity_calendar(user, course, month):
    """`month` is a 'YYYY-MM' string. Returns per-day activity flags plus
    upcoming scheduled exams for that month."""
    from videos_app.models import VideoProgress

    year, mon = int(month[:4]), int(month[5:7])
    start = datetime(year, mon, 1).date()
    end = datetime(year + 1, 1, 1).date() if mon == 12 else datetime(year, mon + 1, 1).date()

    days = defaultdict(lambda: {'quiz': False, 'mock': False, 'practice': False, 'video': False})

    ta_qs = TestAttempt.objects.filter(
        user=user, status='submitted', start_time__date__gte=start, start_time__date__lt=end,
    ).select_related('test')
    if course:
        ta_qs = ta_qs.filter(Q(test__courses__id=course) | Q(test__courses__isnull=True))
    for a in ta_qs.distinct():
        d = a.start_time.date().isoformat()
        days[d]['mock' if a.test.exam_type == 'mock' else 'quiz'] = True

    qa_qs = QuestionAttempt.objects.filter(user=user, answered_at__date__gte=start, answered_at__date__lt=end)
    if course:
        qa_qs = qa_qs.filter(question__courses__id=course)
    for d in qa_qs.values_list('answered_at__date', flat=True).distinct():
        days[d.isoformat()]['practice'] = True

    vp_qs = VideoProgress.objects.filter(user=user, last_watched_at__date__gte=start, last_watched_at__date__lt=end)
    if course:
        vp_qs = vp_qs.filter(video__courses__id=course)
    for d in vp_qs.values_list('last_watched_at__date', flat=True).distinct():
        days[d.isoformat()]['video'] = True

    # Was courses__isnull=True treated as "shared" — wrong for Test, where
    # blank means "not yet assigned to anyone" (see tests_app.models.Test),
    # not shared, and had no is_draft/assigned_students/assigned_batches
    # check at all, leaking draft and out-of-course exam ids/titles into
    # the calendar. visible_test_queryset is the same eligibility gate
    # _start_attempt() itself uses.
    upcoming_qs = visible_test_queryset(user, Test.objects.filter(
        scheduled_start__date__gte=start, scheduled_start__date__lt=end, scheduled_start__gte=timezone.now(),
    ))
    if course:
        upcoming_qs = upcoming_qs.filter(courses__id=course)
    upcoming_exams = [
        {'date': t.scheduled_start.date().isoformat(), 'test_id': t.id, 'title': t.title, 'exam_type': t.exam_type}
        for t in upcoming_qs.distinct()
    ]

    return {'days': dict(days), 'upcoming_exams': upcoming_exams}
