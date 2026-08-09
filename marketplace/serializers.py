from rest_framework import serializers

from .models import CourseCategory, CourseEnrollment, CourseLesson, CourseSection, TeacherCourse, TeacherProfile


class TeacherProfileSummarySerializer(serializers.ModelSerializer):
    """Lightweight — embedded on the User serializer so the Frontend's
    RequireTeacher gate can read status straight off /auth/me/ with no
    extra request."""

    class Meta:
        model = TeacherProfile
        fields = ['status', 'bio', 'qualification', 'specialization', 'photo', 'rejection_reason']


class TeacherProfileSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source='user.email', read_only=True)
    name = serializers.SerializerMethodField()
    course_count = serializers.ReadOnlyField()
    student_count = serializers.ReadOnlyField()

    class Meta:
        model = TeacherProfile
        fields = [
            'id', 'email', 'name', 'status', 'bio', 'qualification', 'specialization', 'photo',
            'submitted_at', 'decided_at', 'rejection_reason', 'course_count', 'student_count',
        ]
        read_only_fields = ['status', 'submitted_at', 'decided_at', 'rejection_reason']

    def get_name(self, obj):
        return obj.user.first_name or obj.user.email

    def create(self, validated_data):
        request = self.context.get('request')
        return TeacherProfile.objects.create(user=request.user, **validated_data)


class CourseCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = CourseCategory
        fields = ['id', 'name', 'slug', 'order']
        extra_kwargs = {'slug': {'required': False}}


class CourseLessonSerializer(serializers.ModelSerializer):
    video_title = serializers.CharField(source='video.title', read_only=True)
    test_title = serializers.CharField(source='test.title', read_only=True)

    class Meta:
        model = CourseLesson
        fields = [
            'id', 'section', 'lesson_type', 'title', 'description', 'order',
            'video', 'video_title', 'test', 'test_title', 'pdf_file', 'notes_content',
        ]


class CourseSectionSerializer(serializers.ModelSerializer):
    lessons = CourseLessonSerializer(many=True, read_only=True)

    class Meta:
        model = CourseSection
        fields = ['id', 'course', 'title', 'order', 'lessons']


class TeacherCourseSerializer(serializers.ModelSerializer):
    sections = CourseSectionSerializer(many=True, read_only=True)
    teacher_name = serializers.SerializerMethodField()
    section_count = serializers.ReadOnlyField()
    lesson_count = serializers.ReadOnlyField()

    class Meta:
        model = TeacherCourse
        fields = [
            'id', 'teacher', 'teacher_name', 'category', 'title', 'slug', 'short_description', 'description',
            'thumbnail', 'level', 'price', 'is_free', 'access_duration_type', 'access_duration_days',
            'status', 'rejection_reason', 'submitted_at', 'reviewed_at', 'created_at', 'updated_at',
            'sections', 'section_count', 'lesson_count',
        ]
        read_only_fields = ['teacher', 'status', 'rejection_reason', 'submitted_at', 'reviewed_at']

    def get_teacher_name(self, obj):
        return obj.teacher.first_name or obj.teacher.email

    def create(self, validated_data):
        request = self.context.get('request')
        return TeacherCourse.objects.create(teacher=request.user, **validated_data)


class CourseEnrollmentSerializer(serializers.ModelSerializer):
    student_name = serializers.SerializerMethodField()
    course_title = serializers.CharField(source='course.title', read_only=True)

    class Meta:
        model = CourseEnrollment
        fields = ['id', 'user', 'student_name', 'course', 'course_title', 'source', 'enrolled_at', 'expires_at', 'is_active']
        read_only_fields = ['enrolled_at']

    def get_student_name(self, obj):
        return obj.user.first_name or obj.user.email
