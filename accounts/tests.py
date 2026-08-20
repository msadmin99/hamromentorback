from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from billing.models import Purchase
from core.models import DeletionAuditLog
from marketplace.models import TeacherCourse

User = get_user_model()


class AdminAccountDeleteTests(APITestCase):
    def setUp(self):
        self.super_admin = User.objects.create_user(
            username='super1', email='super1@example.com', password='pw12345',
            is_staff=True, admin_role='super_admin',
        )
        self.plain_admin = User.objects.create_user(
            username='admin1', email='admin1@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.client.force_authenticate(user=self.super_admin)

    def test_only_super_admin_can_delete(self):
        target = User.objects.create_user(
            username='editor1', email='editor1@example.com', password='pw12345',
            is_staff=True, admin_role='editor',
        )
        self.client.force_authenticate(user=self.plain_admin)

        resp = self.client.delete(f'/api/auth/admin-accounts/{target.id}/')

        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
        self.assertTrue(User.objects.filter(id=target.id).exists())

    def test_blocked_when_account_owns_marketplace_courses(self):
        teacher_account = User.objects.create_user(
            username='teacher1', email='teacher1@example.com', password='pw12345',
            is_staff=True, admin_role='teacher',
        )
        TeacherCourse.objects.create(teacher=teacher_account, title='Physiology Basics')

        resp = self.client.delete(f'/api/auth/admin-accounts/{teacher_account.id}/')

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(User.objects.filter(id=teacher_account.id).exists())
        entry = DeletionAuditLog.objects.get(resource_type='AdminAccount', resource_id=str(teacher_account.id))
        self.assertEqual(entry.result, 'failure')

    def test_blocked_when_account_has_purchase_history(self):
        Purchase.objects.create(
            user=self.plain_admin, kind='subscription', original_amount=500, final_amount=500,
        )

        resp = self.client.delete(f'/api/auth/admin-accounts/{self.plain_admin.id}/')

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(User.objects.filter(id=self.plain_admin.id).exists())
        self.assertIn('financial records', resp.data['detail'])

    def test_permanent_delete_succeeds_for_clean_account(self):
        target = User.objects.create_user(
            username='editor1', email='editor1@example.com', password='pw12345',
            is_staff=True, admin_role='editor',
        )

        resp = self.client.delete(f'/api/auth/admin-accounts/{target.id}/')

        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(User.objects.filter(id=target.id).exists())
        entry = DeletionAuditLog.objects.get(resource_type='AdminAccount')
        self.assertEqual(entry.result, 'success')
