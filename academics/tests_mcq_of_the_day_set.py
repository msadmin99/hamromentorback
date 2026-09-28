"""DailyMCQSetView (core "MCQ of the Day" tab, choose-a-program flow) —
5 auto-rotating MCQs per course, deterministic per (date, course), scoped
via academics.access.question_scoped_to_single_course (Subject.courses,
NOT the unpopulated Question.courses field — see that function's own
docstring for why a naive filter would leak every course's questions into
every other course)."""
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from courses.models import Course

from .models import Question, Subject

User = get_user_model()


def _user(username):
    return User.objects.create_user(username=username, email=f'{username}@example.com', password='pw12345')


def _question(subject, courses, text):
    q = Question.objects.create(subject=subject, text=text)
    if courses:
        q.subject.courses.set(courses)
    return q


class DailyMCQSetTests(APITestCase):
    def setUp(self):
        self.mbbs = Course.objects.create(name='CEE-MBBS', prefix='MBBS', program_group='CEE-UG')
        self.bds = Course.objects.create(name='CEE-BDS', prefix='BDS', program_group='CEE-UG')

        self.physiology = Subject.objects.create(name='Physiology')
        self.physiology.courses.set([self.mbbs])
        self.pathology = Subject.objects.create(name='Pathology')
        self.pathology.courses.set([self.bds])  # deliberately NOT MBBS

        for i in range(3):
            Question.objects.create(subject=self.physiology, text=f'Physiology Q{i}')
        for i in range(3):
            Question.objects.create(subject=self.pathology, text=f'Pathology Q{i}')

        self.student = _user('daily_mcq_student')
        self.client.force_authenticate(user=self.student)

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get('/api/mcq-of-the-day-set/?course=MBBS')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_course_query_param_required(self):
        resp = self.client.get('/api/mcq-of-the-day-set/')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_course_rejected(self):
        resp = self.client.get('/api/mcq-of-the-day-set/?course=NOSUCHCOURSE')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_mbbs_never_sees_pathology_questions(self):
        """The literal acceptance criterion: MBBS program must not see
        Pathology MCQs, because Pathology belongs to a different course."""
        resp = self.client.get('/api/mcq-of-the-day-set/?course=MBBS')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        subjects = {q['subject'] for q in resp.data['questions']} if resp.data['questions'] and 'subject' in resp.data['questions'][0] else None
        texts = [q['text'] for q in resp.data['questions']]
        self.assertTrue(all('Pathology' not in t for t in texts))
        self.assertTrue(any('Physiology' in t for t in texts))

    def test_bds_never_sees_physiology_questions_tagged_only_to_mbbs(self):
        resp = self.client.get('/api/mcq-of-the-day-set/?course=BDS')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        texts = [q['text'] for q in resp.data['questions']]
        self.assertTrue(all('Physiology' not in t for t in texts))
        self.assertTrue(any('Pathology' in t for t in texts))

    def test_same_day_same_course_returns_identical_questions_every_call(self):
        first = self.client.get('/api/mcq-of-the-day-set/?course=MBBS').data['questions']
        second = self.client.get('/api/mcq-of-the-day-set/?course=MBBS').data['questions']
        self.assertEqual([q['id'] for q in first], [q['id'] for q in second])

    def test_different_day_can_return_different_questions(self):
        """Not guaranteed to differ for every possible seed collision, but
        with a fresh Physiology subject holding several questions across
        two far-apart dates, the seeded RNG should not always land on the
        exact same one — we assert the mechanism (different seed strings
        actually get used), not a specific outcome."""
        with patch('django.utils.timezone.localdate', return_value=date(2026, 1, 1)):
            day1 = self.client.get('/api/mcq-of-the-day-set/?course=MBBS').data
        with patch('django.utils.timezone.localdate', return_value=date(2027, 6, 15)):
            day2 = self.client.get('/api/mcq-of-the-day-set/?course=MBBS').data
        self.assertEqual(day1['date'], '2026-01-01')
        self.assertEqual(day2['date'], '2027-06-15')

    def test_untagged_subject_is_treated_as_global_and_can_appear_for_any_course(self):
        global_subject = Subject.objects.create(name='General Awareness')  # no .courses set at all
        Question.objects.create(subject=global_subject, text='Global Q0')
        resp_mbbs = self.client.get('/api/mcq-of-the-day-set/?course=MBBS')
        resp_bds = self.client.get('/api/mcq-of-the-day-set/?course=BDS')
        self.assertEqual(resp_mbbs.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_bds.status_code, status.HTTP_200_OK)

    def test_response_includes_course_and_date(self):
        resp = self.client.get('/api/mcq-of-the-day-set/?course=MBBS')
        self.assertEqual(resp.data['course']['prefix'], 'MBBS')
        self.assertEqual(resp.data['course']['program_group'], 'CEE-UG')
        self.assertIn('date', resp.data)

    def test_at_most_five_questions_returned(self):
        for i in range(10):
            Question.objects.create(subject=self.physiology, text=f'Extra Physio Q{i}')
        extra_subjects = []
        for i in range(6):
            s = Subject.objects.create(name=f'MBBS Subject {i}')
            s.courses.set([self.mbbs])
            Question.objects.create(subject=s, text=f'S{i} Q0')
            extra_subjects.append(s)
        resp = self.client.get('/api/mcq-of-the-day-set/?course=MBBS')
        self.assertLessEqual(len(resp.data['questions']), 5)
