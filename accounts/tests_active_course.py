from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from courses.models import Course, Enrollment

User = get_user_model()


class ActiveCourseSwitchTests(APITestCase):
    """Students may switch their active (currently viewed) course to ANY
    existing course, not only ones they hold an Enrollment in — this
    is a pure navigation pointer, never an access grant. See
    ActiveCourseView's own docstring for why no content-access gate reads
    active_course: every real gate re-derives eligibility from
    Enrollment/Subscription/GrandTestAccess scoped to the resource's own
    course."""

    def setUp(self):
        self.enrolled_course = Course.objects.create(name='CEE-PG', prefix='CEE-PG')
        self.other_course = Course.objects.create(name='CEE-MBBS', prefix='CEE-MBBS')
        self.student = User.objects.create_user(
            username='switchstudent', email='switchstudent@example.com', password='pw12345',
        )
        Enrollment.objects.create(user=self.student, course=self.enrolled_course, access_type='free', is_active=True)
        self.client.force_authenticate(user=self.student)

    def test_can_switch_to_a_course_with_no_enrollment(self):
        resp = self.client.post('/api/auth/active-course/', {'course_id': self.other_course.id})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.student.refresh_from_db()
        self.assertEqual(self.student.active_course_id, self.other_course.id)

    def test_switching_never_creates_an_enrollment_row(self):
        self.assertFalse(Enrollment.objects.filter(user=self.student, course=self.other_course).exists())
        self.client.post('/api/auth/active-course/', {'course_id': self.other_course.id})
        self.assertFalse(
            Enrollment.objects.filter(user=self.student, course=self.other_course).exists(),
            'Switching active_course must never grant academic/paid access by creating an Enrollment.',
        )

    def test_switching_back_to_an_enrolled_course_still_works(self):
        self.student.active_course = self.other_course
        self.student.save(update_fields=['active_course'])
        resp = self.client.post('/api/auth/active-course/', {'course_id': self.enrolled_course.id})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.student.refresh_from_db()
        self.assertEqual(self.student.active_course_id, self.enrolled_course.id)

    def test_nonexistent_course_id_returns_404_not_500(self):
        resp = self.client.post('/api/auth/active-course/', {'course_id': 999999})
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_missing_course_id_returns_400(self):
        resp = self.client.post('/api/auth/active-course/', {})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthenticated_request_rejected(self):
        self.client.force_authenticate(user=None)
        resp = self.client.post('/api/auth/active-course/', {'course_id': self.other_course.id})
        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
