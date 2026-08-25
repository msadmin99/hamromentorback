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

    def test_deleting_course_drops_it_from_coupon_courses_instead_of_cascading(self):
        """Regression test for the Coupon.course CASCADE->SET_NULL fix found
        during the deletion-system audit (course was originally a single FK;
        now a ManyToMany for multi-course coupons, same non-destructive
        intent): a coupon scoped to a course must survive that course's
        deletion, falling back to unscoped once the course drops out of its
        courses set."""
        course = Course.objects.create(name='CEE-MD Ayurveda', prefix='AYU')
        coupon = Coupon.objects.create(code='AYU10')
        coupon.courses.add(course)

        course.delete()

        coupon.refresh_from_db()
        self.assertEqual(coupon.courses.count(), 0)
        self.assertTrue(Coupon.objects.filter(id=coupon.id).exists())


class CourseQuestionCountTests(APITestCase):
    """get_question_count must be per-course, not a platform-wide total
    (the originally reported bug), and must count questions that inherit
    their course scope from Subject (the real production shape, where
    Question.courses is unpopulated) — not only questions explicitly
    tagged on Question.courses (which production never uses)."""

    def setUp(self):
        from academics.models import Question, Subject

        self.cee_ug = Course.objects.create(name='CEE-UG QCount', prefix='CEEUGQCOUNT')
        self.cee_pg = Course.objects.create(name='CEE-PG QCount', prefix='CEEPGQCOUNT')

        physics = Subject.objects.create(name='Physics QCount')
        physics.courses.set([self.cee_ug])
        Question.objects.create(subject=physics, text='Physics QCount Q1')
        Question.objects.create(subject=physics, text='Physics QCount Q2')

        pathology = Subject.objects.create(name='Pathology QCount')
        pathology.courses.set([self.cee_pg])
        Question.objects.create(subject=pathology, text='Pathology QCount Q1')

        self.staff = User.objects.create_user(
            username='qcount_staff', email='qcount_staff@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.client.force_authenticate(user=self.staff)

    def test_question_count_is_per_course_not_platform_wide(self):
        resp = self.client.get('/api/courses/')
        by_id = {c['id']: c['question_count'] for c in resp.data}
        self.assertEqual(by_id[self.cee_ug.id], 2)
        self.assertEqual(by_id[self.cee_pg.id], 1)
