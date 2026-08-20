from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from core.models import DeletionAuditLog
from marketplace.models import CourseEnrollment, TeacherCourse

User = get_user_model()


class TeacherCourseDeleteTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='staff1', email='staff1@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.teacher = User.objects.create_user(username='teacher1', email='teacher1@example.com', password='pw12345')
        self.student = User.objects.create_user(username='student1', email='student1@example.com', password='pw12345')
        self.client.force_authenticate(user=self.staff)

    def test_blocked_when_students_are_enrolled(self):
        course = TeacherCourse.objects.create(teacher=self.teacher, title='Anatomy Crash Course')
        CourseEnrollment.objects.create(user=self.student, course=course)

        resp = self.client.delete(f'/api/teacher-courses/{course.id}/')

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(TeacherCourse.objects.filter(id=course.id).exists())
        entry = DeletionAuditLog.objects.get(resource_type='TeacherCourse', resource_id=str(course.id))
        self.assertEqual(entry.result, 'failure')

    def test_permanent_delete_succeeds_for_course_with_no_enrollments(self):
        course = TeacherCourse.objects.create(teacher=self.teacher, title='Anatomy Crash Course')

        resp = self.client.delete(f'/api/teacher-courses/{course.id}/')

        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(TeacherCourse.objects.filter(id=course.id).exists())
        entry = DeletionAuditLog.objects.get(resource_type='TeacherCourse')
        self.assertEqual(entry.result, 'success')

    def test_non_staff_cannot_delete(self):
        course = TeacherCourse.objects.create(teacher=self.teacher, title='Anatomy Crash Course')
        self.client.force_authenticate(user=self.teacher)

        resp = self.client.delete(f'/api/teacher-courses/{course.id}/')

        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
        self.assertTrue(TeacherCourse.objects.filter(id=course.id).exists())
