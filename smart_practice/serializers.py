from rest_framework import serializers

from academics.models import QuestionAttempt
from academics.serializers import QuestionSerializer

from .models import SmartPracticeSession, SmartPracticeSessionQuestion


class SmartPracticeQuestionSerializer(serializers.ModelSerializer):
    """One session row — the nested `question` reuses the exact
    student-facing QuestionSerializer shape QuestionSolver.js already
    knows how to render, so the session page doesn't need a second
    question-rendering code path."""
    question = serializers.SerializerMethodField()

    class Meta:
        model = SmartPracticeSessionQuestion
        fields = ['id', 'order', 'origin', 'is_correct', 'answered_at', 'question']

    def get_question(self, obj):
        q = obj.question
        q.is_bookmarked_by_user = q.id in self.context.get('bookmarked_ids', set())
        return QuestionSerializer(q, context=self.context).data


class SmartPracticeSessionSerializer(serializers.ModelSerializer):
    source_test_id = serializers.IntegerField(read_only=True)
    source_test_title = serializers.CharField(source='source_test.title', read_only=True)
    questions = serializers.SerializerMethodField()

    class Meta:
        model = SmartPracticeSession
        fields = [
            'id', 'mode', 'status', 'question_count', 'selection_reason',
            'started_at', 'completed_at', 'score', 'accuracy',
            'source_test_id', 'source_test_title', 'questions',
        ]

    def get_questions(self, obj):
        session_questions = (
            obj.questions
            .select_related('question', 'question__subject', 'question__chapter', 'question__topic')
            .prefetch_related('question__options')
        )
        question_ids = [sq.question_id for sq in session_questions]
        bookmarked_ids = set(
            QuestionAttempt.objects.filter(user=obj.user, question_id__in=question_ids, is_bookmarked=True)
            .values_list('question_id', flat=True)
        )
        context = {**self.context, 'bookmarked_ids': bookmarked_ids}
        return SmartPracticeQuestionSerializer(session_questions, many=True, context=context).data


class SmartPracticeSessionListSerializer(serializers.ModelSerializer):
    """Lightweight — no nested questions, for a future session-history list."""
    source_test_id = serializers.IntegerField(read_only=True)
    source_test_title = serializers.CharField(source='source_test.title', read_only=True)

    class Meta:
        model = SmartPracticeSession
        fields = [
            'id', 'mode', 'status', 'question_count', 'selection_reason',
            'started_at', 'completed_at', 'score', 'accuracy',
            'source_test_id', 'source_test_title',
        ]
