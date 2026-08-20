from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APITestCase

from academics.models import Chapter, ImportBatch, ImportRow, Option, Question, Subject, Topic
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
