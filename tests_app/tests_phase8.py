"""Phase 8 — Historical Integrity, Question Versioning & Result Snapshot
Architecture: regression suite.

Covers the mandatory historical-integrity test matrix: question/option/
explanation edits after finalization, correct-answer changes, option
replacement, question deletion protection, Test-configuration changes,
question-order changes, import replacement, auto-submitted attempts,
backward-compatible fallback for pre-Phase-8 attempts, and concurrent
finalization. See docs/QUESTION_VERSIONING_DESIGN.md for the design these
tests hold to account.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase, APITransactionTestCase

from academics.models import Chapter, Option, Question, Subject, Topic
from tests_app.lifecycle import finalize_attempt
from tests_app.models import Answer, AttemptQuestionSnapshot, Test, TestAttempt, TestQuestion
from tests_app.tests_phase6 import _mkexam

User = get_user_model()


def _mkquestion(subject, text='Q?', marks=2, negative_marks=1, **overrides):
    fields = {'subject': subject, 'text': text, 'marks': marks, 'negative_marks': negative_marks}
    fields.update(overrides)
    return Question.objects.create(**fields)


def _mkoptions(question, correct_text='Right', wrong_texts=('Wrong A', 'Wrong B')):
    correct = Option.objects.create(question=question, text=correct_text, order=0, is_correct=True)
    wrongs = [
        Option.objects.create(question=question, text=t, order=i + 1, is_correct=False)
        for i, t in enumerate(wrong_texts)
    ]
    return correct, wrongs


def _finalize_with_answer(student, test, question, selected_option, is_correct):
    attempt = TestAttempt.objects.create(user=student, test=test)
    Answer.objects.create(attempt=attempt, question=question, selected_option=selected_option, is_correct=is_correct)
    return finalize_attempt(attempt, auto_submitted=False)


class QuestionEditHistoricalIntegrityTests(APITestCase):
    """Question Edit Test + Correct Answer Change Test + Explanation
    Change Test from the mandatory matrix."""

    def setUp(self):
        self.student = User.objects.create_user(username='qe_student', email='qe_student@example.com', password='pw')
        self.subject = Subject.objects.create(name='QE Subject')
        self.client.force_authenticate(user=self.student)

    def _built_test_and_question(self, text='Original text', explanation='Original explanation'):
        question = _mkquestion(self.subject, text=text, explanation=explanation)
        correct, wrongs = _mkoptions(question)
        test = _mkexam(solutions_visibility='auto', allow=self.student)
        TestQuestion.objects.create(test=test, question=question)
        return test, question, correct, wrongs

    def test_question_text_edit_after_finalize_does_not_change_historical_result(self):
        test, question, correct, _ = self._built_test_and_question()
        attempt = _finalize_with_answer(self.student, test, question, correct, True)
        old_score = attempt.score

        question.text = 'EDITED text — should never appear in the old result'
        question.save(update_fields=['text'])

        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(float(resp.data['score']), float(old_score))
        self.assertEqual(resp.data['questions'][0]['text'], 'Original text')
        self.assertNotIn('EDITED', resp.data['questions'][0]['text'])

    def test_new_attempt_after_edit_sees_the_new_content(self):
        test, question, correct, _ = self._built_test_and_question()
        _finalize_with_answer(self.student, test, question, correct, True)

        question.text = 'New content for future attempts'
        question.save(update_fields=['text'])

        new_attempt = TestAttempt.objects.create(user=self.student, test=test)
        resp = self.client.get(f'/api/attempts/{new_attempt.id}/')
        self.assertEqual(resp.data['questions'][0]['text'], 'New content for future attempts')

    def test_correct_answer_change_after_finalize_does_not_alter_historical_grading_or_display(self):
        test, question, correct, wrongs = self._built_test_and_question()
        attempt = _finalize_with_answer(self.student, test, question, correct, True)
        old_score = attempt.score
        original_correct_id = correct.id

        # Change the correct answer to a different option.
        correct.is_correct = False
        correct.save(update_fields=['is_correct'])
        wrongs[0].is_correct = True
        wrongs[0].save(update_fields=['is_correct'])

        attempt.refresh_from_db()
        self.assertEqual(float(attempt.score), float(old_score))  # grading untouched

        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')
        opts = {o['id']: o for o in resp.data['questions'][0]['options']}
        self.assertTrue(opts[original_correct_id]['is_correct'])  # still shows the ORIGINAL correct option
        self.assertFalse(opts[wrongs[0].id]['is_correct'])
        self.assertTrue(resp.data['questions'][0]['is_correct'])  # student's own correctness judgment unchanged

    def test_new_attempt_after_correct_answer_change_uses_the_new_correct_answer(self):
        test, question, correct, wrongs = self._built_test_and_question()
        _finalize_with_answer(self.student, test, question, correct, True)

        correct.is_correct = False
        correct.save(update_fields=['is_correct'])
        wrongs[0].is_correct = True
        wrongs[0].save(update_fields=['is_correct'])

        new_attempt = _finalize_with_answer(self.student, test, question, wrongs[0], True)
        self.assertTrue(new_attempt.score > 0)  # scored correct under the NEW answer key
        resp = self.client.get(f'/api/attempts/{new_attempt.id}/result/')
        opts = {o['id']: o for o in resp.data['questions'][0]['options']}
        self.assertTrue(opts[wrongs[0].id]['is_correct'])

    def test_explanation_change_after_finalize_shows_historical_explanation(self):
        test, question, correct, _ = self._built_test_and_question(explanation='Original explanation text')
        attempt = _finalize_with_answer(self.student, test, question, correct, True)

        question.explanation = 'EDITED explanation'
        question.save(update_fields=['explanation'])

        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')
        self.assertEqual(resp.data['questions'][0]['explanation'], 'Original explanation text')

    def test_new_attempt_sees_new_explanation(self):
        test, question, correct, _ = self._built_test_and_question(explanation='Original')
        _finalize_with_answer(self.student, test, question, correct, True)
        question.explanation = 'Updated explanation'
        question.save(update_fields=['explanation'])

        new_attempt = _finalize_with_answer(self.student, test, question, correct, True)
        resp = self.client.get(f'/api/attempts/{new_attempt.id}/result/')
        self.assertEqual(resp.data['questions'][0]['explanation'], 'Updated explanation')


class OptionReplacementIntegrityTests(APITestCase):
    """Option Replacement Test — the exact QuestionAdminSerializer.update()
    scenario (delete every Option, recreate fresh rows)."""

    def setUp(self):
        self.student = User.objects.create_user(username='or_student', email='or_student@example.com', password='pw')
        self.subject = Subject.objects.create(name='OR Subject')
        self.client.force_authenticate(user=self.student)

    def test_selected_and_correct_option_survive_full_option_table_replacement(self):
        question = _mkquestion(self.subject)
        correct, wrongs = _mkoptions(question, correct_text='B (correct)')
        test = _mkexam(solutions_visibility='auto', allow=self.student)
        TestQuestion.objects.create(test=test, question=question)
        attempt = _finalize_with_answer(self.student, test, question, wrongs[0], False)

        # Exactly what QuestionAdminSerializer.update() does: hard delete + recreate.
        question.options.all().delete()
        Option.objects.create(question=question, text='All-new option 1', order=0, is_correct=True)
        Option.objects.create(question=question, text='All-new option 2', order=1, is_correct=False)

        # The live FK is nulled (the exact pre-Phase-8 bug) ...
        answer = Answer.objects.get(attempt=attempt, question=question)
        self.assertIsNone(answer.selected_option_id)

        # ... but the historical review is completely unaffected.
        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')
        q = resp.data['questions'][0]
        self.assertEqual(q['selected_option_id'], wrongs[0].id)
        option_texts = {o['id']: o['text'] for o in q['options']}
        self.assertEqual(option_texts[wrongs[0].id], wrongs[0].text)
        self.assertEqual(option_texts[correct.id], 'B (correct)')
        self.assertTrue(next(o for o in q['options'] if o['id'] == correct.id)['is_correct'])
        # The brand-new options never existed at the time of this attempt.
        self.assertEqual(len(q['options']), 3)


class QuestionDeletionProtectionTests(APITestCase):
    """Question Delete Test — deletion of a historically-referenced
    question is already blocked (pre-existing protection, re-confirmed
    here); the snapshot's own SET_NULL is a defensive backstop, tested
    directly at the model level since the guarded API path can't exercise
    it (by design)."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='qd_staff', email='qd_staff@example.com', password='pw', is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(username='qd_student', email='qd_student@example.com', password='pw')
        self.subject = Subject.objects.create(name='QD Subject')

    def test_question_with_historical_attempt_cannot_be_deleted_via_api(self):
        question = _mkquestion(self.subject)
        correct, _ = _mkoptions(question)
        test = _mkexam(solutions_visibility='auto', allow=self.student)
        TestQuestion.objects.create(test=test, question=question)
        _finalize_with_answer(self.student, test, question, correct, True)

        self.client.force_authenticate(user=self.staff)
        resp = self.client.delete(f'/api/questions/{question.id}/')
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(Question.objects.filter(pk=question.id).exists())

    def test_snapshot_survives_even_if_the_live_question_row_is_gone(self):
        """Direct model-level proof of the SET_NULL defensive design —
        simulates a Question row disappearing by a path other than the
        guarded API (e.g. a future/alternate deletion route, or manual
        DB intervention) and confirms the snapshot's own content is
        completely unaffected."""
        question = _mkquestion(self.subject, text='Will be force-deleted')
        correct, _ = _mkoptions(question)
        test = _mkexam(solutions_visibility='auto', allow=self.student)
        TestQuestion.objects.create(test=test, question=question)
        attempt = _finalize_with_answer(self.student, test, question, correct, True)

        snapshot = AttemptQuestionSnapshot.objects.get(attempt=attempt)
        self.assertEqual(snapshot.text, 'Will be force-deleted')

        # Force through the ORM directly, bypassing the guarded view —
        # proves the snapshot doesn't depend on the live row surviving.
        Answer.objects.filter(question=question).delete()  # Answer is CASCADE from Question
        TestQuestion.objects.filter(question=question).delete()
        question.delete()

        snapshot.refresh_from_db()
        self.assertIsNone(snapshot.question_id)
        self.assertEqual(snapshot.text, 'Will be force-deleted')
        self.assertEqual(len(snapshot.options_snapshot), 3)

        self.client.force_authenticate(user=self.student)
        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['questions'][0]['text'], 'Will be force-deleted')


class TestConfigurationHistoricalIntegrityTests(APITestCase):
    """Test Configuration Change Test — editing Test-level fields after
    finalization must never alter the already-stored score."""

    def setUp(self):
        self.student = User.objects.create_user(username='tc_student', email='tc_student@example.com', password='pw')
        self.subject = Subject.objects.create(name='TC Subject')
        self.client.force_authenticate(user=self.student)

    def test_negative_marking_and_marks_change_does_not_alter_finalized_score(self):
        question = _mkquestion(self.subject, marks=5, negative_marks=2)
        correct, wrongs = _mkoptions(question)
        test = _mkexam(solutions_visibility='auto', negative_marking=True, allow=self.student)
        TestQuestion.objects.create(test=test, question=question)
        attempt = _finalize_with_answer(self.student, test, question, wrongs[0], False)  # wrong -> -2
        old_score = attempt.score
        self.assertEqual(float(old_score), -2.0)

        test.negative_marking = False
        test.save(update_fields=['negative_marking'])
        question.marks = 100
        question.negative_marks = 50
        question.save(update_fields=['marks', 'negative_marks'])

        attempt.refresh_from_db()
        self.assertEqual(float(attempt.score), float(old_score))


class QuestionOrderHistoricalIntegrityTests(APITestCase):
    """Question Order Test — TestAdminSerializer's question-list replace
    (delete+recreate every TestQuestion) must not change an old attempt's
    reconstructable order."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='qo_staff', email='qo_staff@example.com', password='pw', is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(username='qo_student', email='qo_student@example.com', password='pw')
        self.subject = Subject.objects.create(name='QO Subject')
        self.client.force_authenticate(user=self.student)

    def test_reordering_test_questions_does_not_change_old_attempts_snapshot_order(self):
        q1 = _mkquestion(self.subject, text='Q1')
        q2 = _mkquestion(self.subject, text='Q2')
        c1, _ = _mkoptions(q1)
        c2, _ = _mkoptions(q2)
        test = _mkexam(solutions_visibility='auto', allow=self.student)
        TestQuestion.objects.create(test=test, question=q1, order=0)
        TestQuestion.objects.create(test=test, question=q2, order=1)

        attempt = TestAttempt.objects.create(user=self.student, test=test)
        Answer.objects.create(attempt=attempt, question=q1, selected_option=c1, is_correct=True)
        Answer.objects.create(attempt=attempt, question=q2, selected_option=c2, is_correct=True)
        finalize_attempt(attempt, auto_submitted=False)

        old_order = list(AttemptQuestionSnapshot.objects.filter(attempt=attempt).order_by('order').values_list('question_id', flat=True))
        self.assertEqual(old_order, [q1.id, q2.id])

        # Admin reorders the live Test (q2 first) via the exact TestAdminSerializer path.
        self.client.force_authenticate(user=self.staff)
        resp = self.client.patch(f'/api/tests/{test.id}/', {'question_ids': [q2.id, q1.id]}, format='json')
        self.assertEqual(resp.status_code, 200)

        # Old attempt's snapshot order is untouched.
        unchanged_order = list(AttemptQuestionSnapshot.objects.filter(attempt=attempt).order_by('order').values_list('question_id', flat=True))
        self.assertEqual(unchanged_order, [q1.id, q2.id])

        self.client.force_authenticate(user=self.student)
        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')
        self.assertEqual([q['id'] for q in resp.data['questions']], [q1.id, q2.id])


class ImportReplacementHistoricalIntegrityTests(APITestCase):
    """Import Replacement Test — 'replace' on a question already
    referenced by a historical attempt must not delete it (pre-existing
    protection, re-confirmed here) — a new question is created instead,
    and the old attempt's snapshot (and the original question) are both
    completely unaffected."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='ir_staff', email='ir_staff@example.com', password='pw', is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(username='ir_student', email='ir_student@example.com', password='pw')
        self.subject = Subject.objects.create(name='IR Subject')
        self.chapter = Chapter.objects.create(subject=self.subject, name='IR Chapter')
        self.topic = Topic.objects.create(chapter=self.chapter, name='IR Topic')

    def test_replace_on_a_referenced_question_creates_new_instead_of_deleting_old(self):
        from academics.import_engine import question_is_referenced

        old_question = _mkquestion(self.subject, text='Original imported question')
        correct, _ = _mkoptions(old_question)
        test = _mkexam(solutions_visibility='auto', allow=self.student)
        TestQuestion.objects.create(test=test, question=old_question)
        attempt = _finalize_with_answer(self.student, test, old_question, correct, True)

        self.assertTrue(question_is_referenced(old_question))

        # Exact "replace" guard from _create_questions_for_test:
        from academics.import_engine import create_question_from_row

        if not question_is_referenced(old_question):
            old_question.delete()
        else:
            new_question = create_question_from_row(
                {'text_html': '<p>Replacement text</p>', 'options': [
                    {'text_html': 'A', 'is_correct': True}, {'text_html': 'B', 'is_correct': False},
                ], 'explanation_html': '', 'explanation_video_url': '', 'remarks': '', 'past_exam_years': '', 'references': []},
                type('B', (), {'subject': self.subject, 'chapter': self.chapter, 'topic': self.topic})(),
                [],
            )

        self.assertTrue(Question.objects.filter(pk=old_question.id).exists())  # never deleted
        self.assertNotEqual(new_question.id, old_question.id)

        # Old attempt's historical review is completely unaffected.
        self.client.force_authenticate(user=self.student)
        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')
        self.assertEqual(resp.data['questions'][0]['text'], 'Original imported question')


class AutoSubmittedHistoricalIntegrityTests(APITestCase):
    """Auto-Submit Test — an auto-finalized attempt gets snapshots exactly
    like a manually-submitted one, and stays historically correct after a
    later question edit."""

    def setUp(self):
        self.student = User.objects.create_user(username='as_student', email='as_student@example.com', password='pw')
        self.subject = Subject.objects.create(name='AS Subject')
        self.client.force_authenticate(user=self.student)

    def test_auto_submitted_attempt_gets_a_snapshot_and_stays_correct_after_edit(self):
        from tests_app.tests_phase6 import _backdate_start

        question = _mkquestion(self.subject, text='Auto-submit content')
        correct, _ = _mkoptions(question)
        test = _mkexam(solutions_visibility='auto', duration_minutes=30, allow=self.student)
        TestQuestion.objects.create(test=test, question=question)
        attempt = TestAttempt.objects.create(user=self.student, test=test)
        Answer.objects.create(attempt=attempt, question=question, selected_option=correct, is_correct=True)
        attempt = _backdate_start(attempt, timezone.now() - timezone.timedelta(hours=1))

        # A manual /submit/ call is accepted even past the deadline (Phase
        # 6: auto_submitted stays False for a real Submit click) — to
        # actually exercise auto-submission, touch the attempt via a
        # lazy-finalization entry point instead, exactly as a student
        # revisiting an expired attempt would.
        resp = self.client.get(f'/api/attempts/{attempt.id}/')
        self.assertEqual(resp.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'submitted')
        self.assertTrue(attempt.auto_submitted)
        self.assertTrue(AttemptQuestionSnapshot.objects.filter(attempt=attempt).exists())

        question.text = 'Edited after auto-submit'
        question.save(update_fields=['text'])

        result = self.client.get(f'/api/attempts/{attempt.id}/result/')
        self.assertEqual(result.data['questions'][0]['text'], 'Auto-submit content')
        self.assertTrue(result.data['auto_submitted'])


class BackwardCompatibilityFallbackTests(APITestCase):
    """Backward Migration Test — an attempt with no snapshot (simulating a
    pre-Phase-8 record, since finalize_attempt() is never called here)
    must still be fully readable via the original live-read path."""

    def setUp(self):
        self.student = User.objects.create_user(username='bc_student', email='bc_student@example.com', password='pw')
        self.subject = Subject.objects.create(name='BC Subject')
        self.client.force_authenticate(user=self.student)

    def test_pre_phase8_style_attempt_with_no_snapshot_falls_back_to_live_read(self):
        question = _mkquestion(self.subject, text='Legacy pre-snapshot question')
        correct, _ = _mkoptions(question)
        test = _mkexam(solutions_visibility='auto', allow=self.student)
        TestQuestion.objects.create(test=test, question=question)

        # A "historical" attempt created directly, bypassing finalize_attempt()
        # entirely — exactly what every attempt looked like before this phase.
        attempt = TestAttempt.objects.create(
            user=self.student, test=test, status='submitted', score=2, rank=1, percentile=100, end_time=timezone.now(),
        )
        Answer.objects.create(attempt=attempt, question=question, selected_option=correct, is_correct=True)

        self.assertFalse(AttemptQuestionSnapshot.objects.filter(attempt=attempt).exists())

        resp = self.client.get(f'/api/attempts/{attempt.id}/result/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['questions'][0]['text'], 'Legacy pre-snapshot question')
        self.assertEqual(resp.data['questions'][0]['selected_option_id'], correct.id)

    def test_makemigrations_check_is_clean(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        try:
            call_command('makemigrations', '--check', '--dry-run', stdout=out, stderr=out)
            clean = True
        except SystemExit:
            clean = False
        self.assertTrue(clean, out.getvalue())


class ConcurrentFinalizationSnapshotTests(APITransactionTestCase):
    """Concurrency Test — two overlapping finalize_attempt() calls on the
    same attempt must create exactly one set of snapshots, never two (the
    same idempotency guard Phase 6 already relies on for scoring)."""

    def test_concurrent_finalize_creates_exactly_one_snapshot_set(self):
        import threading
        import time

        from django.db import connection

        student = User.objects.create_user(username='cfs_student', email='cfs_student@example.com', password='pw')
        subject = Subject.objects.create(name='Concurrent Snapshot Subject')
        question = _mkquestion(subject, marks=1, negative_marks=0)
        correct, _ = _mkoptions(question)
        test = _mkexam(duration_minutes=30)
        TestQuestion.objects.create(test=test, question=question)
        attempt = TestAttempt.objects.create(user=student, test=test)
        Answer.objects.create(attempt=attempt, question=question, selected_option=correct, is_correct=True)

        def finalize_once():
            for attempt_no in range(20):
                try:
                    if connection.vendor == 'sqlite':
                        with connection.cursor() as cur:
                            cur.execute('PRAGMA busy_timeout = 30000')
                    a = TestAttempt.objects.get(pk=attempt.pk)
                    finalize_attempt(a, auto_submitted=True)
                    return
                except Exception as exc:  # noqa: BLE001 — SQLite lock-contention retry, matches existing precedent
                    if 'locked' in str(exc).lower() and attempt_no < 19:
                        time.sleep(0.05)
                        continue
                    raise
                finally:
                    connection.close()

        threads = [threading.Thread(target=finalize_once) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(AttemptQuestionSnapshot.objects.filter(attempt=attempt).count(), 1)
