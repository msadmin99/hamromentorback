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
