from rest_framework import serializers

from .models import Video, VideoCategory, VideoNote, VideoProgress, VideoResource


class VideoCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = VideoCategory
        fields = ['id', 'name', 'slug', 'order']
        extra_kwargs = {'slug': {'required': False}}


class VideoResourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = VideoResource
        fields = ['id', 'video', 'resource_type', 'title', 'file', 'external_url', 'order']
        extra_kwargs = {'video': {'required': False}}


class VideoNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = VideoNote
        fields = ['id', 'video', 'timestamp_seconds', 'text', 'created_at']
        read_only_fields = ['created_at']


class VideoProgressSerializer(serializers.ModelSerializer):
    class Meta:
        model = VideoProgress
        fields = [
            'last_position_seconds', 'max_position_seconds', 'is_completed', 'completed_at',
            'is_bookmarked', 'last_watched_at',
        ]


def _play_url(obj, request):
    if obj.source_type == 'upload' and obj.video_file:
        url = obj.video_file.url
        return request.build_absolute_uri(url) if request else url
    return obj.video_url


class VideoListSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source='subject.name', read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True)
    instructor_display = serializers.SerializerMethodField()
    has_access = serializers.SerializerMethodField()
    progress = serializers.SerializerMethodField()
    play_url = serializers.SerializerMethodField()

    class Meta:
        model = Video
        fields = [
            'id', 'title', 'slug', 'thumbnail', 'subject', 'subject_name', 'category', 'category_name',
            'instructor_display', 'duration_seconds', 'access_level', 'has_access', 'views_count',
            'progress', 'play_url', 'order',
        ]

    def get_instructor_display(self, obj):
        if obj.instructor_name:
            return obj.instructor_name
        if obj.created_by_id:
            return obj.created_by.first_name or obj.created_by.email
        return ''

    def get_has_access(self, obj):
        from billing.access import has_video_access
        request = self.context.get('request')
        return has_video_access(request.user if request else None, obj)

    def get_progress(self, obj):
        request = self.context.get('request')
        user = request.user if request else None
        if not user or not user.is_authenticated:
            return None
        progress = obj.progress.filter(user=user).first()
        if not progress:
            return None
        return VideoProgressSerializer(progress).data

    def get_play_url(self, obj):
        # Only expose a play URL to requesters who actually have access — locked
        # premium/course-based content shouldn't leak its raw source either way.
        if not self.get_has_access(obj):
            return None
        return _play_url(obj, self.context.get('request'))


class VideoDetailSerializer(VideoListSerializer):
    resources = serializers.SerializerMethodField()
    linked_tests_detail = serializers.SerializerMethodField()
    chapter_name = serializers.CharField(source='chapter.name', read_only=True)
    topic_name = serializers.CharField(source='topic.name', read_only=True)

    class Meta(VideoListSerializer.Meta):
        fields = VideoListSerializer.Meta.fields + [
            'description', 'chapter', 'chapter_name', 'topic', 'topic_name',
            'allow_notes_download', 'allow_slides_download', 'resources', 'linked_tests_detail',
        ]

    def get_resources(self, obj):
        if not self.get_has_access(obj):
            return []
        return VideoResourceSerializer(obj.resources.all(), many=True).data

    def get_linked_tests_detail(self, obj):
        # A linked Test can be draft or scoped to a course other than the
        # viewer's own — obj.linked_tests.all() alone leaked its id/title/
        # exam_type regardless, since being visible on this Video (already
        # course-scoped) says nothing about the linked Test's own
        # eligibility. visible_test_queryset is the same gate used for the
        # main Test listing/start endpoints.
        from tests_app.access import visible_test_queryset

        request = self.context.get('request')
        user = request.user if request else None
        visible = visible_test_queryset(user, obj.linked_tests.all())
        return [{'id': t.id, 'title': t.title, 'exam_type': t.exam_type} for t in visible]


class VideoAdminSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()
    subject_name = serializers.CharField(source='subject.name', read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True)
    resources = VideoResourceSerializer(many=True, read_only=True)

    class Meta:
        model = Video
        fields = [
            'id', 'title', 'slug', 'description', 'category', 'category_name',
            'courses', 'subject', 'subject_name', 'chapter', 'topic',
            'source_type', 'video_file', 'video_url', 'thumbnail',
            'instructor_name', 'duration_seconds',
            'access_level', 'allow_notes_download', 'allow_slides_download',
            'linked_tests', 'is_active', 'is_archived', 'order', 'views_count',
            'created_by', 'created_by_name', 'created_at', 'updated_at', 'resources',
        ]
        read_only_fields = ['views_count', 'created_by', 'created_at', 'updated_at']

    def get_created_by_name(self, obj):
        if not obj.created_by_id:
            return ''
        return obj.created_by.first_name or obj.created_by.email

    def create(self, validated_data):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            validated_data['created_by'] = request.user
        return super().create(validated_data)
