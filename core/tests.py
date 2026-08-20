from unittest.mock import Mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.deletion_audit import delete_file_field, record_deletion
from core.models import DeletionAuditLog

User = get_user_model()


class RecordDeletionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='admin1', email='admin1@example.com', password='pw12345', is_staff=True,
        )

    def _request(self, user=None, ip='203.0.113.5', forwarded=None, agent='pytest-agent'):
        request = Mock()
        request.user = user
        request.META = {'REMOTE_ADDR': ip, 'HTTP_USER_AGENT': agent}
        if forwarded:
            request.META['HTTP_X_FORWARDED_FOR'] = forwarded
        return request

    def test_success_entry_captures_actor_and_ip(self):
        request = self._request(user=self.user)
        record_deletion(request, 'Question', 42, 'PH0001', result='success')

        entry = DeletionAuditLog.objects.get()
        self.assertEqual(entry.actor, self.user)
        self.assertEqual(entry.actor_email, 'admin1@example.com')
        self.assertEqual(entry.resource_type, 'Question')
        self.assertEqual(entry.resource_id, '42')
        self.assertEqual(entry.resource_label, 'PH0001')
        self.assertEqual(entry.result, 'success')
        self.assertEqual(entry.ip_address, '203.0.113.5')
        self.assertEqual(entry.failure_reason, '')

    def test_failure_entry_captures_reason(self):
        request = self._request(user=self.user)
        record_deletion(request, 'Course', 7, 'CEE-PG', result='failure', failure_reason='has enrollments')

        entry = DeletionAuditLog.objects.get()
        self.assertEqual(entry.result, 'failure')
        self.assertEqual(entry.failure_reason, 'has enrollments')

    def test_anonymous_actor_is_not_recorded(self):
        anon = Mock()
        anon.is_authenticated = False
        request = self._request(user=anon)
        record_deletion(request, 'Course', 7, 'CEE-PG', result='success')

        entry = DeletionAuditLog.objects.get()
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.actor_email, '')

    def test_x_forwarded_for_takes_priority_over_remote_addr(self):
        request = self._request(user=self.user, ip='10.0.0.1', forwarded='198.51.100.9, 10.0.0.1')
        record_deletion(request, 'Course', 7, 'CEE-PG', result='success')

        entry = DeletionAuditLog.objects.get()
        self.assertEqual(entry.ip_address, '198.51.100.9')

    def test_does_not_store_deleted_content_beyond_label(self):
        """The audit log must never carry the deleted record's private content —
        only a short label, per the spec's minimal-audit requirement."""
        request = self._request(user=self.user)
        record_deletion(request, 'Question', 42, 'PH0001', result='success')

        entry = DeletionAuditLog.objects.get()
        field_names = {f.name for f in DeletionAuditLog._meta.get_fields()}
        self.assertNotIn('resource_body', field_names)
        self.assertNotIn('content', field_names)
        self.assertLessEqual(len(entry.resource_label), 255)


class DeleteFileFieldTests(TestCase):
    def test_noop_on_empty_field(self):
        empty_field = Mock()
        empty_field.__bool__ = Mock(return_value=False)
        delete_file_field(empty_field)
        empty_field.delete.assert_not_called()

    def test_deletes_underlying_storage_file(self):
        field = Mock()
        field.__bool__ = Mock(return_value=True)
        delete_file_field(field)
        field.delete.assert_called_once_with(save=False)

    def test_swallows_storage_errors(self):
        field = Mock()
        field.__bool__ = Mock(return_value=True)
        field.delete.side_effect = OSError('storage unavailable')
        delete_file_field(field)  # must not raise
