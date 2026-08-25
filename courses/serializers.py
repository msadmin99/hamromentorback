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
        from academics.models import Question
        return Question.objects.count()


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
