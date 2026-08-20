from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from billing.models import Coupon, Subscription, SubscriptionPlan
from core.models import DeletionAuditLog
from courses.models import Course, Enrollment

User = get_user_model()


class CourseDeleteTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='staff1', email='staff1@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(username='student1', email='student1@example.com', password='pw12345')
        self.client.force_authenticate(user=self.staff)

    def test_blocked_when_students_are_enrolled(self):
        course = Course.objects.create(name='CEE-MD Ayurveda', prefix='AYU')
        Enrollment.objects.create(user=self.student, course=course)

        resp = self.client.delete(f'/api/courses/{course.id}/')

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(Course.objects.filter(id=course.id).exists())
        entry = DeletionAuditLog.objects.get(resource_type='Course', resource_id=str(course.id))
        self.assertEqual(entry.result, 'failure')

    def test_blocked_when_students_hold_a_subscription(self):
        course = Course.objects.create(name='CEE-MD Ayurveda', prefix='AYU')
        plan = SubscriptionPlan.objects.create(course=course, product_type='qbank', name='QBank', price=100)
        Subscription.objects.create(user=self.student, plan=plan, course=course, product_type='qbank')

        resp = self.client.delete(f'/api/courses/{course.id}/')

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(Course.objects.filter(id=course.id).exists())

    def test_permanent_delete_succeeds_for_unused_course(self):
        course = Course.objects.create(name='CEE-MD Ayurveda', prefix='AYU')

        resp = self.client.delete(f'/api/courses/{course.id}/')

        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Course.objects.filter(id=course.id).exists())
        entry = DeletionAuditLog.objects.get(resource_type='Course')
        self.assertEqual(entry.result, 'success')

    def test_deleting_course_sets_coupon_course_to_null_instead_of_cascading(self):
        """Regression test for the Coupon.course CASCADE->SET_NULL fix found
        during the deletion-system audit: a coupon scoped to a course must
        survive that course's deletion, falling back to unscoped."""
        course = Course.objects.create(name='CEE-MD Ayurveda', prefix='AYU')
        coupon = Coupon.objects.create(code='AYU10', course=course)

        course.delete()

        coupon.refresh_from_db()
        self.assertIsNone(coupon.course)
        self.assertTrue(Coupon.objects.filter(id=coupon.id).exists())
