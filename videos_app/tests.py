from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from videos_app.models import Video

User = get_user_model()


class CourseGatedVideoListingScopingTests(APITestCase):
    """VideoViewSet.get_queryset() (the LIST endpoint) had no eligibility
    check for access_level='course' videos at all — only the actual
    stream/mark-progress endpoints checked billing.access.has_video_access.
    A CEE-PG student could still see (title/description/thumbnail) a
    course-gated video scoped only to CEE-UG in the general videos list,
    even though attempting to play it was already correctly blocked."""

    def setUp(self):
        from courses.models import Course, Enrollment

        self.cee_ug = Course.objects.create(name='CEE-UG Video', prefix='CEEUGVIDEO')
        self.cee_pg = Course.objects.create(name='CEE-PG Video', prefix='CEEPGVIDEO')

        self.pg_student = User.objects.create_user(username='video_pg', email='video_pg@example.com', password='pw12345')
        Enrollment.objects.create(user=self.pg_student, course=self.cee_pg)

        self.ug_video = Video.objects.create(title='UG Course Video', access_level='course')
        self.ug_video.courses.set([self.cee_ug])

        self.pg_video = Video.objects.create(title='PG Course Video', access_level='course')
        self.pg_video.courses.set([self.cee_pg])

        self.shared_course_video = Video.objects.create(title='Shared Course-tier Video', access_level='course')
        self.public_video = Video.objects.create(title='Public Video', access_level='public')
        self.registered_video = Video.objects.create(title='Registered Video', access_level='registered')

        self.client.force_authenticate(user=self.pg_student)

    def test_course_gated_video_from_other_course_excluded_from_listing(self):
        resp = self.client.get('/api/videos/')
        titles = {v['title'] for v in resp.data}
        self.assertIn('PG Course Video', titles)
        self.assertNotIn('UG Course Video', titles)

    def test_untagged_course_gated_video_still_listed(self):
        resp = self.client.get('/api/videos/')
        titles = {v['title'] for v in resp.data}
        self.assertIn('Shared Course-tier Video', titles)

    def test_non_course_gated_access_levels_unaffected(self):
        resp = self.client.get('/api/videos/')
        titles = {v['title'] for v in resp.data}
        self.assertIn('Public Video', titles)
        self.assertIn('Registered Video', titles)

    def test_anonymous_user_never_sees_course_gated_video(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get('/api/videos/')
        titles = {v['title'] for v in resp.data}
        self.assertNotIn('UG Course Video', titles)
        self.assertNotIn('PG Course Video', titles)

    def test_tampered_course_param_cannot_widen_access(self):
        resp = self.client.get(f'/api/videos/?course={self.cee_ug.id}')
        titles = {v['title'] for v in resp.data}
        self.assertNotIn('UG Course Video', titles)


class LinkedTestVisibilityTests(APITestCase):
    """VideoDetailSerializer.get_linked_tests_detail must not leak a linked
    Test's id/title/exam_type when that Test itself is a draft or scoped to
    a course the viewer isn't enrolled in — being linked from an otherwise-
    visible Video says nothing about the Test's own eligibility."""

    def setUp(self):
        from courses.models import Course, Enrollment
        from tests_app.models import Test

        self.cee_ug = Course.objects.create(name='CEE-UG LinkedTest', prefix='CEEUGLINKEDTEST')
        self.cee_pg = Course.objects.create(name='CEE-PG LinkedTest', prefix='CEEPGLINKEDTEST')

        self.pg_student = User.objects.create_user(username='linked_pg', email='linked_pg@example.com', password='pw12345')
        Enrollment.objects.create(user=self.pg_student, course=self.cee_pg)

        self.video = Video.objects.create(title='Shared Video With Linked Tests', access_level='public')

        self.pg_test = Test.objects.create(title='PG Linked Quiz', exam_type='qbank', is_draft=False)
        self.pg_test.courses.set([self.cee_pg])
        self.ug_test = Test.objects.create(title='UG Linked Quiz', exam_type='qbank', is_draft=False)
        self.ug_test.courses.set([self.cee_ug])
        self.draft_test = Test.objects.create(title='Draft Linked Quiz', exam_type='qbank')
        self.draft_test.courses.set([self.cee_pg])
        self.video.linked_tests.set([self.pg_test, self.ug_test, self.draft_test])

        self.client.force_authenticate(user=self.pg_student)

    def test_linked_tests_excludes_other_course_and_draft_tests(self):
        resp = self.client.get(f'/api/videos/{self.video.id}/')
        ids = {t['id'] for t in resp.data['linked_tests_detail']}
        self.assertEqual(ids, {self.pg_test.id})
