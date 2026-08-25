from rest_framework import serializers

from .models import (
    Batch,
    Course,
    CoursePackage,
    Enrollment,
    EnrollmentRequest,
)


class BatchSerializer(serializers.ModelSerializer):
    course_name = serializers.CharField(source='course.name', read_only=True)
    student_count = serializers.SerializerMethodField()

    class Meta:
        model = Batch
        fields = ['id', 'course', 'course_name', 'name', 'is_active', 'student_count']

    def get_student_count(self, obj):
        return obj.enrollments.filter(is_active=True).count()


class CoursePackageSerializer(serializers.ModelSerializer):
    class Meta:
        model = CoursePackage
        fields = ['id', 'course', 'name', 'price', 'duration_days', 'is_active']
        extra_kwargs = {'course': {'required': False}}


class CourseSerializer(serializers.ModelSerializer):
    packages = CoursePackageSerializer(many=True, read_only=True)
    student_count = serializers.SerializerMethodField()
    question_count = serializers.SerializerMethodField()

    class Meta:
        model = Course
        fields = [
            'id', 'name', 'prefix', 'program_group', 'order', 'is_active',
            'icon', 'color', 'description',
            'packages', 'student_count', 'question_count',
        ]

    def get_student_count(self, obj):
        return obj.enrollments.filter(is_active=True).count()

    def get_question_count(self, obj):
        # Was Question.objects.count() — the platform-wide total regardless
        # of `obj`. obj.questions.count() (Question.courses' related_name)
        # alone undercounts to 0 for every course, since Question.courses is
        # unpopulated on every real question — only Subject.courses is
        # actually maintained by admins. Mirrors
        # academics.views._question_course_scoped's inheritance rule: a
        # question with its own `courses` tag counts there; a question with
        # none counts toward every course its Subject is scoped to.
        from django.db.models import Q

        from academics.models import Question

        return Question.objects.filter(
            Q(courses=obj) | Q(courses__isnull=True, subject__courses=obj)
        ).distinct().count()


class EnrollmentSerializer(serializers.ModelSerializer):
    student_name = serializers.SerializerMethodField()
    email = serializers.CharField(source='user.email', read_only=True)
    course_name = serializers.CharField(source='course.name', read_only=True)
    course_prefix = serializers.CharField(source='course.prefix', read_only=True)
    course_program_group = serializers.CharField(source='course.program_group', read_only=True)
    batch_name = serializers.CharField(source='batch.name', read_only=True, default=None)
    active_devices = serializers.SerializerMethodField()

    class Meta:
        model = Enrollment
        fields = [
            'id', 'user', 'student_name', 'email', 'course', 'course_name', 'course_prefix', 'course_program_group',
            'package', 'batch', 'batch_name', 'access_type', 'student_code', 'is_active', 'enrolled_at', 'expires_at',
            'active_devices',
        ]

    def get_student_name(self, obj):
        return f'{obj.user.first_name} {obj.user.last_name}'.strip() or obj.user.email

    def get_active_devices(self, obj):
        return obj.user.devices.count()


class EnrollmentRequestSerializer(serializers.ModelSerializer):
    student_name = serializers.SerializerMethodField()
    email = serializers.CharField(source='user.email', read_only=True)
    course_name = serializers.CharField(source='course.name', read_only=True)
    package_name = serializers.CharField(source='package.name', read_only=True)

    class Meta:
        model = EnrollmentRequest
        fields = [
            'id', 'user', 'student_name', 'email', 'course', 'course_name',
            'package', 'package_name', 'student_code', 'status', 'submitted_at', 'decided_at',
        ]
        read_only_fields = ['submitted_at', 'decided_at']

    def get_student_name(self, obj):
        return f'{obj.user.first_name} {obj.user.last_name}'.strip() or obj.user.email
