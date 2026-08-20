from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APITestCase

from academics.models import Option, Question, Subject
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
