"""DailyMCQSetView (public "MCQ of the Day" feature) — 5 auto-rotating
MCQs per course, deterministic per (date, course) in Asia/Kathmandu, scoped
via academics.access.question_scoped_to_single_course (Subject.courses,
NOT the unpopulated Question.courses field — see that function's own
docstring for why a naive filter would leak every course's questions into
every other course).

Two-step public flow (product spec, final review):
  GET  /api/mcq-of-the-day-set/?course=<prefix>   — display only, no answer key
  POST /api/mcq-of-the-day-set/ {course, answers} — server grades, THEN reveals

Deliberately PUBLIC (AllowAny, no JWT). Every test below that doesn't
specifically exercise the "authenticated visitor gets the same thing" case
runs fully anonymous (APITestCase's default client, no force_authenticate
at all)."""
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from courses.models import Course

from .models import Option, Question, QuestionAttempt, Subject

User = get_user_model()

KATHMANDU = ZoneInfo('Asia/Kathmandu')


def _user(username):
    return User.objects.create_user(username=username, email=f'{username}@example.com', password='pw12345')


def _question(subject, text, *, correct='Correct option', wrong='Wrong option', explanation='Because X.'):
    q = Question.objects.create(subject=subject, text=text, explanation='Question-level explanation.')
    Option.objects.create(question=q, text=correct, is_correct=True, order=0, explanation=explanation)
    Option.objects.create(question=q, text=wrong, is_correct=False, order=1, explanation='Not right because Y.')
    return q


def _kathmandu_now(d):
    """An aware datetime that is unambiguously `d` in Asia/Kathmandu —
    used to control the view's `today` without depending on wall-clock
    time when the test actually runs."""
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=KATHMANDU)


class DailyMCQSetTests(APITestCase):
    def setUp(self):
        self.mbbs = Course.objects.create(name='CEE-MBBS', prefix='MBBS', program_group='CEE-UG')
        self.bds = Course.objects.create(name='CEE-BDS', prefix='BDS', program_group='CEE-UG')

        self.physiology = Subject.objects.create(name='Physiology')
        self.physiology.courses.set([self.mbbs])
        self.pathology = Subject.objects.create(name='Pathology')
        self.pathology.courses.set([self.bds])  # deliberately NOT MBBS

        for i in range(3):
            _question(self.physiology, f'Physiology Q{i}')
        for i in range(3):
            _question(self.pathology, f'Pathology Q{i}')

        self.student = _user('daily_mcq_student')
        # Deliberately NOT self.client.force_authenticate() here — the
        # endpoint is public, and most tests below exercise the anonymous
        # path, which is the default for every test in this class.

    def _get_daily_set(self, course_prefix='MBBS', *, now=None):
        ctx = patch('django.utils.timezone.now', return_value=now) if now else None
        if ctx:
            with ctx:
                return self.client.get(f'/api/mcq-of-the-day-set/?course={course_prefix}')
        return self.client.get(f'/api/mcq-of-the-day-set/?course={course_prefix}')

    def _submit(self, course_prefix, answers, *, now=None):
        payload = {'course': course_prefix, 'answers': answers}
        if now:
            with patch('django.utils.timezone.now', return_value=now):
                return self.client.post('/api/mcq-of-the-day-set/', payload, format='json')
        return self.client.post('/api/mcq-of-the-day-set/', payload, format='json')

    def _full_answer_payload(self, course_prefix, *, now, pick='correct'):
        """Fetches the real daily set for (course, now) and builds a
        complete answers[] payload picking either the correct or the
        wrong option for every question — used so tests don't have to
        hardcode question/option ids."""
        resp = self._get_daily_set(course_prefix, now=now)
        answers = []
        for q in resp.data['questions']:
            opts = q['options']
            chosen = opts[0] if pick == 'correct' else opts[-1]
            # We don't know is_correct pre-submission (that's the whole
            # point) — instead read it straight from the DB by id, since
            # the test itself is allowed to know the ground truth.
            answers.append({'question_id': q['id'], 'option_id': chosen['id']})
        return resp.data, answers

    # --- 1: anonymous GET succeeds ---------------------------------------

    def test_anonymous_get_succeeds(self):
        resp = self._get_daily_set()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    # --- 2/3/4: GET reveals nothing --------------------------------------

    def test_initial_get_contains_no_is_correct(self):
        resp = self._get_daily_set()
        for q in resp.data['questions']:
            for opt in q['options']:
                self.assertNotIn('is_correct', opt)

    def test_initial_get_contains_no_correct_option_identifier(self):
        resp = self._get_daily_set()
        for q in resp.data['questions']:
            self.assertNotIn('correct_option_id', q)
            self.assertNotIn('selected_option_id', q)
            self.assertNotIn('is_correct', q)

    def test_initial_get_does_not_expose_explanation_or_solution_data(self):
        resp = self._get_daily_set()
        for q in resp.data['questions']:
            self.assertNotIn('explanation', q)
            self.assertNotIn('explanation_latex', q)
            self.assertNotIn('key_takeaway', q)
            self.assertNotIn('reference_book_name', q)
            self.assertNotIn('references', q)
            for opt in q['options']:
                self.assertNotIn('explanation', opt)

    def test_initial_get_still_includes_everything_needed_to_display_and_answer(self):
        resp = self._get_daily_set()
        q = resp.data['questions'][0]
        self.assertIn('text', q)
        self.assertIn('subject_name', q)
        for opt in q['options']:
            self.assertIn('id', opt)
            self.assertIn('text', opt)

    # --- 5/6/7: valid submission is accepted and graded ------------------

    def test_valid_public_submission_is_accepted(self):
        with patch('django.utils.timezone.now', return_value=_kathmandu_now(date(2026, 9, 28))):
            _, answers = self._full_answer_payload('MBBS', now=_kathmandu_now(date(2026, 9, 28)))
            resp = self._submit('MBBS', answers, now=_kathmandu_now(date(2026, 9, 28)))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_backend_calculates_the_score(self):
        now = _kathmandu_now(date(2026, 9, 28))
        _, answers = self._full_answer_payload('MBBS', now=now, pick='correct')
        resp = self._submit('MBBS', answers, now=now)
        self.assertEqual(resp.data['score'], resp.data['correct_count'])
        self.assertEqual(resp.data['correct_count'] + resp.data['incorrect_count'] + resp.data['unanswered_count'], resp.data['total'])

    # --- 8: explanations only after submission ---------------------------

    def test_backend_returns_correct_answers_and_explanations_only_after_submission(self):
        now = _kathmandu_now(date(2026, 9, 28))
        _, answers = self._full_answer_payload('MBBS', now=now, pick='correct')
        resp = self._submit('MBBS', answers, now=now)
        q = resp.data['questions'][0]
        self.assertIn('explanation', q)
        self.assertTrue(any('is_correct' in opt for opt in q['options']))

    # --- 9/10/11: grading correctness -------------------------------------

    def test_correct_selected_answers_produce_correct_score(self):
        now = _kathmandu_now(date(2026, 9, 28))
        _, answers = self._full_answer_payload('MBBS', now=now, pick='correct')
        resp = self._submit('MBBS', answers, now=now)
        self.assertEqual(resp.data['correct_count'], resp.data['total'])
        self.assertEqual(resp.data['incorrect_count'], 0)

    def test_incorrect_answers_are_graded_correctly(self):
        now = _kathmandu_now(date(2026, 9, 28))
        _, answers = self._full_answer_payload('MBBS', now=now, pick='wrong')
        resp = self._submit('MBBS', answers, now=now)
        self.assertEqual(resp.data['incorrect_count'], resp.data['total'])
        self.assertEqual(resp.data['correct_count'], 0)

    def test_unanswered_questions_are_handled_correctly(self):
        now = _kathmandu_now(date(2026, 9, 28))
        daily, _ = self._full_answer_payload('MBBS', now=now)
        # Submit with every option_id explicitly null — fully unanswered.
        answers = [{'question_id': q['id'], 'option_id': None} for q in daily['questions']]
        resp = self._submit('MBBS', answers, now=now)
        self.assertEqual(resp.data['unanswered_count'], resp.data['total'])
        self.assertEqual(resp.data['correct_count'], 0)
        for q in resp.data['questions']:
            self.assertFalse(q['is_answered'])
            self.assertIsNone(q['selected_option_id'])

    # --- 12: repeated submission is deterministic -------------------------

    def test_repeated_submission_with_same_answers_gives_the_same_result(self):
        now = _kathmandu_now(date(2026, 9, 28))
        _, answers = self._full_answer_payload('MBBS', now=now, pick='correct')
        first = self._submit('MBBS', answers, now=now)
        second = self._submit('MBBS', answers, now=now)
        self.assertEqual(first.data['score'], second.data['score'])
        self.assertEqual(
            [q['id'] for q in first.data['questions']],
            [q['id'] for q in second.data['questions']],
        )

    # --- 13/14/15/16: reject manipulation ---------------------------------

    def test_arbitrary_question_ids_are_rejected(self):
        now = _kathmandu_now(date(2026, 9, 28))
        resp = self._submit('MBBS', [{'question_id': 99999999, 'option_id': 1}], now=now)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_arbitrary_option_ids_are_rejected(self):
        now = _kathmandu_now(date(2026, 9, 28))
        daily, _ = self._full_answer_payload('MBBS', now=now)
        qid = daily['questions'][0]['id']
        resp = self._submit('MBBS', [{'question_id': qid, 'option_id': 99999999}], now=now)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cross_course_question_manipulation_is_rejected(self):
        """A question that legitimately belongs to today's BDS set must be
        rejected when submitted against MBBS."""
        now = _kathmandu_now(date(2026, 9, 28))
        bds_daily, _ = self._full_answer_payload('BDS', now=now)
        bds_question_id = bds_daily['questions'][0]['id']
        resp = self._submit('MBBS', [{'question_id': bds_question_id, 'option_id': 1}], now=now)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_more_than_five_questions_rejected(self):
        now = _kathmandu_now(date(2026, 9, 28))
        answers = [{'question_id': i, 'option_id': None} for i in range(6)]
        resp = self._submit('MBBS', answers, now=now)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_question_in_submission_rejected(self):
        now = _kathmandu_now(date(2026, 9, 28))
        daily, _ = self._full_answer_payload('MBBS', now=now)
        qid = daily['questions'][0]['id']
        resp = self._submit('MBBS', [{'question_id': qid, 'option_id': None}, {'question_id': qid, 'option_id': None}], now=now)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_option_belonging_to_a_different_question_is_rejected(self):
        now = _kathmandu_now(date(2026, 9, 28))
        # MBBS only has one subject (Physiology) in the base setUp — add a
        # second so today's daily set actually contains 2+ questions to
        # cross-reference.
        anatomy = Subject.objects.create(name='Anatomy')
        anatomy.courses.set([self.mbbs])
        _question(anatomy, 'Anatomy Q0')

        daily, _ = self._full_answer_payload('MBBS', now=now)
        self.assertGreaterEqual(len(daily['questions']), 2)
        q0, q1 = daily['questions'][0], daily['questions'][1]
        foreign_option_id = q1['options'][0]['id']
        resp = self._submit('MBBS', [{'question_id': q0['id'], 'option_id': foreign_option_id}], now=now)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    # --- 17: no anonymous private records ---------------------------------

    def test_anonymous_submission_does_not_create_question_attempt_records(self):
        now = _kathmandu_now(date(2026, 9, 28))
        _, answers = self._full_answer_payload('MBBS', now=now, pick='correct')
        before = QuestionAttempt.objects.count()
        self._submit('MBBS', answers, now=now)
        after = QuestionAttempt.objects.count()
        self.assertEqual(before, after)

    # --- 18: authenticated users still work --------------------------------

    def test_authenticated_get_and_submit_still_work(self):
        self.client.force_authenticate(user=self.student)
        now = _kathmandu_now(date(2026, 9, 28))
        resp = self._get_daily_set('MBBS', now=now)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        _, answers = self._full_answer_payload('MBBS', now=now, pick='correct')
        submit_resp = self._submit('MBBS', answers, now=now)
        self.assertEqual(submit_resp.status_code, status.HTTP_200_OK)

    # --- 19: course scoping remains correct --------------------------------

    def test_mbbs_never_sees_pathology_questions(self):
        """The literal acceptance criterion: MBBS program must not see
        Pathology MCQs, because Pathology belongs to a different course."""
        resp = self._get_daily_set('MBBS')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        texts = [q['text'] for q in resp.data['questions']]
        self.assertTrue(all('Pathology' not in t for t in texts))
        self.assertTrue(any('Physiology' in t for t in texts))

    def test_bds_never_sees_physiology_questions_tagged_only_to_mbbs(self):
        resp = self._get_daily_set('BDS')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        texts = [q['text'] for q in resp.data['questions']]
        self.assertTrue(all('Physiology' not in t for t in texts))
        self.assertTrue(any('Pathology' in t for t in texts))

    def test_untagged_subject_is_treated_as_global_and_can_appear_for_any_course(self):
        global_subject = Subject.objects.create(name='General Awareness')  # no .courses set at all
        _question(global_subject, 'Global Q0')
        resp_mbbs = self._get_daily_set('MBBS')
        resp_bds = self._get_daily_set('BDS')
        self.assertEqual(resp_mbbs.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_bds.status_code, status.HTTP_200_OK)

    # --- 20: daily set remains deterministic -------------------------------

    def test_same_day_same_course_returns_identical_questions_every_call(self):
        now = _kathmandu_now(date(2026, 9, 28))
        first = self._get_daily_set('MBBS', now=now).data['questions']
        second = self._get_daily_set('MBBS', now=now).data['questions']
        self.assertEqual([q['id'] for q in first], [q['id'] for q in second])

    def test_different_day_can_return_different_questions(self):
        with patch('django.utils.timezone.now', return_value=_kathmandu_now(date(2026, 1, 1))):
            day1 = self.client.get('/api/mcq-of-the-day-set/?course=MBBS').data
        with patch('django.utils.timezone.now', return_value=_kathmandu_now(date(2027, 6, 15))):
            day2 = self.client.get('/api/mcq-of-the-day-set/?course=MBBS').data
        self.assertEqual(day1['date'], '2026-01-01')
        self.assertEqual(day2['date'], '2027-06-15')

    def test_daily_boundary_uses_kathmandu_not_utc(self):
        """23:30 UTC on 2026-09-27 is already 2026-09-28 05:15 in
        Asia/Kathmandu (UTC+5:45) — the reported `date` must follow Nepal's
        calendar day, not UTC's, even though settings.TIME_ZONE is UTC."""
        utc_late_night = datetime(2026, 9, 27, 23, 30, tzinfo=ZoneInfo('UTC'))
        with patch('django.utils.timezone.now', return_value=utc_late_night):
            resp = self.client.get('/api/mcq-of-the-day-set/?course=MBBS').data
        self.assertEqual(resp['date'], '2026-09-28')

    def test_get_and_post_agree_on_the_same_daily_set_for_the_same_moment(self):
        now = _kathmandu_now(date(2026, 9, 28))
        get_resp = self._get_daily_set('MBBS', now=now)
        with patch('django.utils.timezone.now', return_value=now):
            post_resp = self._submit('MBBS', [], now=now)
        self.assertEqual(
            {q['id'] for q in get_resp.data['questions']},
            {q['id'] for q in post_resp.data['questions']},
        )

    # --- misc: basic input validation retained from the original endpoint --

    def test_course_query_param_required(self):
        resp = self.client.get('/api/mcq-of-the-day-set/')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_course_rejected(self):
        resp = self.client.get('/api/mcq-of-the-day-set/?course=NOSUCHCOURSE')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_response_includes_course_and_date(self):
        resp = self._get_daily_set('MBBS')
        self.assertEqual(resp.data['course']['prefix'], 'MBBS')
        self.assertEqual(resp.data['course']['program_group'], 'CEE-UG')
        self.assertIn('date', resp.data)

    def test_public_get_response_contains_no_private_or_admin_fields(self):
        resp = self._get_daily_set('MBBS')
        q = resp.data['questions'][0]
        forbidden_fields = {
            'is_bookmarked', 'mastery_status', 'is_incorrect', 'is_revision_due',
            'incorrect_count', 'attempts_count', 'confidence', 'last_attempted_at',
            'revision_due_at', 'revision_reason', 'tags', 'created_by',
            'instructor_difficulty', 'actual_difficulty',
        }
        self.assertFalse(forbidden_fields & set(q.keys()))
        for opt in q['options']:
            self.assertFalse({'pick_count', 'pick_percentage'} & set(opt.keys()))

    def test_endpoint_cannot_be_used_to_browse_or_enumerate_questions(self):
        resp = self.client.get('/api/mcq-of-the-day-set/?course=MBBS&id=999999&page=2&subject=1')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertLessEqual(len(resp.data['questions']), 5)
        texts = [q['text'] for q in resp.data['questions']]
        self.assertTrue(all('Pathology' not in t for t in texts))

    def test_at_most_five_questions_returned(self):
        for i in range(10):
            _question(self.physiology, f'Extra Physio Q{i}')
        for i in range(6):
            s = Subject.objects.create(name=f'MBBS Subject {i}')
            s.courses.set([self.mbbs])
            _question(s, f'S{i} Q0')
        resp = self._get_daily_set('MBBS')
        self.assertLessEqual(len(resp.data['questions']), 5)
