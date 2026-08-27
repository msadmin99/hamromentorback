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

    def test_question_count_handles_multi_course_tags_and_shared_subjects_without_double_counting(self):
        """Correctness check for the annotate()-free grouped-aggregate
        rewrite: a question directly tagged to TWO courses must count once
        toward each (not fan out into a wrong total via the M2M join), a
        subject shared across two courses must fan its inherited questions
        out to both, and a directly-tagged question must never also be
        double-counted via the subject-inheritance path."""
        from academics.models import Question, Subject

        third_course = Course.objects.create(name='NMCLE QCount', prefix='NMCLEQCOUNT')

        # Directly tagged to BOTH cee_ug and cee_pg — must count once in each.
        shared_subject = Subject.objects.create(name='Anatomy QCount Shared')
        shared_subject.courses.set([self.cee_ug, self.cee_pg, third_course])
        multi_tagged_q = Question.objects.create(subject=shared_subject, text='Multi-tagged Q')
        multi_tagged_q.courses.set([self.cee_ug, self.cee_pg])  # explicit override, narrower than the subject

        # Blank courses, inherits from a subject scoped to two courses at once.
        Question.objects.create(subject=shared_subject, text='Inherited via shared subject')

        resp = self.client.get('/api/courses/')
        by_id = {c['id']: c['question_count'] for c in resp.data}
        # cee_ug: 2 (from setUp) + multi_tagged_q (direct) + shared inherited = 4
        self.assertEqual(by_id[self.cee_ug.id], 4)
        # cee_pg: 1 (from setUp) + multi_tagged_q (direct) + shared inherited = 3
        self.assertEqual(by_id[self.cee_pg.id], 3)
        # third_course: only the shared inherited one (multi_tagged_q's own
        # courses override excludes third_course explicitly)
        self.assertEqual(by_id[third_course.id], 1)

    def test_question_count_query_count_does_not_scale_with_number_of_courses(self):
        """The actual N+1 regression test — was one query per course
        (2.5s/21+ queries in production); must now stay constant regardless
        of how many courses exist."""
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        from academics.models import Question, Subject

        from courses.serializers import question_counts_by_course

        for i in range(15):
            course = Course.objects.create(name=f'QCount Scale {i}', prefix=f'QCS{i}')
            subject = Subject.objects.create(name=f'QCount Scale Subject {i}')
            subject.courses.set([course])
            Question.objects.create(subject=subject, text=f'QCount Scale Q{i}')

        # Isolate question_counts_by_course()'s own query cost — the actual
        # fix under test — from CourseSerializer's other fields
        # (student_count, packages) which have their own pre-existing
        # per-object query patterns, out of scope here (see the audit's
        # Issue 2, scoped specifically to get_question_count).
        with CaptureQueriesContext(connection) as ctx:
            counts = question_counts_by_course()
        self.assertGreaterEqual(len(counts), 17)  # 2 from setUp + 15 new, all with >=1 question
        self.assertEqual(len(ctx.captured_queries), 2, f'{len(ctx.captured_queries)} queries — looks like N+1 again')

        # And confirm the endpoint itself still returns correct, non-zero
        # counts for the newly created courses (correctness, not query count).
        resp = self.client.get('/api/courses/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        by_id = {c['id']: c['question_count'] for c in resp.data}
        for i in range(15):
            course = Course.objects.get(prefix=f'QCS{i}')
            self.assertEqual(by_id[course.id], 1)


class BackfillEnrollmentFromSubscriptionMigrationTests(APITestCase):
    """Direct test of the data migration fixing "student pays, sees
    nothing": every pre-existing Subscription (created before
    billing.payment_service._ensure_enrollment existed) must get a matching
    Enrollment, without duplicating or touching one that already exists."""

    def test_backfill_creates_missing_enrollment_and_generates_a_student_code(self):
        import importlib

        from django.apps import apps

        course = Course.objects.create(name='Backfill Course', prefix='BFC')
        student = User.objects.create_user(username='backfill_student', email='backfill@example.com', password='pw12345')
        plan = SubscriptionPlan.objects.create(course=course, product_type='qbank', name='Plan', price=500)
        # Simulate the pre-fix world: a Subscription with no Enrollment —
        # bypasses _extend_or_create_subscription deliberately, since that's
        # now fixed and would create the Enrollment itself.
        Subscription.objects.create(user=student, plan=plan, course=course, product_type='qbank')
        self.assertFalse(Enrollment.objects.filter(user=student, course=course).exists())

        migration_module = importlib.import_module('courses.migrations.0007_backfill_enrollment_from_subscription')
        migration_module.backfill_enrollment_from_subscription(apps, None)

        enrollment = Enrollment.objects.get(user=student, course=course)
        self.assertTrue(enrollment.is_active)
        self.assertEqual(enrollment.access_type, 'package')
        self.assertTrue(enrollment.student_code)
        self.assertTrue(enrollment.student_code.startswith('BFC'))

    def test_backfill_does_not_duplicate_an_existing_enrollment(self):
        import importlib

        from django.apps import apps

        course = Course.objects.create(name='Backfill Course 2', prefix='BFC2')
        student = User.objects.create_user(username='backfill_student2', email='backfill2@example.com', password='pw12345')
        plan = SubscriptionPlan.objects.create(course=course, product_type='qbank', name='Plan', price=500)
        Subscription.objects.create(user=student, plan=plan, course=course, product_type='qbank')
        Enrollment.objects.create(user=student, course=course, access_type='free', is_active=True)

        migration_module = importlib.import_module('courses.migrations.0007_backfill_enrollment_from_subscription')
        migration_module.backfill_enrollment_from_subscription(apps, None)

        self.assertEqual(Enrollment.objects.filter(user=student, course=course).count(), 1)
        self.assertEqual(Enrollment.objects.get(user=student, course=course).access_type, 'free')  # untouched
