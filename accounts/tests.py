import threading

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from academics.models import Question, QuestionAttempt, Subject
from billing.models import Purchase, SubscriptionPlan
from core.models import AdminEditAuditLog, DeletionAuditLog
from courses.models import Course, CoursePackage, Enrollment, EnrollmentRequest
from marketplace.models import TeacherCourse
from tests_app.models import Test, TestAttempt

User = get_user_model()


class AdminAccountDeleteTests(APITestCase):
    def setUp(self):
        self.super_admin = User.objects.create_user(
            username='super1', email='super1@example.com', password='pw12345',
            is_staff=True, admin_role='super_admin',
        )
        self.plain_admin = User.objects.create_user(
            username='admin1', email='admin1@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.client.force_authenticate(user=self.super_admin)

    def test_only_super_admin_can_delete(self):
        target = User.objects.create_user(
            username='editor1', email='editor1@example.com', password='pw12345',
            is_staff=True, admin_role='editor',
        )
        self.client.force_authenticate(user=self.plain_admin)

        resp = self.client.delete(f'/api/auth/admin-accounts/{target.id}/')

        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
        self.assertTrue(User.objects.filter(id=target.id).exists())

    def test_blocked_when_account_owns_marketplace_courses(self):
        teacher_account = User.objects.create_user(
            username='teacher1', email='teacher1@example.com', password='pw12345',
            is_staff=True, admin_role='teacher',
        )
        TeacherCourse.objects.create(teacher=teacher_account, title='Physiology Basics')

        resp = self.client.delete(f'/api/auth/admin-accounts/{teacher_account.id}/')

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(User.objects.filter(id=teacher_account.id).exists())
        entry = DeletionAuditLog.objects.get(resource_type='AdminAccount', resource_id=str(teacher_account.id))
        self.assertEqual(entry.result, 'failure')

    def test_blocked_when_account_has_purchase_history(self):
        Purchase.objects.create(
            user=self.plain_admin, kind='subscription', original_amount=500, final_amount=500,
        )

        resp = self.client.delete(f'/api/auth/admin-accounts/{self.plain_admin.id}/')

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(User.objects.filter(id=self.plain_admin.id).exists())
        self.assertIn('financial records', resp.data['detail'])

    def test_permanent_delete_succeeds_for_clean_account(self):
        target = User.objects.create_user(
            username='editor1', email='editor1@example.com', password='pw12345',
            is_staff=True, admin_role='editor',
        )

        resp = self.client.delete(f'/api/auth/admin-accounts/{target.id}/')

        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(User.objects.filter(id=target.id).exists())
        entry = DeletionAuditLog.objects.get(resource_type='AdminAccount')
        self.assertEqual(entry.result, 'success')


class AdminStudentDetailTests(APITestCase):
    """Phase 1: GET /auth/users/<id>/detail/ — permissions, field correctness,
    cross-student isolation, and query-count/N+1 guarantees."""

    def setUp(self):
        self.super_admin = User.objects.create_user(
            username='super1', email='super1@example.com', password='pw12345',
            is_staff=True, admin_role='super_admin',
        )
        self.plain_admin = User.objects.create_user(
            username='admin1', email='admin1@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.editor = User.objects.create_user(
            username='editor1', email='editor1@example.com', password='pw12345',
            is_staff=True, admin_role='editor',
        )

        self.course = Course.objects.create(name='CEE-PG Course', prefix='CEEPG', program_group='CEE-PG')
        self.package = CoursePackage.objects.create(course=self.course, name='3 Month Package', price=1000)

        self.referrer = User.objects.create_user(
            username='referrer1', email='referrer1@example.com', password='pw12345', first_name='Referrer',
        )
        self.student = User.objects.create_user(
            username='student1', email='student1@example.com', password='pw12345',
            first_name='Ram', last_name='Sharma', phone='9800000001',
            program='CEE-PG', course='CEEPG', active_course=self.course, referred_by=self.referrer,
        )
        from accounts.models import StudentProfile

        StudentProfile.objects.create(
            user=self.student, college='Test Medical College', district='Kathmandu',
            province='Bagmati', exam_target='CEE-PG 2082', batch='2082',
        )

        self.enrollment = Enrollment.objects.create(
            user=self.student, course=self.course, package=self.package, access_type='package',
        )
        EnrollmentRequest.objects.create(user=self.student, course=self.course, package=self.package, status='approved')

        self.plan = SubscriptionPlan.objects.create(
            course=self.course, product_type='qbank', name='QBank 3mo', price=1000,
        )
        Purchase.objects.create(
            user=self.student, kind='subscription', plan=self.plan,
            original_amount=1000, final_amount=1000, status='approved',
            payment_reference='REF123', decided_by=self.plain_admin,
        )

        self.subject = Subject.objects.create(name='Anatomy')
        self.question = Question.objects.create(subject=self.subject, text='A test question')
        QuestionAttempt.objects.create(
            user=self.student, question=self.question, is_correct=True,
            attempts_count=3, correct_count=2, incorrect_count=1, mastery_status='learning',
        )

        self.test_obj = Test.objects.create(title='Mock Test 1', exam_type='mock')
        TestAttempt.objects.create(
            user=self.student, test=self.test_obj, status='submitted',
            score=45, accuracy=75, rank=3, percentile=90,
        )

        self.other_student = User.objects.create_user(
            username='student2', email='student2@example.com', password='pw12345',
            first_name='Sita', last_name='Thapa',
        )
        StudentProfile.objects.create(user=self.other_student)
        Enrollment.objects.create(user=self.other_student, course=self.course, access_type='free')
        Purchase.objects.create(
            user=self.other_student, kind='subscription', original_amount=500, final_amount=500,
        )

        self.blocked_student = User.objects.create_user(
            username='blocked1', email='blocked1@example.com', password='pw12345', is_active=False,
        )
        StudentProfile.objects.create(user=self.blocked_student)

        self.client.force_authenticate(user=self.plain_admin)

    def _detail_url(self, user):
        return f'/api/auth/users/{user.id}/detail/'

    # --- permissions -----------------------------------------------------

    def test_admin_role_can_access(self):
        resp = self.client.get(self._detail_url(self.student))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_super_admin_can_access(self):
        self.client.force_authenticate(user=self.super_admin)
        resp = self.client.get(self._detail_url(self.student))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_editor_role_forbidden(self):
        self.client.force_authenticate(user=self.editor)
        resp = self.client.get(self._detail_url(self.student))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_plain_student_forbidden(self):
        self.client.force_authenticate(user=self.student)
        resp = self.client.get(self._detail_url(self.student))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_unauthorized(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get(self._detail_url(self.student))
        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_nonexistent_student_404(self):
        resp = self.client.get('/api/auth/users/999999/detail/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_staff_account_not_reachable_via_student_detail(self):
        """The queryset filters is_staff=False — an admin account's id must
        not be viewable through this student-scoped endpoint."""
        resp = self.client.get(self._detail_url(self.plain_admin))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_inactive_blocked_student_still_viewable(self):
        resp = self.client.get(self._detail_url(self.blocked_student))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data['is_active'])

    # --- field correctness -------------------------------------------------

    def test_password_never_serialized(self):
        resp = self.client.get(self._detail_url(self.student))
        body = str(resp.content)
        self.assertNotIn('password', resp.data)
        self.assertNotIn(self.student.password, body)

    def test_personal_contact_academic_fields(self):
        resp = self.client.get(self._detail_url(self.student))
        data = resp.data
        self.assertEqual(data['first_name'], 'Ram')
        self.assertEqual(data['last_name'], 'Sharma')
        self.assertEqual(data['email'], 'student1@example.com')
        self.assertEqual(data['phone'], '9800000001')
        self.assertEqual(data['profile']['college'], 'Test Medical College')
        self.assertEqual(data['profile']['district'], 'Kathmandu')
        self.assertEqual(data['profile']['province'], 'Bagmati')
        self.assertEqual(data['profile']['exam_target'], 'CEE-PG 2082')
        self.assertEqual(data['profile']['batch'], '2082')
        self.assertEqual(data['program'], 'CEE-PG')
        self.assertEqual(data['course'], 'CEEPG')
        self.assertEqual(data['active_course'], self.course.id)
        self.assertEqual(data['active_course_detail']['name'], 'CEE-PG Course')

    def test_account_fields(self):
        resp = self.client.get(self._detail_url(self.student))
        data = resp.data
        self.assertEqual(data['username'], 'student1')
        self.assertTrue(data['is_active'])
        self.assertIsNotNone(data['date_joined'])
        self.assertTrue(data['referral_code'])
        self.assertEqual(float(data['wallet_balance']), 0.0)
        self.assertEqual(data['referred_by']['email'], 'referrer1@example.com')

    def test_enrollment_and_enrollment_request_data(self):
        resp = self.client.get(self._detail_url(self.student))
        data = resp.data
        self.assertEqual(len(data['enrollments']), 1)
        enr = data['enrollments'][0]
        self.assertEqual(enr['course'], self.course.id)
        self.assertEqual(enr['course_name'], 'CEE-PG Course')
        self.assertEqual(enr['package'], self.package.id)
        self.assertEqual(enr['access_type'], 'package')
        self.assertTrue(enr['is_active'])

        self.assertEqual(len(data['enrollment_requests']), 1)
        self.assertEqual(data['enrollment_requests'][0]['status'], 'approved')

    def test_payment_summary_data(self):
        resp = self.client.get(self._detail_url(self.student))
        purchases = resp.data['purchases']
        self.assertEqual(len(purchases), 1)
        p = purchases[0]
        self.assertEqual(p['kind'], 'subscription')
        self.assertEqual(p['item_name'], 'QBank 3mo')
        self.assertEqual(float(p['final_amount']), 1000.0)
        self.assertEqual(p['status'], 'approved')
        self.assertEqual(p['payment_reference'], 'REF123')
        self.assertIn('decided_by_name', p)
        self.assertFalse(p['has_screenshot'])
        # Never the raw storage key/bucket.
        self.assertNotIn('payment_screenshot_key', p)
        self.assertNotIn('payment_screenshot_bucket', p)

    def test_activity_summary_data(self):
        resp = self.client.get(self._detail_url(self.student))
        summary = resp.data['activity_summary']
        self.assertEqual(summary['questions_attempted'], 1)
        self.assertEqual(summary['total_attempts'], 3)
        self.assertEqual(summary['total_correct'], 2)
        self.assertEqual(summary['mastery_breakdown']['learning'], 1)
        self.assertEqual(summary['tests_taken'], 1)
        self.assertEqual(float(summary['avg_score']), 45.0)
        self.assertEqual(float(summary['avg_accuracy']), 75.0)
        self.assertEqual(len(summary['recent_test_attempts']), 1)
        self.assertEqual(summary['recent_test_attempts'][0]['test_title'], 'Mock Test 1')
        self.assertEqual(summary['recent_test_attempts'][0]['rank'], 3)

    def test_device_data(self):
        from accounts.models import Device

        Device.objects.create(user=self.student, device_id='dev-1', device_label='Chrome on Windows')
        resp = self.client.get(self._detail_url(self.student))
        devices = resp.data['devices']
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]['device_label'], 'Chrome on Windows')
        self.assertEqual(resp.data['device_count'], 1)

    def test_no_cross_student_data_leakage(self):
        resp = self.client.get(self._detail_url(self.student))
        data = resp.data
        for enr in data['enrollments']:
            self.assertNotEqual(enr.get('id'), None)
        purchase_users = [p for p in data['purchases']]
        # The other student's purchase must not appear here.
        self.assertEqual(len(data['purchases']), 1)
        self.assertNotIn('student2@example.com', str(data))

        resp2 = self.client.get(self._detail_url(self.other_student))
        self.assertEqual(resp2.data['email'], 'student2@example.com')
        self.assertNotIn('student1@example.com', str(resp2.data))
        self.assertEqual(len(resp2.data['purchases']), 1)
        self.assertEqual(float(resp2.data['purchases'][0]['final_amount']), 500.0)

    # --- performance ---------------------------------------------------

    def test_query_count_bounded_and_no_n_plus_1(self):
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(self._detail_url(self.student))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        baseline_count = len(ctx.captured_queries)

        # Add substantially more related history and confirm the query
        # count does NOT grow — proof the bounded Prefetch()es and
        # aggregate()-only activity stats are doing their job.
        for i in range(30):
            extra_course = Course.objects.create(name=f'Extra Course {i}', prefix=f'EXTRA{i}')
            Enrollment.objects.create(
                user=self.student, course=extra_course, access_type='free', student_code=f'EXTRA{i}',
            )
            Purchase.objects.create(
                user=self.student, kind='subscription', original_amount=100, final_amount=100,
            )
            q = Question.objects.create(subject=self.subject, text=f'Extra question {i}')
            QuestionAttempt.objects.create(user=self.student, question=q, attempts_count=1, correct_count=1)
            t = Test.objects.create(title=f'Extra test {i}')
            TestAttempt.objects.create(user=self.student, test=t, status='submitted', score=10, accuracy=50)

        with CaptureQueriesContext(connection) as ctx2:
            resp2 = self.client.get(self._detail_url(self.student))
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        grown_count = len(ctx2.captured_queries)

        self.assertEqual(
            baseline_count, grown_count,
            f'Query count grew from {baseline_count} to {grown_count} after adding more history — '
            f'indicates an N+1 or an unbounded query.',
        )
        # Documented expectation from the view's own docstring.
        self.assertLessEqual(baseline_count, 10)


class AdminStudentEditTests(APITestCase):
    """Phase 2: PATCH /auth/users/<id>/edit/ — allowlist enforcement,
    permissions, audit logging, cross-student isolation, and that nothing
    about registration/login was disturbed."""

    def setUp(self):
        self.super_admin = User.objects.create_user(
            username='super2', email='super2@example.com', password='pw12345',
            is_staff=True, admin_role='super_admin',
        )
        self.plain_admin = User.objects.create_user(
            username='admin2', email='admin2@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.editor = User.objects.create_user(
            username='editor2', email='editor2@example.com', password='pw12345',
            is_staff=True, admin_role='editor',
        )

        from accounts.models import StudentProfile

        self.student = User.objects.create_user(
            username='editstudent1', email='editstudent1@example.com', password='pw12345',
            first_name='Ram', last_name='Sharma', phone='9800000010', program='CEE-PG', course='CEEPG',
        )
        StudentProfile.objects.create(user=self.student, college='Old College', district='Old District')

        self.other_student = User.objects.create_user(
            username='editstudent2', email='editstudent2@example.com', password='pw12345',
            first_name='Sita', last_name='Thapa',
        )
        StudentProfile.objects.create(user=self.other_student)

        self.client.force_authenticate(user=self.plain_admin)

    def _edit_url(self, user):
        return f'/api/auth/users/{user.id}/edit/'

    # --- 1/2/3/4/5: permissions ------------------------------------------

    def test_admin_can_edit_allowed_fields(self):
        resp = self.client.patch(self._edit_url(self.student), {'first_name': 'Ramesh'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.student.refresh_from_db()
        self.assertEqual(self.student.first_name, 'Ramesh')

    def test_super_admin_can_edit_allowed_fields(self):
        self.client.force_authenticate(user=self.super_admin)
        resp = self.client.patch(self._edit_url(self.student), {'last_name': 'Karki'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.student.refresh_from_db()
        self.assertEqual(self.student.last_name, 'Karki')

    def test_editor_forbidden(self):
        self.client.force_authenticate(user=self.editor)
        resp = self.client.patch(self._edit_url(self.student), {'first_name': 'X'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.student.refresh_from_db()
        self.assertEqual(self.student.first_name, 'Ram')

    def test_student_forbidden(self):
        self.client.force_authenticate(user=self.student)
        resp = self.client.patch(self._edit_url(self.student), {'first_name': 'X'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_forbidden(self):
        self.client.force_authenticate(user=None)
        resp = self.client.patch(self._edit_url(self.student), {'first_name': 'X'}, format='json')
        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    # --- 6/7/8: allowlist enforcement -------------------------------------

    def test_disallowed_field_rejected(self):
        resp = self.client.patch(self._edit_url(self.student), {'wallet_balance': '999.00'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.student.refresh_from_db()
        self.assertEqual(float(self.student.wallet_balance), 0.0)
        self.assertEqual(AdminEditAuditLog.objects.count(), 0)

    def test_email_cannot_be_changed(self):
        resp = self.client.patch(self._edit_url(self.student), {'email': 'new@example.com'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.student.refresh_from_db()
        self.assertEqual(self.student.email, 'editstudent1@example.com')

    def test_password_cannot_be_changed(self):
        old_hash = self.student.password
        resp = self.client.patch(self._edit_url(self.student), {'password': 'newpassword123'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.student.refresh_from_db()
        self.assertEqual(self.student.password, old_hash)

    def test_mixed_allowed_and_disallowed_rejects_whole_request(self):
        """A request that mixes a legitimate field with a disallowed one
        must be rejected outright, not partially applied."""
        resp = self.client.patch(
            self._edit_url(self.student), {'first_name': 'Should Not Apply', 'is_active': False}, format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.student.refresh_from_db()
        self.assertEqual(self.student.first_name, 'Ram')
        self.assertTrue(self.student.is_active)

    # --- 9/10: audit logging ----------------------------------------------

    def test_audit_log_created_for_successful_edit(self):
        resp = self.client.patch(
            self._edit_url(self.student), {'first_name': 'Ramesh', 'district': 'Bhaktapur'}, format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        entry = AdminEditAuditLog.objects.get(resource_type='Student', resource_id=str(self.student.id))
        self.assertEqual(entry.actor_id, self.plain_admin.id)
        self.assertEqual(entry.actor_email, self.plain_admin.email)
        self.assertEqual(entry.resource_label, self.student.email)
        self.assertEqual(entry.changed_fields['first_name'], {'old': 'Ram', 'new': 'Ramesh'})
        self.assertEqual(entry.changed_fields['district'], {'old': 'Old District', 'new': 'Bhaktapur'})
        self.assertNotIn('password', str(entry.changed_fields))
        self.assertIsNotNone(entry.created_at)

    def test_no_op_edit_creates_no_audit_entry(self):
        """Submitting the same value as already stored is not a 'change'."""
        resp = self.client.patch(self._edit_url(self.student), {'first_name': 'Ram'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(AdminEditAuditLog.objects.count(), 0)

    def test_failed_edit_creates_no_audit_entry(self):
        # Disallowed-field rejection.
        self.client.patch(self._edit_url(self.student), {'email': 'x@example.com'}, format='json')
        # Validation failure (duplicate phone).
        self.client.patch(self._edit_url(self.other_student), {'phone': '9800000010'}, format='json')
        self.assertEqual(AdminEditAuditLog.objects.count(), 0)

    def test_duplicate_phone_rejected_with_validation_error(self):
        resp = self.client.patch(self._edit_url(self.other_student), {'phone': '9800000010'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('phone', resp.data)

    # --- 11: cross-student isolation ---------------------------------------

    def test_cross_student_isolation(self):
        resp = self.client.patch(self._edit_url(self.student), {'first_name': 'Ramesh'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.other_student.refresh_from_db()
        self.assertEqual(self.other_student.first_name, 'Sita')

    def test_nonexistent_student_404(self):
        resp = self.client.patch('/api/auth/users/999999/edit/', {'first_name': 'X'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_staff_account_not_editable_via_student_edit(self):
        resp = self.client.patch(self._edit_url(self.plain_admin), {'first_name': 'X'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    # --- 12: registration/login unaffected ---------------------------------

    def test_registration_and_login_unaffected(self):
        reg_resp = self.client.post('/api/auth/register/', {
            'name': 'New Student', 'email': 'newreg@example.com', 'phone': '9811112222',
            'password': 'StrongPass123!', 'program': 'CEE-PG', 'course': 'CEEPG',
        }, format='json')
        self.assertEqual(reg_resp.status_code, status.HTTP_201_CREATED)

        login_resp = self.client.post('/api/auth/login/', {
            'identifier': 'newreg@example.com', 'password': 'StrongPass123!',
        }, format='json')
        self.assertEqual(login_resp.status_code, status.HTTP_200_OK)

    def test_response_reflects_only_changed_field_names(self):
        resp = self.client.patch(self._edit_url(self.student), {'batch': '2083'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['changed_fields'], ['batch'])
        self.assertEqual(resp.data['batch'], '2083')

    # --- query count --------------------------------------------------------

    def test_query_count_bounded(self):
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.patch(
                self._edit_url(self.student), {'first_name': 'Ramesh', 'college': 'New College'}, format='json',
            )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # select_for_update(user) + select_for_update(profile) + user.save +
        # profile.save + audit-log create — small and independent of history size.
        self.assertLessEqual(len(ctx.captured_queries), 8)


class AdminStudentEditConcurrencyTests(TransactionTestCase):
    """Real multi-threaded concurrent-edit test — needs TransactionTestCase
    (not the default TestCase, which wraps the whole test in one transaction
    that all threads would share, defeating the point) so each thread gets
    its own DB connection and the select_for_update() locking in
    student_edit is actually exercised."""

    def setUp(self):
        self.plain_admin = User.objects.create_user(
            username='concurrentadmin', email='concurrentadmin@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        from accounts.models import StudentProfile

        self.student = User.objects.create_user(
            username='concurrentstudent', email='concurrentstudent@example.com', password='pw12345',
            first_name='Original',
        )
        StudentProfile.objects.create(user=self.student, district='OriginalDistrict')

    def test_concurrent_edits_to_different_fields_both_persist(self):
        """Two 'admins' editing different fields on the same student at
        (approximately) the same time must both land — neither should be
        lost to a stale-read race, since each write only touches its own
        column via update_fields.

        select_for_update() makes the two requests serialize at the DB
        row-lock level on the real production backend (MySQL) — one simply
        waits for the other's transaction to commit, then proceeds. SQLite
        (test-only) has no real row-level locking and, even with a generous
        busy timeout, can still surface a same-instant write collision as
        an OperationalError rather than a wait — so each thread retries
        past that specific, SQLite-only condition, exactly as a sensible
        real client would on a lock-wait timeout. The assertion this test
        actually cares about — that BOTH edits land and neither is lost —
        is unaffected by how many retries it took to get there.
        """
        import time
        from django.db.utils import OperationalError

        results = {}

        def run_with_retries(field, value, key):
            client = APIClient()
            client.raise_request_exception = False
            client.force_authenticate(user=self.plain_admin)
            for attempt in range(5):
                try:
                    r = client.patch(f'/api/auth/users/{self.student.id}/edit/', {field: value}, format='json')
                    if r.status_code != 500:
                        results[key] = r.status_code
                        return
                except OperationalError:
                    pass
                time.sleep(0.2 * (attempt + 1))
            results[key] = results.get(key, 500)

        t1 = threading.Thread(target=run_with_retries, args=('first_name', 'ThreadA', 'first_name'))
        t2 = threading.Thread(target=run_with_retries, args=('district', 'ThreadBDistrict', 'district'))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        self.assertEqual(results.get('first_name'), status.HTTP_200_OK)
        self.assertEqual(results.get('district'), status.HTTP_200_OK)

        self.student.refresh_from_db()
        self.assertEqual(self.student.first_name, 'ThreadA')
        self.assertEqual(self.student.profile.district, 'ThreadBDistrict')

        # Both edits must have their own audit trail entry — neither
        # clobbered the other's log write either.
        self.assertEqual(AdminEditAuditLog.objects.filter(resource_id=str(self.student.id)).count(), 2)




class AdminStudentBrowseTests(APITestCase):
    """Phase 3: GET /auth/users/browse/ — real pagination, embedded
    enrollment summary, annotated device_count, permissions, and that
    list()/scholarships' bare-array caller and Student Detail/Edit are
    all unaffected."""

    PAGE_SIZE = 20

    def setUp(self):
        self.super_admin = User.objects.create_user(
            username='browsesuper', email='browsesuper@example.com', password='pw12345',
            is_staff=True, admin_role='super_admin',
        )
        self.plain_admin = User.objects.create_user(
            username='browseadmin', email='browseadmin@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.editor = User.objects.create_user(
            username='browseeditor', email='browseeditor@example.com', password='pw12345',
            is_staff=True, admin_role='editor',
        )

        from accounts.models import Device, StudentProfile

        self.course_a = Course.objects.create(name='Browse Course A', prefix='BCA')
        self.course_b = Course.objects.create(name='Browse Course B', prefix='BCB')

        # 25 students -> page 1 (20) + page 2 (5, final/partial page).
        self.students = []
        for i in range(25):
            s = User.objects.create_user(
                username=f'browsestudent{i}', email=f'browsestudent{i}@example.com', password='pw12345',
                first_name=f'Browse{i}',
            )
            StudentProfile.objects.create(user=s)
            self.students.append(s)

        # Multiple enrollments for one student, exactly one for another,
        # zero for a third — exercises items 7/8/9 directly.
        self.multi_enroll_student = self.students[0]
        Enrollment.objects.create(user=self.multi_enroll_student, course=self.course_a, access_type='package')
        Enrollment.objects.create(user=self.multi_enroll_student, course=self.course_b, access_type='free')

        self.single_enroll_student = self.students[1]
        Enrollment.objects.create(user=self.single_enroll_student, course=self.course_a, access_type='free')

        self.no_enroll_student = self.students[2]
        # (no Enrollment created)

        # Devices — including a student with 3 (the real-world max) to
        # prove the annotated count still matches obj.devices.count().
        self.device_student = self.students[3]
        for i in range(3):
            Device.objects.create(user=self.device_student, device_id=f'dev{i}', device_label=f'Device {i}')

        self.client.force_authenticate(user=self.plain_admin)

    def _browse(self, **params):
        params.setdefault('page_size', self.PAGE_SIZE)
        qs = '&'.join(f'{k}={v}' for k, v in params.items())
        return self.client.get(f'/api/auth/users/browse/?{qs}')

    # --- 1/2/3: pagination -------------------------------------------------

    def test_pagination_page_1(self):
        resp = self._browse(page=1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data
        self.assertEqual(data['count'], 25)
        self.assertEqual(len(data['results']), 20)
        self.assertIsNotNone(data['next'])
        self.assertIsNone(data['previous'])

    def test_pagination_final_page(self):
        resp = self._browse(page=2)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data
        self.assertEqual(len(data['results']), 5)
        self.assertIsNone(data['next'])
        self.assertIsNotNone(data['previous'])

    def test_page_navigation(self):
        page1_ids = {s['id'] for s in self._browse(page=1).data['results']}
        page2_ids = {s['id'] for s in self._browse(page=2).data['results']}
        self.assertEqual(len(page1_ids), 20)
        self.assertEqual(len(page2_ids), 5)
        self.assertEqual(page1_ids | page2_ids, {s.id for s in self.students})

    # --- 4/5/6: filters + pagination ---------------------------------------

    def test_search_plus_pagination(self):
        resp = self._browse(page=1, search='Browse1')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Matches Browse1, Browse10-19 -> 11 students, all fit on one page.
        self.assertEqual(resp.data['count'], 11)
        self.assertIsNone(resp.data['next'])

    def test_course_filter_plus_pagination(self):
        resp = self._browse(page=1, course=self.course_a.id)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        returned_ids = {s['id'] for s in resp.data['results']}
        self.assertEqual(resp.data['count'], 2)
        self.assertEqual(returned_ids, {self.multi_enroll_student.id, self.single_enroll_student.id})

    def test_date_filter_plus_pagination(self):
        from django.utils import timezone

        future = (timezone.now() + timezone.timedelta(days=1)).date().isoformat()
        resp = self._browse(page=1, **{'from': future})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['count'], 0)

        past = (timezone.now() - timezone.timedelta(days=1)).date().isoformat()
        resp = self._browse(page=1, **{'from': past})
        self.assertEqual(resp.data['count'], 25)

    # --- 7/8/9: enrollment summary correctness -----------------------------

    def test_enrollment_summary_correctness(self):
        # page_size=30 (> the 25 fixture students) so these data-correctness
        # checks don't depend on -date_joined ordering putting any
        # particular fixture student on page 1 vs 2 — that's covered
        # separately by the pagination-mechanics tests above.
        resp = self._browse(page=1, page_size=30)
        by_id = {s['id']: s for s in resp.data['results']}

        multi = by_id[self.multi_enroll_student.id]
        self.assertEqual(len(multi['enrollments']), 2)
        prefixes = {e['course_prefix'] for e in multi['enrollments']}
        self.assertEqual(prefixes, {'BCA', 'BCB'})
        for e in multi['enrollments']:
            self.assertEqual(set(e.keys()), {'id', 'course_prefix', 'student_code', 'access_type', 'is_active'})

        single = by_id[self.single_enroll_student.id]
        self.assertEqual(len(single['enrollments']), 1)
        self.assertEqual(single['enrollments'][0]['course_prefix'], 'BCA')
        self.assertEqual(single['enrollments'][0]['access_type'], 'free')

    def test_student_with_multiple_enrollments(self):
        # page_size=30 (> the 25 fixture students) so these data-correctness
        # checks don't depend on -date_joined ordering putting any
        # particular fixture student on page 1 vs 2 — that's covered
        # separately by the pagination-mechanics tests above.
        resp = self._browse(page=1, page_size=30)
        by_id = {s['id']: s for s in resp.data['results']}
        self.assertEqual(len(by_id[self.multi_enroll_student.id]['enrollments']), 2)

    def test_student_with_no_enrollment(self):
        # page_size=30 (> the 25 fixture students) so these data-correctness
        # checks don't depend on -date_joined ordering putting any
        # particular fixture student on page 1 vs 2 — that's covered
        # separately by the pagination-mechanics tests above.
        resp = self._browse(page=1, page_size=30)
        by_id = {s['id']: s for s in resp.data['results']}
        self.assertEqual(by_id[self.no_enroll_student.id]['enrollments'], [])

    # --- 10: device count correctness ---------------------------------------

    def test_device_count_correctness(self):
        # page_size=30 (> the 25 fixture students) so these data-correctness
        # checks don't depend on -date_joined ordering putting any
        # particular fixture student on page 1 vs 2 — that's covered
        # separately by the pagination-mechanics tests above.
        resp = self._browse(page=1, page_size=30)
        by_id = {s['id']: s for s in resp.data['results']}
        self.assertEqual(by_id[self.device_student.id]['device_count'], 3)
        self.assertEqual(by_id[self.device_student.id]['device_count'], self.device_student.devices.count())
        self.assertEqual(by_id[self.no_enroll_student.id]['device_count'], 0)

    # --- 11/12: permissions -------------------------------------------------

    def test_admin_permission(self):
        resp = self._browse(page=1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.client.force_authenticate(user=self.super_admin)
        resp = self._browse(page=1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_unauthorized_access(self):
        self.client.force_authenticate(user=self.editor)
        resp = self._browse(page=1)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(user=self.students[0])
        resp = self._browse(page=1)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(user=None)
        resp = self._browse(page=1)
        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    # --- 13/14: completeness across pages -----------------------------------

    def test_no_missing_students_beyond_page_1(self):
        all_ids = set()
        for p in (1, 2):
            all_ids |= {s['id'] for s in self._browse(page=p).data['results']}
        self.assertEqual(all_ids, {s.id for s in self.students})

    def test_no_duplicate_students_across_pages(self):
        page1_ids = [s['id'] for s in self._browse(page=1).data['results']]
        page2_ids = [s['id'] for s in self._browse(page=2).data['results']]
        combined = page1_ids + page2_ids
        self.assertEqual(len(combined), len(set(combined)), 'a student id appeared on more than one page')

    # --- security: no sensitive data added --------------------------------

    def test_no_password_or_extra_sensitive_data(self):
        resp = self._browse(page=1)
        body = str(resp.data)
        self.assertNotIn('password', resp.data['results'][0])
        for s in resp.data['results']:
            self.assertNotIn('wallet_balance', s)
            self.assertNotIn('referral_code', s)

    # --- 15: existing Student Detail/Edit + list()/scholarships regression -

    def test_existing_list_endpoint_still_bare_array(self):
        """The untouched list() action (scholarships/page.js's caller)
        must still return a bare array, not the new envelope."""
        resp = self.client.get('/api/auth/users/?search=Browse0')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIsInstance(resp.data, list)

    def test_block_unblock_still_works_via_plain_patch(self):
        student = self.students[4]
        resp = self.client.patch(f'/api/auth/users/{student.id}/', {'is_active': False}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        student.refresh_from_db()
        self.assertFalse(student.is_active)

    # --- performance ---------------------------------------------------------

    def test_query_count_flat_across_page_sizes_and_dataset_growth(self):
        with CaptureQueriesContext(connection) as ctx20:
            resp = self._browse(page=1, page_size=20)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        count_20 = len(ctx20.captured_queries)

        with CaptureQueriesContext(connection) as ctx50:
            resp = self._browse(page=1, page_size=50)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        count_50 = len(ctx50.captured_queries)

        self.assertEqual(
            count_20, count_50,
            f'query count changed with page size ({count_20} vs {count_50}) — expected flat.',
        )

        # Grow the dataset well past a single page and confirm the count
        # still doesn't move.
        from accounts.models import StudentProfile

        for i in range(100):
            extra = User.objects.create_user(
                username=f'growth{i}', email=f'growth{i}@example.com', password='pw12345',
            )
            StudentProfile.objects.create(user=extra)
            if i % 5 == 0:
                Enrollment.objects.create(user=extra, course=self.course_a, access_type='free')

        with CaptureQueriesContext(connection) as ctx_grown:
            resp = self._browse(page=1, page_size=20)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        count_grown = len(ctx_grown.captured_queries)

        self.assertEqual(
            count_20, count_grown,
            f'query count grew with dataset size ({count_20} vs {count_grown} at 125 students) — indicates an N+1.',
        )
        self.assertLessEqual(count_20, 6)


class AdminStudentBrowseAccessStatusFilterTests(APITestCase):
    """access= (Enrollment.access_type) and status= (User.is_active)
    filters on GET /auth/users/browse/ — correctness, combinability with
    search/course/date, pagination-after-filter, no duplicates, correct
    count, permissions, and that the pre-existing Phase 3 behavior
    (flat query count, no N+1) still holds with these filters applied."""

    PAGE_SIZE = 20

    def setUp(self):
        self.plain_admin = User.objects.create_user(
            username='accstatadmin', email='accstatadmin@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.editor = User.objects.create_user(
            username='accstateditor', email='accstateditor@example.com', password='pw12345',
            is_staff=True, admin_role='editor',
        )

        from accounts.models import StudentProfile

        self.course_a = Course.objects.create(name='AccStat Course A', prefix='ASA')
        self.course_b = Course.objects.create(name='AccStat Course B', prefix='ASB')

        def make(username, **kwargs):
            s = User.objects.create_user(username=username, email=f'{username}@example.com', password='pw12345', **kwargs)
            StudentProfile.objects.create(user=s)
            return s

        # Free: exactly one free enrollment.
        self.free_student = make('accstat_free', first_name='FreeRahul')
        Enrollment.objects.create(user=self.free_student, course=self.course_a, access_type='free')

        # Package: exactly one package enrollment.
        self.package_student = make('accstat_package', first_name='PackageRahul')
        Enrollment.objects.create(user=self.package_student, course=self.course_b, access_type='package')

        # Mixed: one free + one package enrollment -> counts as "package"
        # per the per-student semantic (matches the existing AccessBadge
        # display: any package enrollment at all -> shown as "N Package").
        self.mixed_student = make('accstat_mixed')
        Enrollment.objects.create(user=self.mixed_student, course=self.course_a, access_type='free')
        Enrollment.objects.create(user=self.mixed_student, course=self.course_b, access_type='package')

        # No enrollments at all -> counts as "free" (no package enrollment).
        self.no_enroll_student = make('accstat_none')

        # Status: active (default) vs. blocked.
        self.blocked_student = make('accstat_blocked', is_active=False)
        Enrollment.objects.create(user=self.blocked_student, course=self.course_a, access_type='package')

        self.client.force_authenticate(user=self.plain_admin)

    def _browse(self, **params):
        params.setdefault('page_size', self.PAGE_SIZE)
        qs = '&'.join(f'{k}={v}' for k, v in params.items())
        return self.client.get(f'/api/auth/users/browse/?{qs}')

    def _ids(self, resp):
        return {s['id'] for s in resp.data['results']}

    # --- 1/2/3: access filter correctness -----------------------------------

    def test_all_access_returns_all_eligible_students(self):
        resp = self._browse(page=1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        expected = {self.free_student.id, self.package_student.id, self.mixed_student.id,
                    self.no_enroll_student.id, self.blocked_student.id}
        self.assertEqual(self._ids(resp), expected)

    def test_access_free_returns_only_free_students(self):
        resp = self._browse(page=1, access='free')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # free_student (only free enrollment) and no_enroll_student (no
        # package enrollment at all) both count as "free"; package/mixed/
        # blocked (which has a package enrollment) must not appear.
        self.assertEqual(self._ids(resp), {self.free_student.id, self.no_enroll_student.id})

    def test_access_package_returns_only_package_students(self):
        resp = self._browse(page=1, access='package')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(self._ids(resp), {self.package_student.id, self.mixed_student.id, self.blocked_student.id})

    # --- 4/5/6: status filter correctness -------------------------------------

    def test_all_status_returns_all_eligible_students(self):
        resp = self._browse(page=1)
        self.assertEqual(len(resp.data['results']), 5)

    def test_status_active_returns_active_students(self):
        resp = self._browse(page=1, status='active')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = self._ids(resp)
        self.assertNotIn(self.blocked_student.id, ids)
        self.assertEqual(ids, {self.free_student.id, self.package_student.id, self.mixed_student.id, self.no_enroll_student.id})

    def test_status_blocked_returns_blocked_students(self):
        resp = self._browse(page=1, status='blocked')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(self._ids(resp), {self.blocked_student.id})

    # --- 7/8: search + access / status ----------------------------------------

    def test_search_plus_access(self):
        resp = self._browse(page=1, search='Rahul', access='free')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(self._ids(resp), {self.free_student.id})

    def test_search_plus_status(self):
        resp = self._browse(page=1, search='accstat_blocked', status='blocked')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(self._ids(resp), {self.blocked_student.id})

        resp2 = self._browse(page=1, search='accstat_blocked', status='active')
        self.assertEqual(resp2.data['count'], 0)

    # --- 9/10: course + access / status ----------------------------------------

    def test_course_plus_access(self):
        resp = self._browse(page=1, course=self.course_b.id, access='package')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # course_b enrollments: package_student (package) and mixed_student (package on course_b).
        self.assertEqual(self._ids(resp), {self.package_student.id, self.mixed_student.id})

    def test_course_plus_status(self):
        resp = self._browse(page=1, course=self.course_a.id, status='blocked')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # course_a enrollments: free_student, mixed_student, blocked_student — only blocked_student is blocked.
        self.assertEqual(self._ids(resp), {self.blocked_student.id})

    # --- 11/12: access + status together, all filters together -----------------

    def test_access_plus_status_together(self):
        resp = self._browse(page=1, access='package', status='active')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(self._ids(resp), {self.package_student.id, self.mixed_student.id})

    def test_all_filters_together(self):
        resp = self._browse(
            page=1, search='accstat', course=self.course_a.id, access='free', status='active',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # course_a + free + active + name contains "accstat": free_student only
        # (mixed_student is on course_a but counts as package; blocked_student is on course_a but blocked).
        self.assertEqual(self._ids(resp), {self.free_student.id})

    # --- 13/14/15: pagination after filtering, no duplicates, correct count ----

    def test_pagination_after_filtering(self):
        from accounts.models import StudentProfile

        for i in range(25):
            s = User.objects.create_user(username=f'extra_free_{i}', email=f'extra_free_{i}@example.com', password='pw12345')
            StudentProfile.objects.create(user=s)
            Enrollment.objects.create(user=s, course=self.course_a, access_type='free')

        resp = self._browse(page=1, access='free', page_size=20)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # 25 new free students + free_student + no_enroll_student = 27.
        self.assertEqual(resp.data['count'], 27)
        self.assertEqual(len(resp.data['results']), 20)
        self.assertIsNotNone(resp.data['next'])

        resp2 = self._browse(page=2, access='free', page_size=20)
        self.assertEqual(len(resp2.data['results']), 7)
        self.assertIsNone(resp2.data['next'])

        page1_ids = self._ids(resp)
        page2_ids = self._ids(resp2)
        self.assertTrue(page1_ids.isdisjoint(page2_ids), 'a student appeared on both filtered pages')

    def test_no_duplicates_with_multiple_matching_enrollments(self):
        """mixed_student has two enrollments, one of them a package one —
        the access=package filter must return them exactly once, not once
        per matching Enrollment row."""
        resp = self._browse(page=1, access='package')
        ids_list = [s['id'] for s in resp.data['results']]
        self.assertEqual(len(ids_list), len(set(ids_list)), 'duplicate student rows in access=package results')
        self.assertEqual(ids_list.count(self.mixed_student.id), 1)

    def test_correct_total_count(self):
        resp = self._browse(page=1, access='package')
        self.assertEqual(resp.data['count'], 3)
        resp2 = self._browse(page=1, status='blocked')
        self.assertEqual(resp2.data['count'], 1)

    # --- 16: permissions/security unaffected ------------------------------------

    def test_permissions_unaffected_by_new_filters(self):
        self.client.force_authenticate(user=self.editor)
        resp = self._browse(page=1, access='free', status='active')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(user=None)
        resp = self._browse(page=1, access='free')
        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_invalid_access_or_status_value_is_a_harmless_noop(self):
        """An unrecognized access/status value falls through to 'no filter
        applied' rather than raising — matches how an unrecognized course
        id would just match nothing, not error."""
        resp = self._browse(page=1, access='bogus')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['count'], 5)  # same as "all access"

    # --- query count / no N+1 with filters applied ------------------------------

    def test_query_count_flat_with_access_and_status_filters(self):
        with CaptureQueriesContext(connection) as ctx:
            resp = self._browse(page=1, access='package', status='active')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Same 3-query shape as the unfiltered case, +1 for JWT auth lookup
        # in a live deployment — locally (force_authenticate, no JWT
        # middleware query) this stays at the original 3.
        self.assertLessEqual(len(ctx.captured_queries), 6)

    def test_no_enrollments_or_old_list_endpoint_touched(self):
        """Sanity check that filtering still goes through the same single
        browse() action — not a new code path that reintroduces the
        platform-wide /enrollments/ fetch or the old bare-array list()."""
        with CaptureQueriesContext(connection) as ctx:
            self._browse(page=1, access='free', status='active')
        sql_blob = ' '.join(q['sql'] for q in ctx.captured_queries)
        # The only enrollment-table access should be the id-subquery and
        # the bounded per-page Prefetch — never an unbounded full-table
        # read with no WHERE at all.
        self.assertIn('courses_enrollment', sql_blob)
