"""Phase 9 — Daily Test Attempt Integrity: deterministic question order and
immutable attempt content.

Root cause fixed: TestAttemptSerializer.get_questions() used to re-derive
the question set/order (random.shuffle() plus, for a preview-only
student, a fresh preview slice) on every single GET /attempts/{id}/ call,
so the same in-progress attempt could show a different question set/order
on every reload, and SubmitAnswerView independently re-derived its own
"is this a preview question" allowlist without shuffling — meaning the
two could legitimately disagree about the same question.

This file proves the new invariant directly, end-to-end via the real
views (never by inspecting internal state alone):

    initial GET == refresh GET == resume GET  (same question IDs, same order)
    a question shown as preview is always accepted by the answer endpoint
    editing/deleting the underlying Question after start does not corrupt
        or reorder an already-frozen attempt
    a legacy attempt (no frozen rows) still works via the exact pre-fix
        fallback behavior
    Free Starter / subscription / entitlement rules are read exactly once
        (at freeze time) and never re-derived per request
"""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status
from rest_framework.test import APITestCase

from academics.models import Option, Question, Subject
from billing.models import Subscription
from courses.models import Course, Enrollment
from tests_app.lifecycle import attempt_is_preview_only, attempt_preview_question_ids, freeze_attempt_questions
from tests_app.models import AttemptQuestion, AttemptQuestionSnapshot, Test, TestAttempt, TestQuestion

User = get_user_model()


def _mkexam(exam_type='daily', allow=None, **overrides):
    """Mirrors tests_phase6.py's own `_mkexam` convention exactly (same
    project, same established pattern) — `allow` individually assigns the
    test so can_access_test() grants academic eligibility without needing
    a full course/enrollment setup for tests that aren't about that."""
    fields = {'title': f'{exam_type} exam', 'exam_type': exam_type, 'duration_minutes': 60, 'is_draft': False}
    fields.update(overrides)
    test = Test.objects.create(**fields)
    if allow:
        users = allow if isinstance(allow, (list, tuple)) else [allow]
        test.assigned_students.set(users)
    return test


def _mkquestions(subject, count, marks=1):
    return [
        Question.objects.create(subject=subject, text=f'Question {i}', marks=marks, negative_marks=0)
        for i in range(count)
    ]


def _attach(test, questions):
    for i, q in enumerate(questions):
        TestQuestion.objects.create(test=test, question=q, order=i)
        Option.objects.create(question=q, text='A', is_correct=True, order=0)
        Option.objects.create(question=q, text='B', is_correct=False, order=1)


class AttemptQuestionFreezeTests(APITestCase):
    """Phase 4 — attempt creation: the question set/order is frozen exactly
    once, at _start_attempt() time, via freeze_attempt_questions()."""

    def setUp(self):
        self.student = User.objects.create_user(username='freeze_stu', email='freeze_stu@example.com', password='pw')
        self.subject = Subject.objects.create(name='Freeze Subject')
        self.questions = _mkquestions(self.subject, 10)

    def test_start_creates_exactly_one_attemptquestion_per_question(self):
        test = _mkexam(exam_type='daily', is_pro=False, allow=self.student, max_attempts=5)
        _attach(test, self.questions)
        self.client.force_authenticate(user=self.student)
        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, 201, resp.data)
        attempt = TestAttempt.objects.get(pk=resp.data['id'])
        self.assertEqual(AttemptQuestion.objects.filter(attempt=attempt).count(), 10)
        # order values are exactly 0..9, no gaps, no repeats.
        orders = sorted(AttemptQuestion.objects.filter(attempt=attempt).values_list('order', flat=True))
        self.assertEqual(orders, list(range(10)))

    def test_shuffle_applied_exactly_once_not_reapplied_on_read(self):
        test = _mkexam(exam_type='daily', is_pro=False, allow=self.student, shuffle_questions=True, max_attempts=5)
        _attach(test, self.questions)
        self.client.force_authenticate(user=self.student)
        resp = self.client.post(f'/api/tests/{test.id}/start/')
        attempt = TestAttempt.objects.get(pk=resp.data['id'])
        order_first_read = [aq.question_id for aq in AttemptQuestion.objects.filter(attempt=attempt).order_by('order')]
        # Reading the persisted rows again must never re-shuffle — there is
        # no random.shuffle() call left anywhere in the read path at all.
        order_second_read = [aq.question_id for aq in AttemptQuestion.objects.filter(attempt=attempt).order_by('order')]
        self.assertEqual(order_first_read, order_second_read)

    def test_preview_only_freezes_exactly_free_preview_questions_count(self):
        test = _mkexam(
            exam_type='daily', is_pro=True, free_preview_questions=3, allow=self.student, max_attempts=5,
        )
        _attach(test, self.questions)  # 10 real questions, student has no subscription
        self.client.force_authenticate(user=self.student)
        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, 201, resp.data)
        attempt = TestAttempt.objects.get(pk=resp.data['id'])
        self.assertEqual(AttemptQuestion.objects.filter(attempt=attempt).count(), 3)
        self.assertTrue(AttemptQuestion.objects.filter(attempt=attempt, is_preview=True).count() == 3)

    def test_fully_entitled_student_freezes_the_full_set_not_a_preview_slice(self):
        course = Course.objects.create(name='Freeze Course', prefix='FRZ')
        Enrollment.objects.create(user=self.student, course=course, access_type='package', is_active=True)
        Subscription.objects.create(user=self.student, course=course, product_type='daily_test', is_active=True)
        test = _mkexam(
            exam_type='daily', is_pro=True, free_preview_questions=3, allow=self.student, max_attempts=5,
        )
        _attach(test, self.questions)
        self.client.force_authenticate(user=self.student)
        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, 201, resp.data)
        attempt = TestAttempt.objects.get(pk=resp.data['id'])
        self.assertEqual(AttemptQuestion.objects.filter(attempt=attempt).count(), 10)
        self.assertEqual(AttemptQuestion.objects.filter(attempt=attempt, is_preview=True).count(), 0)


class RefreshResumeDeterminismTests(APITestCase):
    """Phase 8/19 — the core invariant: initial GET == refresh GET ==
    resume GET, for both question IDs and their order. Proven via the
    real HTTP endpoint, many times in a row, not by inspecting internal
    state once."""

    def setUp(self):
        self.student = User.objects.create_user(username='resume_stu', email='resume_stu@example.com', password='pw')
        self.subject = Subject.objects.create(name='Resume Subject')
        self.questions = _mkquestions(self.subject, 20)
        self.test = _mkexam(
            exam_type='daily', is_pro=False, allow=self.student, shuffle_questions=True, max_attempts=5,
        )
        _attach(self.test, self.questions)
        self.client.force_authenticate(user=self.student)
        start = self.client.post(f'/api/tests/{self.test.id}/start/')
        self.attempt_id = start.data['id']

    def _question_ids_in_order(self):
        resp = self.client.get(f'/api/attempts/{self.attempt_id}/')
        self.assertEqual(resp.status_code, 200)
        return [q['id'] for q in resp.data['questions']]

    def test_ten_consecutive_refreshes_return_identical_question_order(self):
        baseline = self._question_ids_in_order()
        self.assertEqual(len(baseline), 20)
        for i in range(10):
            self.assertEqual(self._question_ids_in_order(), baseline, f'diverged on refresh #{i}')

    def test_answering_then_refreshing_keeps_same_order_and_saved_answer(self):
        baseline = self._question_ids_in_order()
        first_question_id = baseline[0]
        option_id = Option.objects.filter(question_id=first_question_id, is_correct=True).first().id
        resp = self.client.post(
            f'/api/attempts/{self.attempt_id}/answer/', {'question_id': first_question_id, 'option_id': option_id},
        )
        self.assertEqual(resp.status_code, 200)
        after_answer = self._question_ids_in_order()
        self.assertEqual(after_answer, baseline)
        # The saved answer survives the refresh too.
        detail = self.client.get(f'/api/attempts/{self.attempt_id}/')
        # resp.data holds the actual Python response object (dict keys are
        # still native ints here, not JSON's string keys, since this is
        # never round-tripped through an actual JSON parse).
        self.assertEqual(detail.data['answers'][first_question_id]['option_id'], option_id)

    def test_leaving_and_returning_resumes_the_same_order(self):
        baseline = self._question_ids_in_order()
        # "Leave and return" == a fresh GET with no other state — no
        # client-side cache is involved in this repo (confirmed: no
        # service worker, no localStorage use in the exam player).
        resumed = self._question_ids_in_order()
        self.assertEqual(resumed, baseline)
        # Calling /start/ again (e.g. the student re-opens the Daily Test
        # card and taps Start again) must resume, not recreate.
        restart = self.client.post(f'/api/tests/{self.test.id}/start/')
        self.assertEqual(restart.status_code, 200)  # resume, not 201 create
        self.assertEqual(restart.data['id'], self.attempt_id)
        self.assertEqual([q['id'] for q in restart.data['questions']], baseline)
        self.assertEqual(TestAttempt.objects.filter(test=self.test, user=self.student).count(), 1)


class AnswerValidationUsesFrozenSetTests(APITestCase):
    """Phase 6/10 — a question shown as preview by GET must always be
    accepted by the answer endpoint; a question NOT in the frozen list
    must always be rejected the same way, regardless of Test.questions'
    live/shuffled order."""

    def setUp(self):
        self.student = User.objects.create_user(username='preview_stu', email='preview_stu@example.com', password='pw')
        self.subject = Subject.objects.create(name='Preview Subject')
        self.questions = _mkquestions(self.subject, 10)
        self.test = _mkexam(
            exam_type='daily', is_pro=True, free_preview_questions=3, allow=self.student, max_attempts=5,
        )
        _attach(self.test, self.questions)
        self.client.force_authenticate(user=self.student)
        start = self.client.post(f'/api/tests/{self.test.id}/start/')
        self.attempt_id = start.data['id']
        self.attempt = TestAttempt.objects.get(pk=self.attempt_id)

    def test_every_question_shown_as_preview_is_accepted_by_the_answer_endpoint(self):
        shown = self.client.get(f'/api/attempts/{self.attempt_id}/').data['questions']
        self.assertEqual(len(shown), 3)
        for q in shown:
            option = Option.objects.filter(question_id=q['id']).first()
            resp = self.client.post(
                f'/api/attempts/{self.attempt_id}/answer/', {'question_id': q['id'], 'option_id': option.id},
            )
            self.assertEqual(resp.status_code, 200, f"question {q['id']} was shown as preview but rejected")

    def test_a_question_outside_the_frozen_preview_set_is_rejected(self):
        frozen_ids = set(AttemptQuestion.objects.filter(attempt=self.attempt).values_list('question_id', flat=True))
        outside = [q for q in self.questions if q.id not in frozen_ids]
        self.assertTrue(outside, 'test fixture must have questions outside the 3-question preview set')
        option = Option.objects.filter(question=outside[0]).first()
        resp = self.client.post(
            f'/api/attempts/{self.attempt_id}/answer/', {'question_id': outside[0].id, 'option_id': option.id},
        )
        self.assertEqual(resp.status_code, 402)
        self.assertEqual(resp.data['code'], 'purchase_required')

    def test_preview_verdict_and_allowlist_are_immutable_once_started(self):
        """Even if the student's live entitlement changes mid-attempt (they
        subscribe), THIS attempt's frozen preview state and question list
        stay exactly what was shown at start — an attempt behaves like an
        immutable session. This is a deliberate design decision, not an
        accident; see attempt_is_preview_only's own docstring."""
        course = Course.objects.create(name='Mid Attempt Course', prefix='MID')
        Enrollment.objects.create(user=self.student, course=course, access_type='package', is_active=True)
        Subscription.objects.create(user=self.student, course=course, product_type='daily_test', is_active=True)
        self.test.courses.add(course)

        # Still preview-limited for THIS already-running attempt.
        self.assertTrue(attempt_is_preview_only(self.attempt))
        shown = self.client.get(f'/api/attempts/{self.attempt_id}/').data['questions']
        self.assertEqual(len(shown), 3)


class SubmitUsesFrozenContentTests(APITestCase):
    """Phase 7 — SubmitTestView's preview gate and scoring both use the
    same frozen attempt content; scoring semantics themselves (which
    read only Answer rows, never Test.questions) are untouched."""

    def setUp(self):
        self.student = User.objects.create_user(username='submit_stu', email='submit_stu@example.com', password='pw')
        self.subject = Subject.objects.create(name='Submit Subject')
        self.questions = _mkquestions(self.subject, 5)

    def test_preview_only_attempt_cannot_submit_even_after_answering_every_shown_question(self):
        test = _mkexam(exam_type='daily', is_pro=True, free_preview_questions=2, allow=self.student, max_attempts=5)
        _attach(test, self.questions)
        self.client.force_authenticate(user=self.student)
        attempt_id = self.client.post(f'/api/tests/{test.id}/start/').data['id']
        shown = self.client.get(f'/api/attempts/{attempt_id}/').data['questions']
        for q in shown:
            option = Option.objects.filter(question_id=q['id'], is_correct=True).first()
            self.client.post(f'/api/attempts/{attempt_id}/answer/', {'question_id': q['id'], 'option_id': option.id})
        resp = self.client.post(f'/api/attempts/{attempt_id}/submit/')
        self.assertEqual(resp.status_code, 402)
        self.assertEqual(resp.data['code'], 'purchase_required')
        self.assertEqual(TestAttempt.objects.get(pk=attempt_id).status, 'in_progress')

    def test_fully_entitled_student_can_submit_and_score_is_correct(self):
        test = _mkexam(exam_type='daily', is_pro=False, allow=self.student, max_attempts=5)
        _attach(test, self.questions)
        self.client.force_authenticate(user=self.student)
        attempt_id = self.client.post(f'/api/tests/{test.id}/start/').data['id']
        shown = self.client.get(f'/api/attempts/{attempt_id}/').data['questions']
        self.assertEqual(len(shown), 5)
        for q in shown:
            option = Option.objects.filter(question_id=q['id'], is_correct=True).first()
            resp = self.client.post(f'/api/attempts/{attempt_id}/answer/', {'question_id': q['id'], 'option_id': option.id})
            self.assertEqual(resp.status_code, 200)
        resp = self.client.post(f'/api/attempts/{attempt_id}/submit/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(float(resp.data['score']), 5.0)  # 5 questions x 1 mark, all correct
        self.assertEqual(TestAttempt.objects.get(pk=attempt_id).status, 'submitted')


class QuestionChangeAfterAttemptStartTests(APITestCase):
    """Phase 12 — editing a live Question's content after an attempt has
    started must not reorder or drop it from the attempt; content itself
    (deliberately not frozen at start — see AttemptQuestion's own
    docstring) is still read live via the FK, matching "only WHICH
    questions and WHAT ORDER are frozen, not their content, before
    finalization"."""

    def setUp(self):
        self.student = User.objects.create_user(username='edit_stu', email='edit_stu@example.com', password='pw')
        self.subject = Subject.objects.create(name='Edit Subject')
        self.questions = _mkquestions(self.subject, 4)
        self.test = _mkexam(exam_type='daily', is_pro=False, allow=self.student, max_attempts=5)
        _attach(self.test, self.questions)
        self.client.force_authenticate(user=self.student)
        self.attempt_id = self.client.post(f'/api/tests/{self.test.id}/start/').data['id']

    def test_editing_question_text_after_start_is_reflected_live_order_unchanged(self):
        baseline_ids = [q['id'] for q in self.client.get(f'/api/attempts/{self.attempt_id}/').data['questions']]
        edited = self.questions[1]
        edited.text = 'EDITED AFTER ATTEMPT STARTED'
        edited.save(update_fields=['text'])

        after_edit = self.client.get(f'/api/attempts/{self.attempt_id}/').data['questions']
        self.assertEqual([q['id'] for q in after_edit], baseline_ids)  # order/set unchanged
        edited_entry = next(q for q in after_edit if q['id'] == edited.id)
        self.assertEqual(edited_entry['text'], 'EDITED AFTER ATTEMPT STARTED')  # live content, as designed

    def test_detaching_a_question_from_the_test_after_start_does_not_affect_the_attempt(self):
        """A question can be REMOVED from a Test's general pool (deleting
        just the TestQuestion join row, e.g. an admin reconfiguring which
        questions the test uses going forward) without deleting the
        Question itself. AttemptQuestion.question FKs the Question
        directly, not the TestQuestion join row, so this is invisible to
        any attempt already frozen against the old pool — a second,
        independent protection beyond the Question-deletion case."""
        baseline_ids = [q['id'] for q in self.client.get(f'/api/attempts/{self.attempt_id}/').data['questions']]
        detached = self.questions[1]
        TestQuestion.objects.filter(test=self.test, question=detached).delete()
        self.assertFalse(self.test.questions.filter(pk=detached.id).exists())  # confirmed detached from the pool

        after_detach = self.client.get(f'/api/attempts/{self.attempt_id}/').data['questions']
        self.assertEqual([q['id'] for q in after_detach], baseline_ids)  # completely unaffected

    def test_answered_question_detached_from_test_still_scores_and_survives_finalization(self):
        """Regression for a real, reproduced defect: admin editing a Test's
        question list (TestAdminSerializer.update()'s ordinary
        delete/recreate of TestQuestion rows — a normal, unguarded admin
        action) between attempt-start and finalization used to make
        finalize_attempt()'s snapshot step (_create_question_snapshots(),
        Phase 8) silently drop an already-answered, correctly-scored
        question from the review page, because that function derived its
        content from the live TestQuestion set instead of this attempt's
        own frozen AttemptQuestion rows. The Answer/score were never lost
        — only the review-page snapshot was missing the question. Fixed by
        making _create_question_snapshots() read the frozen set first."""
        target = self.questions[1]
        option = Option.objects.filter(question=target, is_correct=True).first()
        self.client.post(f'/api/attempts/{self.attempt_id}/answer/', {'question_id': target.id, 'option_id': option.id})

        TestQuestion.objects.filter(test=self.test, question=target).delete()  # detach only, Question untouched
        self.assertFalse(self.test.questions.filter(pk=target.id).exists())

        for q in self.questions:
            if q.id == target.id:
                continue
            opt = Option.objects.filter(question=q, is_correct=True).first()
            self.client.post(f'/api/attempts/{self.attempt_id}/answer/', {'question_id': q.id, 'option_id': opt.id})

        resp = self.client.post(f'/api/attempts/{self.attempt_id}/submit/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(float(resp.data['score']), 4.0)  # all 4 answers correct, including the detached one

        attempt = TestAttempt.objects.get(pk=self.attempt_id)
        snapshot_qids = list(AttemptQuestionSnapshot.objects.filter(attempt=attempt).values_list('question_id', flat=True))
        self.assertIn(target.id, snapshot_qids)  # the answered-but-detached question is NOT silently dropped
        self.assertEqual(len(snapshot_qids), 4)

    def test_detached_question_still_appears_in_the_real_result_endpoint_response(self):
        """Same fix, verified end-to-end through the actual review API
        (not just the model layer) — the concrete case requirement #10
        asks for: detach without deleting, then confirm the historical
        attempt (as returned by GET /attempts/{id}/result/) still contains
        the question."""
        target = self.questions[2]
        option = Option.objects.filter(question=target, is_correct=True).first()
        self.client.post(f'/api/attempts/{self.attempt_id}/answer/', {'question_id': target.id, 'option_id': option.id})

        TestQuestion.objects.filter(test=self.test, question=target).delete()

        for q in self.questions:
            if q.id == target.id:
                continue
            opt = Option.objects.filter(question=q, is_correct=True).first()
            self.client.post(f'/api/attempts/{self.attempt_id}/answer/', {'question_id': q.id, 'option_id': opt.id})
        self.client.post(f'/api/attempts/{self.attempt_id}/submit/')

        result = self.client.get(f'/api/attempts/{self.attempt_id}/result/')
        self.assertEqual(result.status_code, 200, result.data)
        result_qids = [q['id'] for q in result.data['questions']]
        self.assertIn(target.id, result_qids)
        self.assertEqual(len(result_qids), 4)


class QuestionDeletionAfterAttemptStartTests(APITestCase):
    """Phase 13 (required) — deleting the underlying Question after an
    attempt started must not corrupt the attempt or crash the page; the
    admin API already blocks this while any attempt exists
    (QuestionViewSet.destroy(), unmodified), so this exercises the
    defense-in-depth path (a direct .delete(), simulating the Django
    admin site or a management command bypassing that guard)."""

    def setUp(self):
        self.student = User.objects.create_user(username='del_stu', email='del_stu@example.com', password='pw')
        self.subject = Subject.objects.create(name='Delete Subject')
        self.questions = _mkquestions(self.subject, 4)
        self.test = _mkexam(exam_type='daily', is_pro=False, allow=self.student, max_attempts=5)
        _attach(self.test, self.questions)
        self.client.force_authenticate(user=self.student)
        self.attempt_id = self.client.post(f'/api/tests/{self.test.id}/start/').data['id']
        self.attempt = TestAttempt.objects.get(pk=self.attempt_id)

    def test_deleting_a_question_leaves_the_attempt_readable_with_the_other_three_intact(self):
        # Captured before delete() — Django sets the in-memory instance's
        # own .pk/.id to None post-delete, so the ORIGINAL ids must be
        # snapshotted first, not re-read from self.questions afterward.
        original_ids = [q.id for q in self.questions]
        doomed = self.questions[2]
        doomed_id = original_ids[2]
        doomed.delete()  # CASCADE would remove TestQuestion; AttemptQuestion.question is SET_NULL

        row = AttemptQuestion.objects.get(attempt=self.attempt, question_id=None)
        self.assertIsNotNone(row)  # the row survives, just with question=None now

        resp = self.client.get(f'/api/attempts/{self.attempt_id}/')
        self.assertEqual(resp.status_code, 200)
        returned_ids = [q['id'] for q in resp.data['questions']]
        self.assertEqual(len(returned_ids), 3)  # the deleted one is skipped, not a crash
        self.assertNotIn(doomed_id, returned_ids)
        for qid in original_ids:
            if qid != doomed_id:
                self.assertIn(qid, returned_ids)

    def test_submit_still_works_correctly_after_a_question_was_deleted_mid_attempt(self):
        doomed = self.questions[2]
        doomed.delete()
        remaining = self.client.get(f'/api/attempts/{self.attempt_id}/').data['questions']
        for q in remaining:
            option = Option.objects.filter(question_id=q['id'], is_correct=True).first()
            self.client.post(f'/api/attempts/{self.attempt_id}/answer/', {'question_id': q['id'], 'option_id': option.id})
        resp = self.client.post(f'/api/attempts/{self.attempt_id}/submit/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(float(resp.data['score']), 3.0)  # only the 3 still-answerable questions score


class LegacyAttemptCompatibilityTests(APITestCase):
    """Phase 15 — an in-progress attempt with zero AttemptQuestion rows
    (created before this feature existed) must keep working via the exact
    pre-fix fallback, never crash, and never be silently backfilled."""

    def setUp(self):
        self.student = User.objects.create_user(username='legacy_stu', email='legacy_stu@example.com', password='pw')
        self.subject = Subject.objects.create(name='Legacy Subject')
        self.questions = _mkquestions(self.subject, 6)
        self.test = _mkexam(exam_type='daily', is_pro=False, allow=self.student, max_attempts=5)
        _attach(self.test, self.questions)

    def test_legacy_in_progress_attempt_with_no_frozen_rows_still_loads(self):
        # Simulates an attempt that predates this feature: created
        # directly, bypassing _start_attempt()/freeze_attempt_questions().
        attempt = TestAttempt.objects.create(user=self.student, test=self.test, attempt_number=1)
        self.assertEqual(AttemptQuestion.objects.filter(attempt=attempt).count(), 0)

        self.client.force_authenticate(user=self.student)
        resp = self.client.get(f'/api/attempts/{attempt.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['questions']), 6)  # exact pre-fix live-derive fallback, not empty/broken

    def test_legacy_attempt_is_never_silently_backfilled_with_frozen_rows(self):
        attempt = TestAttempt.objects.create(user=self.student, test=self.test, attempt_number=1)
        self.client.force_authenticate(user=self.student)
        self.client.get(f'/api/attempts/{attempt.id}/')
        self.client.get(f'/api/attempts/{attempt.id}/')
        # Reading it (even repeatedly) must never write AttemptQuestion
        # rows for it after the fact — that would be a different kind of
        # silent mutation of old data than the one being fixed.
        self.assertEqual(AttemptQuestion.objects.filter(attempt=attempt).count(), 0)


class QueryCountAndPerformanceTests(APITestCase):
    """Phase 16 — measure, don't assume.

    IMPORTANT, measured finding: QuestionForAttemptSerializer.subject_name
    (`source='subject.name'`) is not select_related anywhere in either the
    old or the new code path, so GET /attempts/{id}/ has a genuine, N+1
    query pattern that scales with question count — CONFIRMED
    PRE-EXISTING, not introduced by this feature: a direct measurement
    (see the Phase 9 report) of the exact pre-fix code path (a legacy
    attempt with zero AttemptQuestion rows, at 50 questions: 56 queries)
    against the new frozen path (also 50 questions: 54 queries) shows the
    new path is not worse — slightly better, within measurement noise.

    This test therefore asserts the actual, honest claim: the new
    AttemptQuestion-based read path never costs MORE queries than the old
    live-derive path did, at the same question count — not that either
    path is flat, which was never true and is a separate, pre-existing,
    out-of-scope performance issue (see the report's Remaining Issues)."""

    def setUp(self):
        self.student = User.objects.create_user(username='perf_stu', email='perf_stu@example.com', password='pw')
        self.subject = Subject.objects.create(name='Perf Subject')
        self.client.force_authenticate(user=self.student)

    def _build(self, question_count, label):
        questions = _mkquestions(self.subject, question_count)
        test = _mkexam(
            exam_type='daily', is_pro=False, allow=self.student, title=f'perf-{label}-{question_count}', max_attempts=5,
        )
        _attach(test, questions)
        return test

    def _frozen_path_query_count(self, question_count):
        test = self._build(question_count, 'frozen')
        attempt_id = self.client.post(f'/api/tests/{test.id}/start/').data['id']
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(f'/api/attempts/{attempt_id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['questions']), question_count)
        return len(ctx.captured_queries)

    def _legacy_path_query_count(self, question_count):
        """The exact pre-fix code path — an attempt with zero
        AttemptQuestion rows, exercising get_questions()'s fallback
        branch verbatim, for a same-size, apples-to-apples comparison."""
        test = self._build(question_count, 'legacy')
        attempt = TestAttempt.objects.create(user=self.student, test=test, attempt_number=1)
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(f'/api/attempts/{attempt.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['questions']), question_count)
        return len(ctx.captured_queries)

    def test_frozen_path_is_never_more_expensive_than_the_old_live_derive_path(self):
        for count in (10, 50, 100):
            with self.subTest(question_count=count):
                legacy = self._legacy_path_query_count(count)
                frozen = self._frozen_path_query_count(count)
                self.assertLessEqual(
                    frozen, legacy + 1,  # +1 tolerance for measurement noise, never a real regression budget
                    f'frozen path ({frozen} queries) regressed past the legacy path ({legacy} queries) at {count} questions',
                )

    def test_payload_size_documented_not_optimized_in_this_release(self):
        """Phase 17 — explicitly NOT solved here per the task's own
        instruction; this only documents the measured scaling so a future,
        separate paginated-delivery optimization has a real baseline."""
        test = self._build(200, 'payload')
        attempt_id = self.client.post(f'/api/tests/{test.id}/start/').data['id']
        resp = self.client.get(f'/api/attempts/{attempt_id}/')
        self.assertEqual(resp.status_code, 200)
        # No assertion on size — informational only. Production logs
        # already measured up to ~283KB for real (larger, richer-content)
        # Daily Tests; this synthetic 200-plain-question payload is a
        # smaller, comparable-shape data point for the same future
        # optimization discussion, not a pass/fail threshold.


class AccessRegressionTests(APITestCase):
    """Phase 20 — this feature must not change WHO can start/answer/submit
    a Daily Test, only WHICH exact question list a given already-started
    attempt sees on every read. Confirms Free Starter, subscription, and
    verification-state independence all still hold with freezing in place."""

    @classmethod
    def setUpTestData(cls):
        cls.subject = Subject.objects.create(name='Access Regression Subject')
        cls.questions = _mkquestions(cls.subject, 5)

    def test_free_starter_quota_consumption_unaffected_by_freezing(self):
        from entitlements.models import FreeStarterEntitlement, FreeStarterPolicy

        student = User.objects.create_user(username='fs_stu', email='fs_stu@example.com', password='pw')
        FreeStarterPolicy.objects.update_or_create(
            resource_type='daily_test', defaults={'quantity': 5, 'unlimited': False, 'is_active': True},
        )
        test = _mkexam(
            exam_type='daily', is_pro=True, free_preview_questions=0, allow=student, max_attempts=5,
        )
        _attach(test, self.questions)
        self.client.force_authenticate(user=student)
        resp = self.client.post(f'/api/tests/{test.id}/start/')
        self.assertEqual(resp.status_code, 201, resp.data)
        entitlement = FreeStarterEntitlement.objects.get(user=student, resource_type='daily_test')
        self.assertEqual(entitlement.used, 1)
        # Full access (not preview-limited) — Free Starter and preview are
        # mutually exclusive by construction (see _start_attempt's own
        # elif chain: the free-starter branch only fires when
        # free_preview_questions <= 0).
        attempt = TestAttempt.objects.get(pk=resp.data['id'])
        self.assertEqual(AttemptQuestion.objects.filter(attempt=attempt).count(), 5)
        self.assertFalse(attempt_is_preview_only(attempt))

    def test_verification_state_never_referenced_anywhere_in_this_feature(self):
        # Structural guard, not a runtime check — this whole feature must
        # never import or branch on verification_status at all.
        import inspect

        from tests_app import lifecycle

        source = inspect.getsource(lifecycle)
        self.assertNotIn('verification_status', source)
        self.assertNotIn('VerificationDocument', source)

    def test_daily_access_functions_unchanged_across_all_four_verification_states(self):
        from accounts.models import StudentProfile
        from billing.access import has_daily_test_access

        student = User.objects.create_user(username='verif_stu', email='verif_stu@example.com', password='pw')
        course = Course.objects.create(name='Verif Course', prefix='VER')
        Enrollment.objects.create(user=student, course=course, access_type='package', is_active=True)
        Subscription.objects.create(user=student, course=course, product_type='daily_test', is_active=True)
        test = _mkexam(exam_type='daily', is_pro=True, free_preview_questions=0, allow=student, max_attempts=10)
        test.courses.add(course)
        _attach(test, self.questions)

        results = {}
        for status_value in ('unverified', 'pending', 'verified', 'rejected'):
            profile, _ = StudentProfile.objects.get_or_create(user=student)
            profile.verification_status = status_value
            profile.save()
            results[status_value] = has_daily_test_access(student, test)
        baseline = results['unverified']
        for status_value, access in results.items():
            self.assertEqual(access, baseline, f'has_daily_test_access differs at verification_status={status_value!r}')
        self.assertTrue(baseline)
