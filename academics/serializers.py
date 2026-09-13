from rest_framework import serializers

from courses.models import Course
from media_library.serializers import resolve_image_data

from .models import Chapter, Option, Question, QuestionAttempt, QuestionReport, ReferenceBook, Subject, Topic


class TopicSerializer(serializers.ModelSerializer):
    question_count = serializers.SerializerMethodField()
    video_count = serializers.SerializerMethodField()

    class Meta:
        model = Topic
        fields = ['id', 'chapter', 'name', 'order', 'question_count', 'video_count']
        extra_kwargs = {'chapter': {'required': False}}

    def get_question_count(self, obj):
        val = getattr(obj, 'annotated_question_count', None)
        return val if val is not None else obj.questions.count()

    def get_video_count(self, obj):
        val = getattr(obj, 'annotated_video_count', None)
        return val if val is not None else obj.videos.count()


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
        val = getattr(obj, 'annotated_mcq_count', None)
        return val if val is not None else obj.questions.count()

    def get_solved_count(self, obj):
        user = self.context.get('request').user if self.context.get('request') else None
        if not user or not user.is_authenticated:
            return 0
        precomputed = self.context.get('solved_count_by_chapter')
        if precomputed is not None:
            return precomputed.get(obj.id, 0)
        return QuestionAttempt.objects.filter(user=user, question__chapter=obj).values('question').distinct().count()

    def get_video_count(self, obj):
        val = getattr(obj, 'annotated_video_count', None)
        return val if val is not None else obj.videos.count()


class SubjectListSerializer(serializers.ModelSerializer):
    module_count = serializers.SerializerMethodField()
    solved_modules = serializers.SerializerMethodField()
    question_count = serializers.SerializerMethodField()
    video_count = serializers.SerializerMethodField()
    courses_detail = serializers.SerializerMethodField()
    courses = serializers.PrimaryKeyRelatedField(queryset=Course.objects.all(), many=True, required=False)
    has_access = serializers.SerializerMethodField()
    attempted_count = serializers.SerializerMethodField()
    percent_practiced = serializers.SerializerMethodField()

    class Meta:
        model = Subject
        fields = [
            'id', 'name', 'slug', 'prefix', 'icon', 'order', 'is_free',
            'module_count', 'solved_modules', 'question_count', 'video_count', 'courses', 'courses_detail', 'has_access',
            'attempted_count', 'percent_practiced',
        ]
        extra_kwargs = {'slug': {'required': False}, 'prefix': {'required': False}}

    def get_module_count(self, obj):
        val = getattr(obj, 'annotated_module_count', None)
        return val if val is not None else obj.chapters.count()

    def get_question_count(self, obj):
        val = getattr(obj, 'annotated_question_count', None)
        return val if val is not None else obj.questions.count()

    def get_video_count(self, obj):
        val = getattr(obj, 'annotated_video_count', None)
        return val if val is not None else obj.videos.count()

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
        precomputed = self.context.get('solved_modules_by_subject')
        if precomputed is not None:
            return precomputed.get(obj.id, 0)
        return (
            QuestionAttempt.objects.filter(user=user, question__subject=obj)
            .values('question__chapter').distinct().count()
        )

    def get_attempted_count(self, obj):
        """Question-level (not chapter-level) attempted count — what
        percent_practiced is based on, distinct from solved_modules'
        chapter-started count above."""
        user = self.context.get('request').user if self.context.get('request') else None
        if not user or not user.is_authenticated:
            return 0
        precomputed = self.context.get('attempted_count_by_subject')
        if precomputed is not None:
            return precomputed.get(obj.id, 0)
        return QuestionAttempt.objects.filter(user=user, question__subject=obj).count()

    def get_percent_practiced(self, obj):
        total = self.get_question_count(obj)
        if not total:
            return 0
        return round(self.get_attempted_count(obj) / total * 100)


class SubjectDetailSerializer(SubjectListSerializer):
    chapters = ChapterSerializer(many=True, read_only=True)

    class Meta(SubjectListSerializer.Meta):
        fields = SubjectListSerializer.Meta.fields + ['chapters']


class OptionSerializer(serializers.ModelSerializer):
    """Public, pre-submission shape — deliberately excludes pick_percentage
    (and is_correct, already excluded) so a student never receives answer
    statistics before they've actually submitted an answer. The `answer`
    action attaches percentages to its own response instead, not to this
    serializer, once the student has actually answered."""
    image_data = serializers.SerializerMethodField()

    class Meta:
        model = Option
        fields = ['id', 'text', 'image', 'image_data', 'latex', 'order']

    def get_image_data(self, obj):
        return resolve_image_data(obj.image_asset, obj.image)


class OptionAdminSerializer(serializers.ModelSerializer):
    """Used by QuestionAdminSerializer (staff editing) and
    QuestionResultSerializer (student post-submission Test Mode review) —
    both contexts where revealing is_correct/pick_percentage/explanation
    is correct, unlike the public pre-submission OptionSerializer above."""
    image_data = serializers.SerializerMethodField()

    class Meta:
        model = Option
        fields = [
            'id', 'text', 'image', 'image_asset', 'image_data', 'latex', 'order',
            'explanation', 'pick_count', 'pick_percentage', 'is_correct',
        ]

    def get_image_data(self, obj):
        return resolve_image_data(obj.image_asset, obj.image)


class QuestionSerializer(serializers.ModelSerializer):
    """Public-facing serializer: hides which option is correct."""
    options = OptionSerializer(many=True, read_only=True)
    subject_name = serializers.CharField(source='subject.name', read_only=True)
    chapter_name = serializers.CharField(source='chapter.name', read_only=True)
    topic_name = serializers.CharField(source='topic.name', read_only=True)
    image_data = serializers.SerializerMethodField()
    is_bookmarked = serializers.SerializerMethodField()
    mastery_status = serializers.SerializerMethodField()
    is_incorrect = serializers.SerializerMethodField()
    is_revision_due = serializers.SerializerMethodField()
    # QBank 2.0 Phase 3: surfaces QuestionAttempt fields the Revision
    # Center / Mistake Bank 2.0 need (wrong count, last attempted,
    # confidence, the real revision date) — all already computed and
    # stored by record_question_result(), never a new mastery/history
    # mechanism. Nullable/additive: every existing caller of this
    # serializer is unaffected (extra fields it doesn't render).
    incorrect_count = serializers.SerializerMethodField()
    attempts_count = serializers.SerializerMethodField()
    confidence = serializers.SerializerMethodField()
    last_attempted_at = serializers.SerializerMethodField()
    revision_due_at = serializers.SerializerMethodField()
    # QBank 2.0 Phase 3B: plain-language "why this question" text, set only
    # by practice_session()'s smart_revision path (see its own docstring) —
    # None for every other caller, never a second recommendation engine.
    revision_reason = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = [
            'id', 'public_id', 'text', 'image', 'image_data', 'latex', 'marks', 'negative_marks',
            'year', 'subject', 'subject_name', 'chapter', 'chapter_name', 'topic', 'topic_name',
            'options', 'is_bookmarked',
            'instructor_difficulty', 'actual_difficulty', 'question_type', 'tags',
            'mastery_status', 'is_incorrect', 'is_revision_due',
            'incorrect_count', 'attempts_count', 'confidence', 'last_attempted_at', 'revision_due_at',
            'revision_reason',
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

    def get_incorrect_count(self, obj):
        return getattr(obj, 'incorrect_count_for_user', None)

    def get_attempts_count(self, obj):
        return getattr(obj, 'attempts_count_for_user', None)

    def get_confidence(self, obj):
        return getattr(obj, 'confidence_for_user', None) or ''

    def get_last_attempted_at(self, obj):
        answered_at = getattr(obj, 'answered_at_for_user', None)
        return answered_at.isoformat() if answered_at else None

    def get_revision_due_at(self, obj):
        due = getattr(obj, 'revision_due_at_for_user', None)
        return due.isoformat() if due else None

    def get_revision_reason(self, obj):
        return getattr(obj, 'revision_reason_for_user', None)


class ReferenceBookSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReferenceBook
        fields = ['id', 'name', 'author']


class QuestionAdminSerializer(serializers.ModelSerializer):
    options = OptionAdminSerializer(many=True)
    created_by_name = serializers.SerializerMethodField()
    image_data = serializers.SerializerMethodField()
    explanation_image_data = serializers.SerializerMethodField()
    reference_book_name = serializers.CharField(source='reference_book.name', read_only=True, default='')

    class Meta:
        model = Question
        fields = [
            'id', 'public_id', 'text', 'image', 'image_asset', 'image_data', 'latex',
            'explanation', 'explanation_image', 'explanation_image_asset', 'explanation_image_data',
            'explanation_latex', 'explanation_video_url', 'references', 'key_takeaway',
            'reference_book', 'reference_book_name', 'reference_edition', 'reference_chapter',
            'reference_page', 'reference_url',
            'marks', 'negative_marks', 'remarks', 'year', 'past_exam_years',
            'subject', 'chapter', 'topic', 'courses', 'options', 'created_by_name',
            'instructor_difficulty', 'actual_difficulty', 'actual_difficulty_sample_size',
            'question_type', 'tags', 'total_attempts', 'correct_attempts',
        ]
        read_only_fields = [
            'public_id', 'actual_difficulty', 'actual_difficulty_sample_size',
            'total_attempts', 'correct_attempts',
        ]

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
    time_taken_seconds = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    confidence = serializers.ChoiceField(choices=['guess', 'unsure', 'confident'], required=False, allow_blank=True)


class QuestionResultSerializer(serializers.ModelSerializer):
    """Post-submission review shape (Test Mode result screen, and the
    shared piece of QuestionViewSet.answer()'s own response) — safe to
    reveal is_correct/pick_percentage/key_takeaway/reference here, since
    the student has already answered (or the test is already submitted).

    Phase 7: solution-revealing content (is_correct, explanation and its
    variants, key_takeaway, reference detail, per-option is_correct/
    explanation/pick stats, and the aggregate correctness stats) is now
    conditional on `context['show_solutions']` — defaults to True, so
    QuestionViewSet.answer() (the QBank immediate-explanation feature,
    unrelated to any Test's solutions_visibility policy) is completely
    unaffected without needing any change there. TestResultSerializer
    (the Test Mode caller this was actually built for) explicitly passes
    `show_solutions=can_view_solutions(...).allowed`. The student's own
    selected_option_id is NEVER stripped — seeing what you picked is
    CanReview's territory, not CanViewSolutions', and is exactly the
    "don't accidentally reveal the correct answer merely because the
    student can see their own answer" distinction this phase's spec asks
    for."""
    options = OptionAdminSerializer(many=True, read_only=True)
    subject_name = serializers.CharField(source='subject.name', read_only=True)
    selected_option_id = serializers.SerializerMethodField()
    is_correct = serializers.SerializerMethodField()
    image_data = serializers.SerializerMethodField()
    explanation_image_data = serializers.SerializerMethodField()
    reference_book_name = serializers.CharField(source='reference_book.name', read_only=True, default='')
    stats_available = serializers.SerializerMethodField()
    students_correct_percent = serializers.SerializerMethodField()
    total_responses = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = [
            'id', 'public_id', 'text', 'image', 'image_data', 'latex',
            'explanation', 'explanation_image', 'explanation_image_data', 'explanation_latex',
            'explanation_video_url', 'references', 'key_takeaway',
            'reference_book_name', 'reference_edition', 'reference_chapter', 'reference_page', 'reference_url',
            'subject_name', 'options', 'selected_option_id', 'is_correct',
            'stats_available', 'students_correct_percent', 'total_responses',
        ]

    def get_image_data(self, obj):
        return resolve_image_data(obj.image_asset, obj.image)

    def get_explanation_image_data(self, obj):
        return resolve_image_data(obj.explanation_image_asset, obj.explanation_image)

    def get_selected_option_id(self, obj):
        attempt = self.context.get('attempt_map', {}).get(obj.id)
        return attempt.selected_option_id if attempt else None

    def _min_attempts(self):
        # Passed in via context by the caller (avoids one QuestionBankConfig
        # lookup per question in a list of dozens) — falls back to loading
        # it directly for any caller that doesn't set it.
        if 'min_attempts_for_option_stats' in self.context:
            return self.context['min_attempts_for_option_stats']
        from .models import QuestionBankConfig
        return QuestionBankConfig.load().min_attempts_for_option_stats

    def get_stats_available(self, obj):
        return obj.total_attempts >= self._min_attempts()

    def get_students_correct_percent(self, obj):
        if obj.total_attempts < self._min_attempts():
            return None
        return round(obj.correct_attempts / obj.total_attempts * 100)

    def get_total_responses(self, obj):
        if obj.total_attempts < self._min_attempts():
            return None
        return obj.total_attempts

    def get_is_correct(self, obj):
        attempt = self.context.get('attempt_map', {}).get(obj.id)
        return attempt.is_correct if attempt else False

    # Fields stripped when solutions are locked — deliberately everything
    # that either IS the answer key (explanation/is_correct/key_takeaway/
    # reference detail) or could be combined to infer it (per-question and
    # per-option correctness stats). `options` is rebuilt via
    # OptionAdminSerializer above (which always includes is_correct/
    # explanation/pick stats, correctly, for its OTHER caller — staff
    # editing) — those specific keys are stripped per-option here instead
    # of touching that serializer, so staff editing is never affected.
    _SOLUTION_QUESTION_FIELDS = (
        'is_correct', 'explanation', 'explanation_image', 'explanation_image_data', 'explanation_latex',
        'explanation_video_url', 'key_takeaway', 'references',
        'reference_book_name', 'reference_edition', 'reference_chapter', 'reference_page', 'reference_url',
        'stats_available', 'students_correct_percent', 'total_responses',
    )
    _SOLUTION_OPTION_FIELDS = ('is_correct', 'explanation', 'pick_count', 'pick_percentage')

    def to_representation(self, instance):
        data = super().to_representation(instance)
        show_solutions = self.context.get('show_solutions', True)
        data['solutions_locked'] = not show_solutions
        if show_solutions:
            return data
        for field in self._SOLUTION_QUESTION_FIELDS:
            data.pop(field, None)
        for option in data.get('options') or []:
            for field in self._SOLUTION_OPTION_FIELDS:
                option.pop(field, None)
        return data


class QuestionReportSerializer(serializers.ModelSerializer):
    """Student-facing create shape (question/user set server-side in the
    view, never trusted from the client) and staff-facing review shape —
    reason/comment/status only, no student identity fields at all here;
    the admin queue surfaces `reporter_role` instead (see
    QuestionReportViewSet), never a name/email/ID."""

    class Meta:
        model = QuestionReport
        fields = ['id', 'question', 'reason', 'comment', 'status', 'created_at']
        # `question` is set by the view from the URL's pk (via get_object(),
        # which already enforces course-eligibility) — never trust a
        # client-supplied question id here, and don't require one either.
        read_only_fields = ['id', 'question', 'status', 'created_at']


class QuestionReportAdminSerializer(serializers.ModelSerializer):
    question_text = serializers.CharField(source='question.text', read_only=True)
    question_public_id = serializers.CharField(source='question.public_id', read_only=True)
    reviewed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = QuestionReport
        fields = [
            'id', 'question', 'question_text', 'question_public_id', 'reason', 'comment',
            'status', 'created_at', 'reviewed_by_name', 'reviewed_at',
        ]
        read_only_fields = ['id', 'question', 'reason', 'comment', 'created_at', 'reviewed_by_name', 'reviewed_at']

    def get_reviewed_by_name(self, obj):
        if not obj.reviewed_by_id:
            return ''
        return obj.reviewed_by.first_name or obj.reviewed_by.email

