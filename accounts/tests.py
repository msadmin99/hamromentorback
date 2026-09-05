from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status
from rest_framework.test import APITestCase

from billing.models import Purchase
from core.models import DeletionAuditLog
from courses.models import Course, Enrollment
from marketplace.models import TeacherCourse

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
