from django.utils import timezone
from rest_framework import serializers

from academics.models import Question
from academics.serializers import OptionSerializer, QuestionResultSerializer

from .models import Answer, ExamSession, ExamTemplate, Test, TestAttempt, TestQuestion


def _staff_name(user):
    if not user:
        return ''
    return user.first_name or user.email


class TestListSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source='subject.name', read_only=True)
    question_count = serializers.IntegerField(read_only=True)
    total_marks = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    best_score = serializers.SerializerMethodField()
    courses_detail = serializers.SerializerMethodField()
    card_status = serializers.SerializerMethodField()
    attempts_used = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    exam_template_id = serializers.IntegerField(source='exam_template.id', read_only=True, default=None)
    exam_code = serializers.CharField(source='exam_template.exam_code', read_only=True, default=None)

    class Meta:
        model = Test
        fields = [
            'id', 'title', 'description', 'difficulty', 'exam_type', 'subject', 'subject_name', 'duration_minutes',
            'question_count', 'total_marks', 'is_pro', 'is_new', 'price', 'max_attempts',
            'academic_year', 'university', 'scheduled_start', 'scheduled_end', 'status', 'best_score',
            'courses_detail', 'card_status', 'attempts_used', 'created_by_name', 'created_at',
            'is_draft', 'exam_template_id', 'exam_code', 'version_number',
        ]

    def get_created_by_name(self, obj):
        if not obj.created_by_id:
            return ''
        return obj.created_by.first_name or obj.created_by.email

    def get_total_marks(self, obj):
        return float(obj.total_marks)

    def get_status(self, obj):
        now = timezone.now()
        if obj.scheduled_start and obj.scheduled_start > now:
            return 'upcoming'
        if obj.scheduled_end and obj.scheduled_end < now:
            return 'ended'
        return 'live'

    def get_best_score(self, obj):
        user = self.context.get('request').user if self.context.get('request') else None
        if not user or not user.is_authenticated:
            return None
        best = obj.attempts.filter(user=user, status='submitted').order_by('-score').first()
        return float(best.score) if best else None

    def get_courses_detail(self, obj):
        return [{'id': c.id, 'name': c.name} for c in obj.courses.all()]

    def get_attempts_used(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return 0
        return obj.attempts.filter(user=request.user).count()

    def get_card_status(self, obj):
        """Available / Upcoming / Completed / Missed — for the dashboard exam card."""
        now = timezone.now()
        has_attempted = self.get_best_score(obj) is not None
        if has_attempted:
            return 'completed'
        if obj.scheduled_start and obj.scheduled_start > now:
            return 'upcoming'
        if obj.scheduled_end and obj.scheduled_end < now:
            return 'missed'
        return 'available'


class QuestionForAttemptSerializer(serializers.ModelSerializer):
    from academics.serializers import OptionSerializer as _OptionSerializer
    options = OptionSerializer(many=True, read_only=True)
    subject_name = serializers.CharField(source='subject.name', read_only=True)

    class Meta:
        from academics.models import Question
        model = Question
        fields = ['id', 'public_id', 'text', 'image', 'latex', 'marks', 'negative_marks', 'subject_name', 'options']


class TestDetailSerializer(TestListSerializer):
    has_access = serializers.SerializerMethodField()
    requires_password = serializers.SerializerMethodField()

    class Meta(TestListSerializer.Meta):
        fields = TestListSerializer.Meta.fields + [
            'shuffle_questions', 'shuffle_options', 'negative_marking', 'max_attempts',
            'free_preview_questions', 'has_access', 'requires_password',
        ]

    def get_has_access(self, obj):
        from billing.access import get_grand_test_access, has_daily_test_access, has_mock_test_access

        if not obj.is_pro:
            return True
        request = self.context.get('request')
        user = request.user if request else None
        if obj.exam_type == 'grand':
            return bool(get_grand_test_access(user, obj))
        if obj.exam_type in ('mock', 'qbank'):
            return has_mock_test_access(user, obj)
        if obj.exam_type == 'daily':
            return has_daily_test_access(user, obj)
        return True

    def get_requires_password(self, obj):
        return bool(obj.access_password) or (obj.exam_type == 'grand' and obj.is_pro)


class TestAdminSerializer(serializers.ModelSerializer):
    question_ids = serializers.PrimaryKeyRelatedField(
        queryset=Question.objects.all(), many=True, write_only=True, required=False, source='selected_questions',
    )
    questions = serializers.SerializerMethodField()
    question_count = serializers.IntegerField(read_only=True)
    total_marks = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    exam_code = serializers.CharField(source='exam_template.exam_code', read_only=True, default=None)

    class Meta:
        model = Test
        fields = [
            'id', 'title', 'description', 'difficulty', 'exam_type', 'subject', 'courses', 'assigned_students',
            'assigned_batches', 'needs_course_review', 'duration_minutes', 'questions_per_page',
            'negative_marking', 'shuffle_questions', 'shuffle_options', 'max_attempts', 'solutions_visibility',
            'is_pro', 'is_new', 'price', 'access_password', 'free_preview_questions', 'academic_year', 'university',
            'scheduled_start', 'scheduled_end', 'is_draft', 'question_ids', 'questions', 'question_count', 'total_marks',
            'created_by_name', 'exam_template', 'exam_code', 'version_number',
        ]
        read_only_fields = ['exam_template', 'version_number']

    def get_total_marks(self, obj):
        return float(obj.total_marks)

    def get_questions(self, obj):
        from academics.serializers import QuestionSerializer
        return QuestionSerializer(obj.questions.all().order_by('testquestion__order'), many=True).data

    def get_created_by_name(self, obj):
        if not obj.created_by_id:
            return ''
        return obj.created_by.first_name or obj.created_by.email

    def create(self, validated_data):
        questions = validated_data.pop('selected_questions', [])
        courses = validated_data.pop('courses', [])
        assigned_students = validated_data.pop('assigned_students', [])
        assigned_batches = validated_data.pop('assigned_batches', [])
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            validated_data['created_by'] = request.user
        test = Test.objects.create(**validated_data)
        if courses:
            test.courses.set(courses)
        if assigned_students:
            test.assigned_students.set(assigned_students)
        if assigned_batches:
            test.assigned_batches.set(assigned_batches)
        for i, q in enumerate(questions):
            TestQuestion.objects.create(test=test, question=q, order=i)
        return test

    def update(self, instance, validated_data):
        questions = validated_data.pop('selected_questions', None)
        courses = validated_data.pop('courses', None)
        assigned_students = validated_data.pop('assigned_students', None)
        assigned_batches = validated_data.pop('assigned_batches', None)
        # needs_course_review is a one-time legacy-migration flag ("visible
        # to everyone until an admin assigns real courses" — see the model's
        # help_text) that the exam-management UI never surfaces, so it was
        # never getting cleared once an admin actually assigned courses
        # here — the real access-control bug this guards against. Auto-clear
        # it the moment this save gives the test a real assignment, unless
        # the caller explicitly set needs_course_review itself this request.
        if 'needs_course_review' not in validated_data and (courses or assigned_students or assigned_batches):
            instance.needs_course_review = False
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if courses is not None:
            instance.courses.set(courses)
        if assigned_students is not None:
            instance.assigned_students.set(assigned_students)
        if assigned_batches is not None:
            instance.assigned_batches.set(assigned_batches)
        if questions is not None:
            TestQuestion.objects.filter(test=instance).delete()
            for i, q in enumerate(questions):
                TestQuestion.objects.create(test=instance, question=q, order=i)
        return instance


class ExamSessionSerializer(serializers.ModelSerializer):
    exam_template_title = serializers.CharField(source='exam_template.title', read_only=True)
    exam_code = serializers.CharField(source='exam_template.exam_code', read_only=True)
    question_count = serializers.IntegerField(source='exam_version.question_count', read_only=True)
    total_marks = serializers.SerializerMethodField()
    duration_minutes = serializers.IntegerField(source='exam_version.duration_minutes', read_only=True)
    negative_marking = serializers.BooleanField(source='exam_version.negative_marking', read_only=True)
    participant_count = serializers.IntegerField(read_only=True)
    created_by_name = serializers.SerializerMethodField()
    password = serializers.CharField(required=False, allow_blank=True, write_only=True)

    class Meta:
        model = ExamSession
        fields = [
            'id', 'exam_template', 'exam_template_title', 'exam_code', 'exam_version', 'session_name',
            'start_datetime', 'end_datetime', 'registration_deadline', 'timezone', 'access_type',
            'access_courses', 'password', 'max_attempts', 'status', 'recurrence', 'question_count',
            'total_marks', 'duration_minutes', 'negative_marking', 'participant_count',
            'created_by_name', 'created_at', 'updated_at',
        ]
        read_only_fields = ['exam_template', 'exam_version', 'status']

    def get_total_marks(self, obj):
        return float(obj.exam_version.total_marks)

    def get_created_by_name(self, obj):
        return _staff_name(obj.created_by)


class ExamTemplateSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()
    version_count = serializers.SerializerMethodField()
    latest_session = serializers.SerializerMethodField()
    total_participants = serializers.SerializerMethodField()

    class Meta:
        model = ExamTemplate
        fields = [
            'id', 'exam_code', 'title', 'exam_type', 'created_by_name', 'created_at',
            'version_count', 'latest_session', 'total_participants',
        ]

    def get_created_by_name(self, obj):
        return _staff_name(obj.created_by)

    def get_version_count(self, obj):
        return obj.versions.count()

    def get_latest_session(self, obj):
        latest = obj.sessions.order_by('-start_datetime').first()
        if not latest:
            return None
        return ExamSessionSerializer(latest, context=self.context).data

    def get_total_participants(self, obj):
        return TestAttempt.objects.filter(session__exam_template=obj).values('user').distinct().count()


class RescheduleSerializer(serializers.Serializer):
    """Input for TestViewSet.reschedule — validated shape only; the actual
    lazy-adoption/locking/versioning logic lives in exam_versioning.py."""
    session_name = serializers.CharField(required=False, allow_blank=True)
    start_datetime = serializers.DateTimeField()
    end_datetime = serializers.DateTimeField()
    registration_deadline = serializers.DateTimeField(required=False, allow_null=True)
    timezone = serializers.CharField(required=False, default='Asia/Kathmandu')
    access_type = serializers.ChoiceField(choices=ExamSession.ACCESS_CHOICES, required=False, default='all')
    access_course_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    password = serializers.CharField(required=False, allow_blank=True)
    max_attempts = serializers.IntegerField(required=False, default=1, min_value=1)
    new_version = serializers.BooleanField(required=False, default=False)
    new_version_question_ids = serializers.ListField(child=serializers.IntegerField(), required=False)


class StartTestSerializer(serializers.Serializer):
    access_password = serializers.CharField(required=False, allow_blank=True)


class TestAttemptSerializer(serializers.ModelSerializer):
    questions = serializers.SerializerMethodField()
    test_title = serializers.CharField(source='test.title', read_only=True)
    duration_minutes = serializers.IntegerField(source='test.duration_minutes', read_only=True)
    questions_per_page = serializers.IntegerField(source='test.questions_per_page', read_only=True)
    preview_only = serializers.SerializerMethodField()
    session_name = serializers.CharField(source='session.session_name', read_only=True, default=None)

    class Meta:
        model = TestAttempt
        fields = [
            'id', 'test', 'test_title', 'duration_minutes', 'questions_per_page', 'attempt_number',
            'start_time', 'status', 'questions', 'preview_only', 'session', 'session_name',
        ]

    def get_preview_only(self, obj):
        from billing.access import is_preview_only
        request = self.context.get('request')
        return is_preview_only(request.user if request else None, obj.test)

    def get_questions(self, obj):
        qs = list(obj.test.questions.all().prefetch_related('options'))
        if obj.test.shuffle_questions:
            import random
            random.shuffle(qs)
        if self.get_preview_only(obj):
            qs = qs[:obj.test.free_preview_questions]
        return QuestionForAttemptSerializer(qs, many=True, context=self.context).data


class SubmitAnswerSerializer(serializers.Serializer):
    question_id = serializers.IntegerField()
    option_id = serializers.IntegerField(required=False, allow_null=True)
    mark_for_review = serializers.BooleanField(required=False, default=False)
    time_taken_seconds = serializers.IntegerField(required=False, allow_null=True, min_value=0)


class TestAttemptSummarySerializer(serializers.ModelSerializer):
    """Lightweight attempt shape for list views (e.g. 'My Attempts' history) —
    skips the nested question/options/explanation payload TestResultSerializer builds."""
    test_title = serializers.CharField(source='test.title', read_only=True)
    exam_type = serializers.CharField(source='test.exam_type', read_only=True)
    total_marks = serializers.SerializerMethodField()
    time_taken_seconds = serializers.SerializerMethodField()
    session_name = serializers.CharField(source='session.session_name', read_only=True, default=None)

    class Meta:
        model = TestAttempt
        fields = [
            'id', 'test', 'test_title', 'exam_type', 'score', 'total_marks', 'rank',
            'percentile', 'accuracy', 'status', 'start_time', 'end_time', 'time_taken_seconds',
            'session', 'session_name',
        ]

    def get_total_marks(self, obj):
        return float(obj.test.total_marks)

    def get_time_taken_seconds(self, obj):
        if obj.end_time and obj.start_time:
            return int((obj.end_time - obj.start_time).total_seconds())
        return None


class SessionAttemptSerializer(TestAttemptSummarySerializer):
    """TestAttemptSummarySerializer + who took it — used only by
    ExamSessionViewSet.attempts (admin Participants/Results view)."""
    user_name = serializers.SerializerMethodField()
    user_email = serializers.CharField(source='user.email', read_only=True)

    class Meta(TestAttemptSummarySerializer.Meta):
        fields = TestAttemptSummarySerializer.Meta.fields + ['user_name', 'user_email']

    def get_user_name(self, obj):
        return f'{obj.user.first_name} {obj.user.last_name}'.strip() or obj.user.email


class TestResultSerializer(serializers.ModelSerializer):
    questions = serializers.SerializerMethodField()
    test_title = serializers.CharField(source='test.title', read_only=True)
    total_marks = serializers.SerializerMethodField()
    session_name = serializers.CharField(source='session.session_name', read_only=True, default=None)

    class Meta:
        model = TestAttempt
        fields = [
            'id', 'test', 'test_title', 'score', 'total_marks', 'rank',
            'percentile', 'accuracy', 'status', 'start_time', 'end_time', 'questions',
            'session', 'session_name',
        ]

    def get_total_marks(self, obj):
        return float(obj.test.total_marks)

    def get_questions(self, obj):
        filter_type = self.context.get('filter', 'all')
        answers = {a.question_id: a for a in obj.answers.select_related('question', 'selected_option')}
        # Every question in the test is reviewable, not just the ones the student answered —
        # a skipped question is still "wrong", and should still show up with its solution.
        questions = list(obj.test.questions.all())
        attempt_map = {
            q.id: answers.get(q.id) or Answer(question_id=q.id, selected_option=None, is_correct=False)
            for q in questions
        }
        if filter_type == 'wrong':
            questions = [q for q in questions if not attempt_map[q.id].is_correct]
        elif filter_type == 'correct':
            questions = [q for q in questions if attempt_map[q.id].is_correct]

        from academics.models import QuestionBankConfig
        context = {
            'attempt_map': attempt_map,
            'min_attempts_for_option_stats': QuestionBankConfig.load().min_attempts_for_option_stats,
        }
        return QuestionResultSerializer(questions, many=True, context=context).data
