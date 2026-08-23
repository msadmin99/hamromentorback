from rest_framework import serializers

from courses.models import Course
from media_library.serializers import resolve_image_data

from .models import Chapter, Option, Question, QuestionAttempt, Subject, Topic


class TopicSerializer(serializers.ModelSerializer):
    question_count = serializers.SerializerMethodField()
    video_count = serializers.SerializerMethodField()

    class Meta:
        model = Topic
        fields = ['id', 'chapter', 'name', 'order', 'question_count', 'video_count']
        extra_kwargs = {'chapter': {'required': False}}

    def get_question_count(self, obj):
        return obj.questions.count()

    def get_video_count(self, obj):
        return obj.videos.count()


class ChapterSerializer(serializers.ModelSerializer):
    topics = TopicSerializer(many=True, read_only=True)
    mcq_count = serializers.SerializerMethodField()
    solved_count = serializers.SerializerMethodField()
    video_count = serializers.SerializerMethodField()

    class Meta:
        model = Chapter
        fields = ['id', 'subject', 'name', 'slug', 'order', 'topics', 'mcq_count', 'solved_count', 'video_count']
        # 'default' (not just required=False) is needed here — Chapter has a `unique_together`
        # on (subject, slug), and DRF's auto-added UniqueTogetherValidator otherwise forces slug
        # to be required in the request body regardless of the model's blank=True / the save()
        # method's auto-slugify, ignoring a plain required=False override.
        extra_kwargs = {'slug': {'required': False, 'default': ''}}

    def get_mcq_count(self, obj):
        return obj.questions.count()

    def get_solved_count(self, obj):
        user = self.context.get('request').user if self.context.get('request') else None
        if not user or not user.is_authenticated:
            return 0
        return QuestionAttempt.objects.filter(user=user, question__chapter=obj).values('question').distinct().count()

    def get_video_count(self, obj):
        return obj.videos.count()


class SubjectListSerializer(serializers.ModelSerializer):
    module_count = serializers.SerializerMethodField()
    solved_modules = serializers.SerializerMethodField()
    question_count = serializers.SerializerMethodField()
    video_count = serializers.SerializerMethodField()
    courses_detail = serializers.SerializerMethodField()
    courses = serializers.PrimaryKeyRelatedField(queryset=Course.objects.all(), many=True, required=False)
    has_access = serializers.SerializerMethodField()

    class Meta:
        model = Subject
        fields = [
            'id', 'name', 'slug', 'prefix', 'icon', 'order', 'is_free',
            'module_count', 'solved_modules', 'question_count', 'video_count', 'courses', 'courses_detail', 'has_access',
        ]
        extra_kwargs = {'slug': {'required': False}, 'prefix': {'required': False}}

    def get_module_count(self, obj):
        return obj.chapters.count()

    def get_question_count(self, obj):
        return obj.questions.count()

    def get_video_count(self, obj):
        return obj.videos.count()

    def get_has_access(self, obj):
        from billing.access import has_qbank_access
        request = self.context.get('request')
        return has_qbank_access(request.user if request else None, obj)

    def get_courses_detail(self, obj):
        return [
            {'id': c.id, 'name': c.name, 'prefix': c.prefix, 'program_group': c.program_group}
            for c in obj.courses.all()
        ]

    def get_solved_modules(self, obj):
        user = self.context.get('request').user if self.context.get('request') else None
        if not user or not user.is_authenticated:
            return 0
        return (
            QuestionAttempt.objects.filter(user=user, question__subject=obj)
            .values('question__chapter').distinct().count()
        )


class SubjectDetailSerializer(SubjectListSerializer):
    chapters = ChapterSerializer(many=True, read_only=True)

    class Meta(SubjectListSerializer.Meta):
        fields = SubjectListSerializer.Meta.fields + ['chapters']


class OptionSerializer(serializers.ModelSerializer):
    image_data = serializers.SerializerMethodField()

    class Meta:
        model = Option
        fields = ['id', 'text', 'image', 'image_data', 'latex', 'order', 'pick_percentage']

    def get_image_data(self, obj):
        return resolve_image_data(obj.image_asset, obj.image)


class OptionAdminSerializer(serializers.ModelSerializer):
    image_data = serializers.SerializerMethodField()

    class Meta:
        model = Option
        fields = ['id', 'text', 'image', 'image_asset', 'image_data', 'latex', 'order', 'pick_percentage', 'is_correct']

    def get_image_data(self, obj):
        return resolve_image_data(obj.image_asset, obj.image)


class QuestionSerializer(serializers.ModelSerializer):
    """Public-facing serializer: hides which option is correct."""
    options = OptionSerializer(many=True, read_only=True)
    subject_name = serializers.CharField(source='subject.name', read_only=True)
    chapter_name = serializers.CharField(source='chapter.name', read_only=True)
    image_data = serializers.SerializerMethodField()
    is_bookmarked = serializers.SerializerMethodField()
    mastery_status = serializers.SerializerMethodField()
    is_incorrect = serializers.SerializerMethodField()
    is_revision_due = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = [
            'id', 'public_id', 'text', 'image', 'image_data', 'latex', 'marks', 'negative_marks',
            'year', 'subject', 'subject_name', 'chapter', 'chapter_name', 'topic', 'options', 'is_bookmarked',
            'instructor_difficulty', 'actual_difficulty', 'question_type', 'tags',
            'mastery_status', 'is_incorrect', 'is_revision_due',
        ]

    def get_image_data(self, obj):
        return resolve_image_data(obj.image_asset, obj.image)

    def get_is_bookmarked(self, obj):
        # Relies on QuestionViewSet.get_queryset()'s is_bookmarked_by_user
        # annotation (a single EXISTS subquery) rather than a per-object
        # query here — this field can appear on a whole chapter's worth of
        # questions in one response.
        return bool(getattr(obj, 'is_bookmarked_by_user', False))

    def get_mastery_status(self, obj):
        # Independently set by callers that build their own queryset (e.g.
        # QuestionViewSet.mistakes/practice_session) as a plain attribute —
        # falls back to the get_queryset() subquery annotation, and to 'new'
        # (never attempted) when neither is present.
        explicit = getattr(obj, 'mastery_status_for_user', None)
        return explicit or 'new'

    def get_is_incorrect(self, obj):
        return getattr(obj, 'last_result_for_user', None) is False

    def get_is_revision_due(self, obj):
        due = getattr(obj, 'revision_due_at_for_user', None)
        if not due:
            return False
        from django.utils import timezone
        return due <= timezone.now()


class QuestionAdminSerializer(serializers.ModelSerializer):
    options = OptionAdminSerializer(many=True)
    created_by_name = serializers.SerializerMethodField()
    image_data = serializers.SerializerMethodField()
    explanation_image_data = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = [
            'id', 'public_id', 'text', 'image', 'image_asset', 'image_data', 'latex',
            'explanation', 'explanation_image', 'explanation_image_asset', 'explanation_image_data',
            'explanation_latex', 'explanation_video_url', 'references',
            'marks', 'negative_marks', 'remarks', 'year', 'past_exam_years',
            'subject', 'chapter', 'topic', 'courses', 'options', 'created_by_name',
            'instructor_difficulty', 'actual_difficulty', 'actual_difficulty_sample_size',
            'question_type', 'tags',
        ]
        read_only_fields = ['public_id', 'actual_difficulty', 'actual_difficulty_sample_size']

    def get_created_by_name(self, obj):
        if not obj.created_by_id:
            return ''
        return obj.created_by.first_name or obj.created_by.email

    def get_image_data(self, obj):
        return resolve_image_data(obj.image_asset, obj.image)

    def get_explanation_image_data(self, obj):
        return resolve_image_data(obj.explanation_image_asset, obj.explanation_image)

    def create(self, validated_data):
        options_data = validated_data.pop('options')
        courses = validated_data.pop('courses', [])
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            validated_data['created_by'] = request.user
        question = Question.objects.create(**validated_data)
        if courses:
            question.courses.set(courses)
        for opt in options_data:
            Option.objects.create(question=question, **opt)
        return question

    def update(self, instance, validated_data):
        options_data = validated_data.pop('options', None)
        courses = validated_data.pop('courses', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if courses is not None:
            instance.courses.set(courses)
        if options_data is not None:
            instance.options.all().delete()
            for opt in options_data:
                Option.objects.create(question=instance, **opt)
        return instance


class AnswerSubmitSerializer(serializers.Serializer):
    option_id = serializers.IntegerField(required=False, allow_null=True)
    bookmark = serializers.BooleanField(required=False, default=False)


class QuestionResultSerializer(serializers.ModelSerializer):
    options = OptionAdminSerializer(many=True, read_only=True)
    subject_name = serializers.CharField(source='subject.name', read_only=True)
    selected_option_id = serializers.SerializerMethodField()
    is_correct = serializers.SerializerMethodField()
    image_data = serializers.SerializerMethodField()
    explanation_image_data = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = [
            'id', 'public_id', 'text', 'image', 'image_data', 'latex',
            'explanation', 'explanation_image', 'explanation_image_data', 'explanation_latex',
            'explanation_video_url', 'references',
            'subject_name', 'options', 'selected_option_id', 'is_correct',
        ]

    def get_image_data(self, obj):
        return resolve_image_data(obj.image_asset, obj.image)

    def get_explanation_image_data(self, obj):
        return resolve_image_data(obj.explanation_image_asset, obj.explanation_image)

    def get_selected_option_id(self, obj):
        attempt = self.context.get('attempt_map', {}).get(obj.id)
        return attempt.selected_option_id if attempt else None

    def get_is_correct(self, obj):
        attempt = self.context.get('attempt_map', {}).get(obj.id)
        return attempt.is_correct if attempt else False

