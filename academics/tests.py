import io

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from docx import Document as DocxDocument
from rest_framework import status
from rest_framework.test import APITestCase

from academics.import_dedup import existing_texts_for_subject, find_duplicate, normalize_option_set, normalize_text
from academics.importers.docx_parser import parse_docx
from academics.models import Chapter, ImportBatch, ImportRow, Option, Question, QuestionAttempt, Subject, Topic
from core.models import DeletionAuditLog
from tests_app.models import Test, TestAttempt, TestQuestion

User = get_user_model()

TINY_GIF = (
    b'GIF87a\x01\x00\x01\x00\x80\x01\x00\x00\x00\x00ccc,\x00'
    b'\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'
)


class QuestionDeleteTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='staff1', email='staff1@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(username='student1', email='student1@example.com', password='pw12345')
        self.subject = Subject.objects.create(name='Physics')
        self.client.force_authenticate(user=self.staff)

    def _make_question(self, with_image=False):
        image = SimpleUploadedFile('q.gif', TINY_GIF, content_type='image/gif') if with_image else None
        question = Question.objects.create(subject=self.subject, text='What is g?', image=image)
        Option.objects.create(question=question, text='9.8', is_correct=True)
        Option.objects.create(question=question, text='10')
        return question

    def test_delete_requires_staff(self):
        question = self._make_question()
        self.client.force_authenticate(user=self.student)
        resp = self.client.delete(f'/api/questions/{question.id}/')
        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
        self.assertTrue(Question.objects.filter(id=question.id).exists())

    def test_blocked_when_question_has_practice_attempt_history(self):
        question = self._make_question()
        option = question.options.first()
        question.attempts.create(user=self.student, selected_option=option, is_correct=True)

        resp = self.client.delete(f'/api/questions/{question.id}/')

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(Question.objects.filter(id=question.id).exists())
        entry = DeletionAuditLog.objects.get(resource_type='Question', resource_id=str(question.id))
        self.assertEqual(entry.result, 'failure')

    def test_blocked_when_used_in_exam_with_student_attempts(self):
        question = self._make_question()
        test = Test.objects.create(title='Mock Test 1', exam_type='mock')
        TestQuestion.objects.create(test=test, question=question)
        TestAttempt.objects.create(user=self.student, test=test, status='submitted')

        resp = self.client.delete(f'/api/questions/{question.id}/')

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Mock Test 1', resp.data['detail'])
        self.assertTrue(Question.objects.filter(id=question.id).exists())

    def test_permanent_delete_succeeds_and_removes_options_and_images(self):
        question = self._make_question(with_image=True)
        option_ids = list(question.options.values_list('id', flat=True))
        image_name = question.image.name

        resp = self.client.delete(f'/api/questions/{question.id}/')

        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Question.objects.filter(id=question.id).exists())
        self.assertFalse(Option.objects.filter(id__in=option_ids).exists())
        self.assertFalse(default_storage_exists(image_name))
        entry = DeletionAuditLog.objects.get(resource_type='Question')
        self.assertEqual(entry.result, 'success')
        self.assertEqual(entry.actor, self.staff)

    def test_delete_of_untouched_question_is_not_blocked_by_unrelated_test(self):
        """A question that's merely attached to a Test with no attempts yet
        must still be deletable — only *attempted* usage should block it."""
        question = self._make_question()
        test = Test.objects.create(title='Draft Mock', exam_type='mock')
        TestQuestion.objects.create(test=test, question=question)

        resp = self.client.delete(f'/api/questions/{question.id}/')

        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Question.objects.filter(id=question.id).exists())


def default_storage_exists(name):
    from django.core.files.storage import default_storage
    return default_storage.exists(name)


class ImportRowDeleteTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='staff1', email='staff1@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.client.force_authenticate(user=self.staff)
        self.batch = ImportBatch.objects.create(
            uploaded_by=self.staff, file_name='questions.xlsx', file_format='xlsx', status='ready', total_rows=2,
        )
        self.bad_row = ImportRow.objects.create(
            batch=self.batch, row_number=1, status='error',
            raw_data={'text_html': '<p>Q1</p>', 'options': [{'text_html': 'A', 'is_correct': True}]},
            errors=['Only 1 option(s) found — at least 2 are required.'],
        )
        self.good_row = ImportRow.objects.create(
            batch=self.batch, row_number=2, status='valid',
            raw_data={
                'text_html': '<p>Q2</p>',
                'options': [{'text_html': 'A', 'is_correct': True}, {'text_html': 'B', 'is_correct': False}],
            },
        )

    def test_delete_removes_row_and_decrements_total(self):
        resp = self.client.delete(f'/api/import-batches/{self.batch.id}/rows/{self.bad_row.id}/')

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ImportRow.objects.filter(id=self.bad_row.id).exists())
        self.assertEqual(resp.data['total_rows'], 1)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.total_rows, 1)

    def test_delete_of_unknown_row_returns_404(self):
        resp = self.client.delete(f'/api/import-batches/{self.batch.id}/rows/999999/')
        self.assertEqual(resp.status_code, 404)

    def test_delete_blocked_once_import_has_started(self):
        self.batch.status = 'importing'
        self.batch.save(update_fields=['status'])

        resp = self.client.delete(f'/api/import-batches/{self.batch.id}/rows/{self.good_row.id}/')

        self.assertEqual(resp.status_code, 400)
        self.assertTrue(ImportRow.objects.filter(id=self.good_row.id).exists())

    def test_delete_requires_staff(self):
        student = User.objects.create_user(username='student1', email='student1@example.com', password='pw12345')
        self.client.force_authenticate(user=student)

        resp = self.client.delete(f'/api/import-batches/{self.batch.id}/rows/{self.good_row.id}/')

        self.assertIn(resp.status_code, (401, 403))
        self.assertTrue(ImportRow.objects.filter(id=self.good_row.id).exists())

    def test_patch_can_fix_an_error_row_to_valid(self):
        """Regression coverage for the Preview & Validate editability fix:
        correcting the underlying data (adding a 2nd option) must flip the
        row's status from error to valid via re-validation, same as the
        Admin's new inline editor relies on."""
        fixed_data = {
            'text_html': '<p>Q1</p>',
            'options': [{'text_html': 'A', 'is_correct': True}, {'text_html': 'B', 'is_correct': False}],
            'explanation_html': '<p>Because A is right.</p>',
        }
        resp = self.client.patch(f'/api/import-batches/{self.batch.id}/rows/{self.bad_row.id}/', {'data': fixed_data}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'valid')
        self.assertEqual(resp.data['errors'], [])


class ImportBatchCreateTestModeMismatchTests(APITestCase):
    """Covers the bug where a batch could reach 'ready' with import_mode
    recorded as 'question_bank' despite the admin actually walking through
    the full Import & Create Test wizard (Test Configuration, Distribution
    Preview) — caused by the "Import Type" selector staying interactive
    while the file was still uploading. The fix: a 'ready' batch (nothing
    written yet) is safe to create-test on regardless of import_mode."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='staff1', email='staff1@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.client.force_authenticate(user=self.staff)
        self.subject = Subject.objects.create(name='Physics')
        self.chapter = Chapter.objects.create(subject=self.subject, name='Mechanics')
        self.topic = Topic.objects.create(chapter=self.chapter, name='Kinematics')
        self.batch = ImportBatch.objects.create(
            uploaded_by=self.staff, file_name='q.xlsx', file_format='xlsx',
            status='ready', total_rows=1, import_mode='question_bank',
            subject=self.subject, chapter=self.chapter, topic=self.topic,
        )
        ImportRow.objects.create(
            batch=self.batch, row_number=1, status='valid',
            raw_data={
                'text_html': '<p>Q1</p>',
                'options': [{'text_html': 'A', 'is_correct': True}, {'text_html': 'B', 'is_correct': False}],
                'explanation_html': '<p>Because.</p>',
            },
        )

    def test_create_test_succeeds_on_ready_batch_despite_mismatched_import_mode(self):
        resp = self.client.post(
            f'/api/import-batches/{self.batch.id}/create-test/',
            {'title': 'Mock Test 1', 'exam_type': 'mock', 'duration_minutes': 30},
            format='json',
        )

        self.assertEqual(resp.status_code, 200)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, 'completed')
        self.assertEqual(self.batch.import_mode, 'create_test')
        self.assertIsNotNone(self.batch.created_test_id)

    def test_create_test_still_blocked_on_a_failed_question_bank_batch(self):
        """A 'failed' batch is only safe to retry here if it failed inside
        this same synchronous flow (a real create_test batch) — a
        'question_bank' batch that failed via the separate background
        /confirm/ run may have already committed some rows, so it must
        stay blocked rather than risk reprocessing them into duplicates."""
        self.batch.status = 'failed'
        self.batch.save(update_fields=['status'])

        resp = self.client.post(
            f'/api/import-batches/{self.batch.id}/create-test/',
            {'title': 'Mock Test 1', 'exam_type': 'mock', 'duration_minutes': 30},
            format='json',
        )

        self.assertEqual(resp.status_code, 400)


def _build_docx(lines):
    """Builds a minimal in-memory .docx from plain-text lines, mirroring
    docx_parser.build_template_docx()'s pattern."""
    doc = DocxDocument()
    for line in lines:
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


class DocxParserExplanationTests(TestCase):
    """Regression coverage for a real reported bug: a rich, AI-style
    explanation (Correct Answer / Core Concept / ... / Option Analysis /
    Common Exam Trap / Review Point) was being shredded on import, because
    the parser treated ANY line starting with "digit." or "letter)" as a
    new question/option — even decimal values and a per-option breakdown
    that legitimately appear inside the explanation itself. Confirmed via
    the actual "Volumetric Analysis.docx" file: 70 phantom rows and 8
    "options" per question instead of 66 real questions with 4 each."""

    def test_decimal_value_inside_explanation_does_not_start_a_new_question(self):
        buf = _build_docx([
            'Q1. What is the normality of a 1 M solution of H3PO4?',
            'A) 0.5 N', 'B) 0.1 N', 'C) 2.0 N', 'D) 3.0 N',
            'Answer: D',
            'Explanation:',
            'Correct Answer: D) 3.0 N',
            '0.5 N -- would require n = 0.5, which has no chemical basis.',
            '2.0 N -- this matches a diprotic acid, not H3PO4.',
            'Review Point: Normality = Molarity x Basicity.',
        ])

        questions = parse_docx(buf)

        self.assertEqual(len(questions), 1)
        self.assertEqual(len(questions[0]['options']), 4)
        self.assertIn('0.5 N', questions[0]['explanation_html'])
        self.assertIn('Review Point', questions[0]['explanation_html'])

    def test_lettered_option_analysis_inside_explanation_is_not_read_as_new_options(self):
        buf = _build_docx([
            'Q1. Molecular weight of a tribasic acid is W. Its equivalent weight is:',
            'A) W/2', 'B) W/3', 'C) W', 'D) 3W',
            'Answer: B',
            'Explanation:',
            'Correct Answer: B) W/3',
            'Option Analysis:',
            'A) W/2 -- corresponds to a dibasic acid.',
            'B) W/3 -- correctly divides by basicity.',
            'C) W -- would mean the acid is monobasic.',
            'D) 3W -- incorrectly multiplies instead of dividing.',
            'Review Point: Equivalent weight = Molecular weight / Basicity.',
        ])

        questions = parse_docx(buf)

        self.assertEqual(len(questions), 1)
        question = questions[0]
        self.assertEqual(len(question['options']), 4)
        correct = [i for i, o in enumerate(question['options']) if o['is_correct']]
        self.assertEqual(correct, [1])  # B
        self.assertIn('Option Analysis', question['explanation_html'])
        self.assertIn('Review Point', question['explanation_html'])

    def test_explicit_q_prefix_still_starts_a_new_question_even_mid_explanation(self):
        buf = _build_docx([
            'Q1. First question?',
            'A) 1', 'B) 2', 'C) 3', 'D) 4',
            'Answer: A',
            'Explanation:',
            'Some explanation text without a proper close.',
            'Q2. Second question?',
            'A) 5', 'B) 6', 'C) 7', 'D) 8',
            'Answer: B',
        ])

        questions = parse_docx(buf)

        self.assertEqual(len(questions), 2)
        self.assertEqual(len(questions[1]['options']), 4)


def _pq(text, option_texts):
    return {'text_html': text, 'options': [{'text_html': t} for t in option_texts]}


def _batch_entry(pq):
    return {'text': normalize_text(pq['text_html']), 'options': normalize_option_set(o['text_html'] for o in pq['options'])}


class ImportDedupTests(TestCase):
    """Regression coverage for a real reported false-positive: two questions
    sharing a common template stem ("Normality of X M solution of Y is?")
    but different specifics and different options were being flagged as
    duplicates on stem similarity alone. Duplicate detection must require
    both the stem AND the option set to substantially match."""

    def test_similar_stem_with_different_options_is_not_flagged(self):
        candidate = _pq('Normality of 1 M solution of phosphoric acid is', ['0.5 N', '0.1 N', '2.0 N', '3.0 N'])
        other = _pq('Normality of 1 M solution of sulphuric acid is', ['1 N', '2 N', 'N/2', 'N/4'])
        batch = {1: _batch_entry(other)}

        dup_id, score = find_duplicate(candidate, {}, batch, self_index=0)

        self.assertIsNone(dup_id)
        self.assertEqual(score, 0.0)

    def test_identical_stem_and_options_is_flagged(self):
        candidate = _pq('Normality of 1 M solution of sulphuric acid is', ['1 N', '2 N', 'N/2', 'N/4'])
        other = _pq('Normality of 1 M solution of sulphuric acid is', ['1 N', '2 N', 'N/2', 'N/4'])
        batch = {1: _batch_entry(other)}

        dup_id, score = find_duplicate(candidate, {}, batch, self_index=0)

        self.assertEqual(dup_id, 'row:1')
        self.assertGreaterEqual(score, 0.85)

    def test_matches_against_existing_db_question_require_both_dimensions(self):
        subject = Subject.objects.create(name='Chemistry')
        existing = Question.objects.create(subject=subject, text='Normality of 1 M solution of sulphuric acid is')
        Option.objects.create(question=existing, text='1 N')
        Option.objects.create(question=existing, text='2 N')
        Option.objects.create(question=existing, text='N/2')
        Option.objects.create(question=existing, text='N/4')

        existing_map = existing_texts_for_subject(subject)

        same_options = _pq('Normality of 1 M solution of sulphuric acid is', ['1 N', '2 N', 'N/2', 'N/4'])
        dup_id, score = find_duplicate(same_options, existing_map, self_index=None)
        self.assertEqual(dup_id, existing.id)
        self.assertGreaterEqual(score, 0.85)

        different_options = _pq('Normality of 1 M solution of phosphoric acid is', ['0.5 N', '0.1 N', '2.0 N', '3.0 N'])
        dup_id, score = find_duplicate(different_options, existing_map, self_index=None)
        self.assertIsNone(dup_id)


class QuestionBookmarkFilterTests(APITestCase):
    """Powers the QBank 'Bookmarks' page — ?bookmarked=true must return only
    this user's own bookmarked questions, never another student's."""

    def setUp(self):
        self.student = User.objects.create_user(username='student1', email='student1@example.com', password='pw12345')
        self.other = User.objects.create_user(username='other1', email='other1@example.com', password='pw12345')
        self.subject = Subject.objects.create(name='Physics')
        self.bookmarked_q = Question.objects.create(subject=self.subject, text='Bookmarked question')
        self.plain_q = Question.objects.create(subject=self.subject, text='Not bookmarked')
        QuestionAttempt.objects.create(user=self.student, question=self.bookmarked_q, is_bookmarked=True)
        QuestionAttempt.objects.create(user=self.student, question=self.plain_q, is_bookmarked=False)
        # Another student bookmarking the same question must not leak into self.student's list.
        QuestionAttempt.objects.create(user=self.other, question=self.plain_q, is_bookmarked=True)

    def test_bookmarked_filter_returns_only_own_bookmarks(self):
        self.client.force_authenticate(user=self.student)
        resp = self.client.get('/api/questions/?bookmarked=true')
        self.assertEqual(resp.status_code, 200)
        ids = {q['id'] for q in resp.data}
        self.assertEqual(ids, {self.bookmarked_q.id})

    def test_bookmarked_filter_requires_auth(self):
        resp = self.client.get('/api/questions/?bookmarked=true')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, [])

    def test_without_filter_all_visible_questions_still_returned(self):
        self.client.force_authenticate(user=self.student)
        resp = self.client.get('/api/questions/')
        ids = {q['id'] for q in resp.data}
        self.assertEqual(ids, {self.bookmarked_q.id, self.plain_q.id})


class QuestionBookmarkToggleTests(APITestCase):
    """Regression coverage: bookmarking must never blank out a previously
    recorded answer (see the bookmark() action's docstring for the bug in
    answer() this deliberately avoids)."""

    def setUp(self):
        self.student = User.objects.create_user(username='student1', email='student1@example.com', password='pw12345')
        self.subject = Subject.objects.create(name='Physics')
        self.question = Question.objects.create(subject=self.subject, text='2+2=?')
        self.option = Option.objects.create(question=self.question, text='4', is_correct=True)
        self.client.force_authenticate(user=self.student)

    def test_bookmark_on_a_never_attempted_question_creates_attempt(self):
        resp = self.client.post(f'/api/questions/{self.question.id}/bookmark/', {'bookmark': True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['is_bookmarked'])
        attempt = QuestionAttempt.objects.get(user=self.student, question=self.question)
        self.assertTrue(attempt.is_bookmarked)
        self.assertIsNone(attempt.selected_option)

    def test_bookmarking_does_not_erase_a_previously_recorded_answer(self):
        self.client.post(f'/api/questions/{self.question.id}/answer/', {'option_id': self.option.id})
        self.client.post(f'/api/questions/{self.question.id}/bookmark/', {'bookmark': True})

        attempt = QuestionAttempt.objects.get(user=self.student, question=self.question)
        self.assertTrue(attempt.is_bookmarked)
        self.assertEqual(attempt.selected_option_id, self.option.id)
        self.assertTrue(attempt.is_correct)

    def test_unbookmark_clears_the_flag_only(self):
        self.client.post(f'/api/questions/{self.question.id}/answer/', {'option_id': self.option.id})
        self.client.post(f'/api/questions/{self.question.id}/bookmark/', {'bookmark': True})
        resp = self.client.post(f'/api/questions/{self.question.id}/bookmark/', {'bookmark': False})

        self.assertFalse(resp.data['is_bookmarked'])
        attempt = QuestionAttempt.objects.get(user=self.student, question=self.question)
        self.assertFalse(attempt.is_bookmarked)
        self.assertEqual(attempt.selected_option_id, self.option.id)

    def test_is_bookmarked_reflects_in_the_question_list_response(self):
        other_question = Question.objects.create(subject=self.subject, text='3+3=?')
        self.client.post(f'/api/questions/{self.question.id}/bookmark/', {'bookmark': True})

        resp = self.client.get('/api/questions/')

        by_id = {q['id']: q['is_bookmarked'] for q in resp.data}
        self.assertTrue(by_id[self.question.id])


class RecordQuestionResultTests(TestCase):
    """academics.services.record_question_result — the single write path
    for the Smart Question Bank's performance tracking, used by both QBank
    practice and test submission."""

    def setUp(self):
        self.student = User.objects.create_user(username='student1', email='student1@example.com', password='pw12345')
        self.subject = Subject.objects.create(name='Physics')
        self.question = Question.objects.create(subject=self.subject, text='2+2=?')

    def test_first_correct_attempt_is_learning_not_mastered(self):
        from academics.services import record_question_result

        attempt = record_question_result(self.student, self.question, True, source='qbank')

        self.assertEqual(attempt.attempts_count, 1)
        self.assertEqual(attempt.correct_count, 1)
        self.assertEqual(attempt.incorrect_count, 0)
        self.assertTrue(attempt.is_correct)
        self.assertTrue(attempt.last_result)
        self.assertEqual(attempt.mastery_status, 'learning')
        self.assertIsNotNone(attempt.revision_due_at)

    def test_two_correct_attempts_become_mastered(self):
        from academics.services import record_question_result

        record_question_result(self.student, self.question, True, source='qbank')
        attempt = record_question_result(self.student, self.question, True, source='qbank')

        self.assertEqual(attempt.attempts_count, 2)
        self.assertEqual(attempt.mastery_status, 'mastered')

    def test_repeated_incorrect_attempts_become_weak(self):
        from academics.services import record_question_result

        record_question_result(self.student, self.question, False, source='qbank')
        record_question_result(self.student, self.question, False, source='qbank')
        attempt = record_question_result(self.student, self.question, False, source='qbank')

        self.assertEqual(attempt.attempts_count, 3)
        self.assertEqual(attempt.incorrect_count, 3)
        self.assertEqual(attempt.mastery_status, 'weak')
        self.assertFalse(attempt.last_result)

    def test_counts_accumulate_across_calls_instead_of_overwriting(self):
        from academics.services import record_question_result

        record_question_result(self.student, self.question, True, source='qbank')
        record_question_result(self.student, self.question, False, source='test')
        attempt = record_question_result(self.student, self.question, True, source='qbank')

        self.assertEqual(attempt.attempts_count, 3)
        self.assertEqual(attempt.correct_count, 2)
        self.assertEqual(attempt.incorrect_count, 1)

    def test_never_touches_bookmark(self):
        from academics.services import record_question_result

        QuestionAttempt.objects.create(user=self.student, question=self.question, is_bookmarked=True)
        record_question_result(self.student, self.question, False, source='qbank')

        attempt = QuestionAttempt.objects.get(user=self.student, question=self.question)
        self.assertTrue(attempt.is_bookmarked)

    def test_logs_an_immutable_question_event_each_call(self):
        from academics.models import QuestionEvent
        from academics.services import record_question_result

        record_question_result(self.student, self.question, True, source='qbank')
        record_question_result(self.student, self.question, False, source='test')

        events = list(QuestionEvent.objects.filter(user=self.student, question=self.question).order_by('id'))
        self.assertEqual(len(events), 2)
        self.assertEqual([e.source for e in events], ['qbank', 'test'])
        self.assertEqual([e.is_correct for e in events], [True, False])

    def test_does_not_create_duplicate_attempt_rows(self):
        from academics.services import record_question_result

        for _ in range(3):
            record_question_result(self.student, self.question, True, source='qbank')

        self.assertEqual(QuestionAttempt.objects.filter(user=self.student, question=self.question).count(), 1)


class QuestionAnswerRecordsPerformanceTests(APITestCase):
    """/questions/{id}/answer/ must feed the new performance-tracking
    counters, not just overwrite the latest-state fields, and must never
    touch is_bookmarked (see QuestionBookmarkToggleTests for that contract)."""

    def setUp(self):
        self.student = User.objects.create_user(username='student1', email='student1@example.com', password='pw12345')
        self.subject = Subject.objects.create(name='Physics')
        self.question = Question.objects.create(subject=self.subject, text='2+2=?')
        self.correct_option = Option.objects.create(question=self.question, text='4', is_correct=True)
        self.wrong_option = Option.objects.create(question=self.question, text='5', is_correct=False)
        self.client.force_authenticate(user=self.student)

    def test_answering_twice_accumulates_attempts_count(self):
        self.client.post(f'/api/questions/{self.question.id}/answer/', {'option_id': self.wrong_option.id})
        self.client.post(f'/api/questions/{self.question.id}/answer/', {'option_id': self.correct_option.id})

        attempt = QuestionAttempt.objects.get(user=self.student, question=self.question)
        self.assertEqual(attempt.attempts_count, 2)
        self.assertEqual(attempt.correct_count, 1)
        self.assertEqual(attempt.incorrect_count, 1)

    def test_answering_with_no_option_does_not_record_an_attempt(self):
        self.client.post(f'/api/questions/{self.question.id}/answer/', {})
        self.assertFalse(QuestionAttempt.objects.filter(user=self.student, question=self.question).exists())

    def test_answering_does_not_reset_an_existing_bookmark(self):
        self.client.post(f'/api/questions/{self.question.id}/bookmark/', {'bookmark': True})
        self.client.post(f'/api/questions/{self.question.id}/answer/', {'option_id': self.correct_option.id})

        attempt = QuestionAttempt.objects.get(user=self.student, question=self.question)
        self.assertTrue(attempt.is_bookmarked)


class QuestionDashboardEndpointTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='student1', email='student1@example.com', password='pw12345')
        self.subject = Subject.objects.create(name='Physics')
        self.q1 = Question.objects.create(subject=self.subject, text='Q1')
        self.q2 = Question.objects.create(subject=self.subject, text='Q2')
        self.q3 = Question.objects.create(subject=self.subject, text='Q3')  # never attempted
        self.client.force_authenticate(user=self.student)

    def test_dashboard_counts_reflect_real_attempts(self):
        from academics.services import record_question_result

        record_question_result(self.student, self.q1, True, source='qbank')
        record_question_result(self.student, self.q1, True, source='qbank')  # -> mastered
        record_question_result(self.student, self.q2, False, source='qbank')

        resp = self.client.get('/api/questions/dashboard/')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['total_questions'], 3)
        self.assertEqual(resp.data['attempted'], 2)
        self.assertEqual(resp.data['new'], 1)
        self.assertEqual(resp.data['correct'], 1)
        self.assertEqual(resp.data['incorrect'], 1)
        self.assertEqual(resp.data['mastered'], 1)
        self.assertEqual(resp.data['weak'], 1)

    def test_dashboard_requires_auth(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get('/api/questions/dashboard/')
        self.assertEqual(resp.status_code, 401)


class QuestionMistakesEndpointTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='student1', email='student1@example.com', password='pw12345')
        self.physics = Subject.objects.create(name='Physics')
        self.chemistry = Subject.objects.create(name='Chemistry')
        self.wrong_physics = Question.objects.create(subject=self.physics, text='Wrong physics Q')
        self.wrong_chemistry = Question.objects.create(subject=self.chemistry, text='Wrong chem Q')
        self.right_physics = Question.objects.create(subject=self.physics, text='Right physics Q')
        self.client.force_authenticate(user=self.student)

    def test_mistakes_grouped_by_subject_and_excludes_correct_questions(self):
        from academics.services import record_question_result

        record_question_result(self.student, self.wrong_physics, False, source='qbank')
        record_question_result(self.student, self.wrong_chemistry, False, source='test')
        record_question_result(self.student, self.right_physics, True, source='qbank')

        resp = self.client.get('/api/questions/mistakes/')

        self.assertEqual(resp.status_code, 200)
        counts = {row['subject_name']: row['count'] for row in resp.data['by_subject']}
        self.assertEqual(counts, {'Physics': 1, 'Chemistry': 1})
        result_ids = {q['id'] for q in resp.data['results']}
        self.assertEqual(result_ids, {self.wrong_physics.id, self.wrong_chemistry.id})

    def test_a_question_later_answered_correctly_leaves_the_mistake_list(self):
        from academics.services import record_question_result

        record_question_result(self.student, self.wrong_physics, False, source='qbank')
        record_question_result(self.student, self.wrong_physics, True, source='qbank')

        resp = self.client.get('/api/questions/mistakes/')
        result_ids = {q['id'] for q in resp.data['results']}
        self.assertNotIn(self.wrong_physics.id, result_ids)


class QuestionPracticeSessionEndpointTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='student1', email='student1@example.com', password='pw12345')
        self.subject = Subject.objects.create(name='Physics')
        self.new_q = Question.objects.create(subject=self.subject, text='Never attempted')
        self.weak_q = Question.objects.create(subject=self.subject, text='Weak question')
        self.mastered_q = Question.objects.create(subject=self.subject, text='Mastered question')
        self.client.force_authenticate(user=self.student)

    def test_status_new_returns_only_unattempted_questions(self):
        from academics.services import record_question_result

        record_question_result(self.student, self.weak_q, False, source='qbank')
        record_question_result(self.student, self.mastered_q, True, source='qbank')
        record_question_result(self.student, self.mastered_q, True, source='qbank')

        resp = self.client.post('/api/questions/practice-session/', {'status': ['new']}, format='json')

        ids = {q['id'] for q in resp.data}
        self.assertEqual(ids, {self.new_q.id})

    def test_status_weak_returns_only_weak_questions(self):
        from academics.services import record_question_result

        record_question_result(self.student, self.weak_q, False, source='qbank')
        record_question_result(self.student, self.weak_q, False, source='qbank')

        resp = self.client.post('/api/questions/practice-session/', {'status': ['weak']}, format='json')

        ids = {q['id'] for q in resp.data}
        self.assertEqual(ids, {self.weak_q.id})

    def test_count_is_capped_at_100(self):
        # Individual .create() calls, not bulk_create — Question.save() is
        # what generates the unique slug/public_id, and bulk_create skips save().
        for i in range(120):
            Question.objects.create(subject=self.subject, text=f'Bulk {i}')

        resp = self.client.post('/api/questions/practice-session/', {'count': 500}, format='json')

        self.assertLessEqual(len(resp.data), 100)


class RecomputeQuestionDifficultyCommandTests(TestCase):
    def setUp(self):
        self.subject = Subject.objects.create(name='Physics')
        self.question = Question.objects.create(subject=self.subject, text='Q1', instructor_difficulty='hard')

    def test_below_min_attempts_leaves_actual_difficulty_blank(self):
        from django.core.management import call_command

        from academics.models import QuestionBankConfig

        QuestionBankConfig.objects.create(pk=1, min_attempts_for_difficulty=30)
        student = User.objects.create_user(username='s1', email='s1@example.com', password='pw12345')
        QuestionAttempt.objects.create(user=student, question=self.question, attempts_count=5, correct_count=5)

        call_command('recompute_question_difficulty')

        self.question.refresh_from_db()
        self.assertEqual(self.question.actual_difficulty, '')

    def test_meets_min_attempts_computes_difficulty_without_touching_instructor_difficulty(self):
        from django.core.management import call_command

        from academics.models import QuestionBankConfig

        QuestionBankConfig.objects.create(pk=1, min_attempts_for_difficulty=10, easy_min_pct=75, medium_min_pct=55, hard_min_pct=30)
        student = User.objects.create_user(username='s1', email='s1@example.com', password='pw12345')
        # 2/10 correct = 20% -> below hard_min_pct (30) -> very_hard
        QuestionAttempt.objects.create(user=student, question=self.question, attempts_count=10, correct_count=2)

        call_command('recompute_question_difficulty')

        self.question.refresh_from_db()
        self.assertEqual(self.question.actual_difficulty, 'very_hard')
        self.assertEqual(self.question.actual_difficulty_sample_size, 10)
        self.assertEqual(self.question.instructor_difficulty, 'hard')


class QuestionCourseScopingTests(APITestCase):
    """A question explicitly tagged to a course must not surface for a
    student not enrolled in it — closes the QBank-search leak (spec item
    19), where the frontend previously sent no ?course= at all and the
    backend's course filter was opt-in on the client supplying one."""

    def setUp(self):
        from courses.models import Course, Enrollment

        self.cee_mbbs = Course.objects.create(name='CEE-MBBS', prefix='CEEMBBSQ')
        self.nmcle_mbbs = Course.objects.create(name='NMCLE-MBBS', prefix='NMCLEMBBSQ')
        self.cee_student = User.objects.create_user(username='cee_q_student', email='ceeq@example.com', password='pw12345')
        Enrollment.objects.create(user=self.cee_student, course=self.cee_mbbs)

        self.subject = Subject.objects.create(name='Biology', is_free=True)
        self.cee_question = Question.objects.create(subject=self.subject, text='CEE-MBBS only question')
        self.cee_question.courses.set([self.cee_mbbs])
        self.nmcle_question = Question.objects.create(subject=self.subject, text='NMCLE-MBBS only question')
        self.nmcle_question.courses.set([self.nmcle_mbbs])
        self.shared_question = Question.objects.create(subject=self.subject, text='Untagged shared question')

        self.client.force_authenticate(user=self.cee_student)

    def test_browse_excludes_other_courses_question_even_without_course_param(self):
        resp = self.client.get('/api/questions/browse/', {'search': 'question'})
        ids = {q['id'] for q in resp.data['results']}
        self.assertIn(self.cee_question.id, ids)
        self.assertIn(self.shared_question.id, ids)
        self.assertNotIn(self.nmcle_question.id, ids)
