from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from academics.models import Option, QuestionBankConfig

from . import services
from .access import SourceScopeError, resolve_source_scope
from .models import SmartPracticeConfig, SmartPracticeSession
from .serializers import SmartPracticeSessionSerializer
from .services import bookmarked_candidates, due_review_candidates, new_question_candidates
from .source_performance import source_missed_questions, source_topic_mastery

MODE_LABELS = dict(SmartPracticeSession.MODE_CHOICES)

_ERROR_STATUS = {
    'grand_test_excluded': status.HTTP_403_FORBIDDEN,
    'not_found': status.HTTP_404_NOT_FOUND,
    'not_authorized': status.HTTP_403_FORBIDDEN,
    'subscription_required': status.HTTP_402_PAYMENT_REQUIRED,
    'no_submitted_attempt': status.HTTP_400_BAD_REQUEST,
    'feature_disabled': status.HTTP_403_FORBIDDEN,
    'session_not_in_progress': status.HTTP_400_BAD_REQUEST,
}


def _error_response(exc):
    return Response(
        {'detail': str(exc), 'code': exc.code},
        status=_ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
    )


class EligibilityView(APIView):
    """Cheap precheck the result page can call on every load without
    committing to generating a full session."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        source_test_id = request.query_params.get('source_test_id')
        if not source_test_id:
            return Response({'detail': 'source_test_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        config = SmartPracticeConfig.load()
        if not config.enabled:
            return Response({'eligible': False, 'reason': 'feature_disabled'})

        try:
            ctx = resolve_source_scope(request.user, source_test_id)
        except SourceScopeError as exc:
            if exc.code == 'grand_test_excluded':
                return Response({'eligible': False, 'reason': exc.code})
            return _error_response(exc)

        missed_count = source_missed_questions(ctx).count()
        topics = source_topic_mastery(ctx, config.weak_topic_accuracy_max_pct)
        weak_topic_count = sum(1 for t in topics if t['is_weak'])
        due_count = len(due_review_candidates(ctx, request.user))
        new_count = len(new_question_candidates(ctx, request.user))
        bookmarked_count = len(bookmarked_candidates(ctx, request.user))

        # Eligible if ANY practice path has something real to offer — the
        # old "mistakes only" gate hid the whole grid (including Due for
        # Review / New Questions / Bookmarked) whenever a student did well,
        # which is exactly when those other paths are still useful.
        has_enough_mistakes = missed_count >= config.min_mistakes_to_recommend
        eligible = has_enough_mistakes or weak_topic_count or due_count or new_count or bookmarked_count
        return Response({
            'eligible': bool(eligible),
            'reason': None if eligible else 'nothing_to_practice',
            'mistake_count': missed_count,
            'weak_topic_count': weak_topic_count,
            'due_count': due_count,
            'new_count': new_count,
            'bookmarked_count': bookmarked_count,
            'source': {'type': ctx.exam_type, 'id': ctx.test.id, 'title': ctx.test.title},
        })


class RecommendationsView(APIView):
    """Mode previews (counts + a template reason) so the frontend can show
    'Retry 5 mistakes' / 'Practice 3 weak topics' before a session is
    actually created."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        source_test_id = request.query_params.get('source_test_id')
        if not source_test_id:
            return Response({'detail': 'source_test_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            ctx = resolve_source_scope(request.user, source_test_id)
        except SourceScopeError as exc:
            return _error_response(exc)

        config = SmartPracticeConfig.load()
        missed_count = source_missed_questions(ctx).count()
        topics = source_topic_mastery(ctx, config.weak_topic_accuracy_max_pct)
        weak_topics = [t for t in topics if t['is_weak']]
        due_count = len(due_review_candidates(ctx, request.user))
        new_count = len(new_question_candidates(ctx, request.user))
        bookmarked_count = len(bookmarked_candidates(ctx, request.user))
        mixed_count = min(missed_count + len(weak_topics) + due_count + new_count, config.max_questions_per_session)

        # Every tile's count is real, queried data — never a fabricated
        # placeholder. A tile is only included when there's genuinely
        # something to practice, so "Practice Paths" never shows a dead
        # button that returns zero questions.
        modes = []
        if missed_count:
            modes.append({
                'mode': 'retry_mistakes', 'label': MODE_LABELS['retry_mistakes'], 'icon': '🔄',
                'question_count': min(missed_count, config.max_questions_per_session),
                'message': f"{missed_count} question{'s' if missed_count != 1 else ''} you missed in {ctx.test.title}.",
            })
        if weak_topics:
            modes.append({
                'mode': 'source_weak_areas', 'label': MODE_LABELS['source_weak_areas'], 'icon': '🎯',
                'question_count': config.default_questions_per_session,
                'message': f"Target {len(weak_topics)} weak topic{'s' if len(weak_topics) != 1 else ''} from {ctx.test.title}: "
                           + ', '.join(t['topic_name'] for t in weak_topics[:3] if t['topic_name']),
            })
            modes.append({
                'mode': 'concept_reinforcement', 'label': MODE_LABELS['concept_reinforcement'], 'icon': '🧠',
                'question_count': config.default_questions_per_session,
                'message': f'Reinforce the concepts behind your mistakes in {ctx.test.title}.',
            })
        if due_count:
            modes.append({
                'mode': 'due_review', 'label': MODE_LABELS['due_review'], 'icon': '📅',
                'question_count': min(due_count, config.max_questions_per_session),
                'message': f"{due_count} question{'s' if due_count != 1 else ''} from {ctx.test.title} due for review.",
            })
        if new_count:
            modes.append({
                'mode': 'new_questions', 'label': MODE_LABELS['new_questions'], 'icon': '🆕',
                'question_count': min(new_count, config.max_questions_per_session),
                'message': f"{new_count} new question{'s' if new_count != 1 else ''} from {ctx.test.title} you haven't tried yet.",
            })
        if bookmarked_count:
            modes.append({
                'mode': 'bookmarked', 'label': MODE_LABELS['bookmarked'], 'icon': '⭐',
                'question_count': min(bookmarked_count, config.max_questions_per_session),
                'message': f"{bookmarked_count} bookmarked question{'s' if bookmarked_count != 1 else ''} from {ctx.test.title}.",
            })
        if mixed_count:
            modes.append({
                'mode': 'ai_mixed', 'label': MODE_LABELS['ai_mixed'], 'icon': '🎲',
                'question_count': mixed_count,
                'message': f'A balanced mix of mistakes, weak areas, review, and new questions from {ctx.test.title}.',
            })

        return Response({
            'source': {'type': ctx.exam_type, 'id': ctx.test.id, 'title': ctx.test.title},
            'modes': modes,
        })


class SessionCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        source_test_id = request.data.get('source_test_id')
        mode = request.data.get('mode')
        question_count = request.data.get('question_count')
        if not source_test_id or not mode:
            return Response({'detail': 'source_test_id and mode are required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            session = services.create_session(request.user, source_test_id, mode, question_count)
        except SourceScopeError as exc:
            return _error_response(exc)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            SmartPracticeSessionSerializer(session, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )


class SessionDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, session_id):
        session = get_object_or_404(SmartPracticeSession, pk=session_id, user=request.user)
        return Response(SmartPracticeSessionSerializer(session, context={'request': request}).data)


class SessionAnswerView(APIView):
    """Response shape mirrors QuestionViewSet.answer() exactly so
    QuestionSolver.js (already wired to that shape) can render a Smart
    Practice session without a second quiz-result rendering path."""
    permission_classes = [IsAuthenticated]

    def post(self, request, session_id):
        session = get_object_or_404(SmartPracticeSession, pk=session_id, user=request.user)
        # QuestionSolver.js's existing POST body only ever carries
        # option_id/time_taken_seconds (question_id is normally the URL
        # path itself, e.g. /questions/{id}/answer/) — this endpoint has
        # no per-question path segment, so the question id travels as a
        # query param via the answerUrl(questionId) callback instead.
        question_id = request.query_params.get('question_id') or request.data.get('question_id')
        option_id = request.data.get('option_id')
        time_taken_seconds = request.data.get('time_taken_seconds')
        if not question_id:
            return Response({'detail': 'question_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            sq = services.record_session_answer(request.user, session, question_id, option_id, time_taken_seconds)
        except SourceScopeError as exc:
            return _error_response(exc)

        question = sq.question
        question.refresh_from_db(fields=['total_attempts', 'correct_attempts'])
        correct_option = question.options.filter(is_correct=True).first()
        config = QuestionBankConfig.load()
        stats_available = bool(option_id) and question.total_attempts >= config.min_attempts_for_option_stats

        options_payload = None
        if option_id:
            options_payload = [
                {
                    'id': opt.id,
                    'pick_percentage': opt.pick_percentage if stats_available else None,
                    'explanation': opt.explanation,
                }
                for opt in Option.objects.filter(question_id=question.id).order_by('order')
            ]

        return Response({
            'is_correct': sq.is_correct,
            'correct_option_id': correct_option.id if correct_option else None,
            'explanation': question.explanation,
            'explanation_image': request.build_absolute_uri(question.explanation_image.url) if question.explanation_image else None,
            'explanation_latex': question.explanation_latex,
            'explanation_video_url': question.explanation_video_url,
            'references': question.references,
            'key_takeaway': question.key_takeaway,
            'reference_book_name': question.reference_book.name if question.reference_book_id else '',
            'reference_edition': question.reference_edition,
            'reference_chapter': question.reference_chapter,
            'reference_page': question.reference_page,
            'reference_url': question.reference_url,
            'options': options_payload,
            'stats_available': stats_available,
            'students_correct_percent': (
                round(question.correct_attempts / question.total_attempts * 100) if stats_available else None
            ),
            'total_responses': question.total_attempts if stats_available else None,
        })


class SessionCompleteView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, session_id):
        session = get_object_or_404(SmartPracticeSession, pk=session_id, user=request.user)
        try:
            session = services.complete_session(request.user, session)
        except SourceScopeError as exc:
            return _error_response(exc)
        return Response(SmartPracticeSessionSerializer(session, context={'request': request}).data)
