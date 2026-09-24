from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from billing.models import Purchase
from core.models import DeletionAuditLog

User = get_user_model()


class AdminStudentDeleteTests(APITestCase):
    """Mirrors AdminAccountDeleteTests' exact reasoning, applied to student
    accounts (AdminUserViewSet's queryset is_staff=False): Super-Admin only,
    blocked on purchase/financial history, every attempt audited."""

    def setUp(self):
        self.super_admin = User.objects.create_user(
            username='sdel_super', email='sdel_super@example.com', password='pw12345',
            is_staff=True, admin_role='super_admin',
        )
        self.plain_admin = User.objects.create_user(
            username='sdel_admin', email='sdel_admin@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(
            username='sdel_student', email='sdel_student@example.com', password='pw12345',
        )
        self.client.force_authenticate(user=self.super_admin)

    def test_only_super_admin_can_delete(self):
        self.client.force_authenticate(user=self.plain_admin)

        resp = self.client.delete(f'/api/auth/users/{self.student.id}/')

        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
        self.assertTrue(User.objects.filter(id=self.student.id).exists())

    def test_blocked_when_student_has_purchase_history(self):
        Purchase.objects.create(
            user=self.student, kind='subscription', original_amount=500, final_amount=500,
        )

        resp = self.client.delete(f'/api/auth/users/{self.student.id}/')

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('financial records', resp.data['detail'])
        self.assertTrue(User.objects.filter(id=self.student.id).exists())
        entry = DeletionAuditLog.objects.get(resource_type='StudentAccount', resource_id=str(self.student.id))
        self.assertEqual(entry.result, 'failure')

    def test_permanent_delete_succeeds_for_clean_student(self):
        resp = self.client.delete(f'/api/auth/users/{self.student.id}/')

        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(User.objects.filter(id=self.student.id).exists())
        entry = DeletionAuditLog.objects.get(resource_type='StudentAccount', resource_id=str(self.student.id))
        self.assertEqual(entry.result, 'success')
        self.assertEqual(entry.resource_label, 'sdel_student@example.com')

    def test_cannot_delete_a_staff_account_through_this_endpoint(self):
        # AdminUserViewSet's queryset is filtered to is_staff=False, so a
        # staff id here must 404, never fall through to deleting it anyway.
        resp = self.client.delete(f'/api/auth/users/{self.plain_admin.id}/')

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(User.objects.filter(id=self.plain_admin.id).exists())

    def test_unauthenticated_request_rejected(self):
        self.client.force_authenticate(user=None)
        resp = self.client.delete(f'/api/auth/users/{self.student.id}/')
        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
        self.assertTrue(User.objects.filter(id=self.student.id).exists())
