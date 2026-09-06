"""RolePermission audit logging — regression suite.

`RolePermission.features` decides what other staff accounts can do
platform-wide, and every mutation of it was previously invisible in the
audit trail. These tests pin that each real mutation path now writes
exactly one attributable `AdminEditAuditLog` record, that failures write
none, and that the actor can never be supplied by the caller.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from accounts.models import ALL_FEATURES, EDITOR_ALLOWED_FEATURES, RolePermission
from accounts.role_audit import diff, snapshot
from core.models import AdminEditAuditLog

User = get_user_model()


def _entries():
    return AdminEditAuditLog.objects.filter(resource_type='RolePermission')


class SnapshotAndDiffTests(TestCase):
    """The representation itself, before any endpoint is involved."""

    def test_snapshot_of_a_missing_row_is_empty(self):
        self.assertEqual(snapshot(None), {})

    def test_snapshot_copies_the_features_list(self):
        """Holding the live list would let a later mutation of the same
        object rewrite what the audit record claims the old value was."""
        row = RolePermission.objects.create(role='editor', features=['question_entry'])
        snap = snapshot(row)
        row.features.append('exam_schedule')
        self.assertEqual(snap['features'], ['question_entry'])

    def test_diff_reports_only_fields_that_changed(self):
        before = {'id': 1, 'role': 'editor', 'features': ['a']}
        after = {'id': 1, 'role': 'editor', 'features': ['a', 'b']}
        self.assertEqual(diff(before, after), {'features': {'old': ['a'], 'new': ['a', 'b']}})

    def test_diff_never_reports_the_id_as_a_change(self):
        """`id` rides along for identification only — it must not appear as
        a permission change."""
        self.assertEqual(diff({'id': 1, 'role': 'admin', 'features': []},
                              {'id': 2, 'role': 'admin', 'features': []}), {})

    def test_create_and_delete_are_expressible_without_a_new_action_column(self):
        after = {'id': 3, 'role': 'admin', 'features': ['billing']}
        self.assertEqual(
            diff({}, after),
            {'role': {'old': None, 'new': 'admin'}, 'features': {'old': None, 'new': ['billing']}},
        )
        self.assertEqual(
            diff(after, {}),
            {'role': {'old': 'admin', 'new': None}, 'features': {'old': ['billing'], 'new': None}},
        )


class RolePermissionApiAuditTests(APITestCase):
    """The admin panel's API path — POST / PUT / PATCH."""

    def setUp(self):
        self.superadmin = User.objects.create_user(
            username='sa', email='sa@x.com', password='pw12345!', is_staff=True, is_superuser=True,
        )
        self.list_url = reverse('role-permission-list')
        self.client.force_authenticate(self.superadmin)

    def _detail(self, row):
        return reverse('role-permission-detail', args=[row.pk])

    def test_create_is_audited_with_the_new_state(self):
        resp = self.client.post(self.list_url, {'role': 'editor', 'features': ['question_entry']}, format='json')
        self.assertEqual(resp.status_code, 201)

        entry = _entries().get()
        self.assertEqual(entry.actor, self.superadmin)
        self.assertEqual(entry.actor_email, 'sa@x.com')
        self.assertEqual(entry.resource_label, 'editor')
        self.assertEqual(entry.changed_fields['role'], {'old': None, 'new': 'editor'})
        self.assertEqual(entry.changed_fields['features'], {'old': None, 'new': ['question_entry']})

    def test_update_records_both_old_and_new_features(self):
        row = RolePermission.objects.create(role='editor', features=['question_entry'])
        new = [f for f in EDITOR_ALLOWED_FEATURES][:2]
        resp = self.client.put(self._detail(row), {'role': 'editor', 'features': new}, format='json')
        self.assertEqual(resp.status_code, 200)

        entry = _entries().get()
        self.assertEqual(entry.changed_fields['features']['old'], ['question_entry'])
        self.assertEqual(entry.changed_fields['features']['new'], new)
        self.assertEqual(entry.resource_id, str(row.pk))

    def test_partial_update_is_audited(self):
        row = RolePermission.objects.create(role='admin', features=['billing'])
        resp = self.client.patch(self._detail(row), {'features': ALL_FEATURES}, format='json')
        self.assertEqual(resp.status_code, 200)
        entry = _entries().get()
        self.assertEqual(entry.changed_fields['features']['old'], ['billing'])
        self.assertNotIn('role', entry.changed_fields)

    def test_a_no_op_update_writes_no_record(self):
        """Resubmitting identical values is not a permission change, and an
        entry for it would later read as one."""
        row = RolePermission.objects.create(role='admin', features=['billing'])
        resp = self.client.patch(self._detail(row), {'features': ['billing']}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_entries().count(), 0)

    def test_exactly_one_record_per_mutation(self):
        row = RolePermission.objects.create(role='admin', features=['billing'])
        self.client.patch(self._detail(row), {'features': ['billing', 'exam_schedule']}, format='json')
        self.assertEqual(_entries().count(), 1)

    def test_delete_is_not_offered_by_this_endpoint(self):
        """DELETE is excluded by the viewset's http_method_names, so no
        delete audit is invented for a path that does not exist."""
        row = RolePermission.objects.create(role='admin', features=['billing'])
        resp = self.client.delete(self._detail(row))
        self.assertEqual(resp.status_code, 405)
        self.assertTrue(RolePermission.objects.filter(pk=row.pk).exists())
        self.assertEqual(_entries().count(), 0)

    def test_reads_are_not_audited(self):
        RolePermission.objects.create(role='admin', features=['billing'])
        self.client.get(self.list_url)
        self.assertEqual(_entries().count(), 0)


class RolePermissionFailedMutationTests(APITestCase):
    """A failure must never leave a record claiming success."""

    def setUp(self):
        self.superadmin = User.objects.create_user(
            username='sa', email='sa@x.com', password='pw12345!', is_staff=True, is_superuser=True,
        )
        self.client.force_authenticate(self.superadmin)

    def test_validation_failure_writes_no_record(self):
        """The editor ceiling rejects a feature outside EDITOR_ALLOWED_FEATURES."""
        row = RolePermission.objects.create(role='editor', features=['question_entry'])
        outside = next(f for f in ALL_FEATURES if f not in EDITOR_ALLOWED_FEATURES)
        resp = self.client.patch(
            reverse('role-permission-detail', args=[row.pk]), {'features': [outside]}, format='json',
        )
        self.assertEqual(resp.status_code, 400)
        row.refresh_from_db()
        self.assertEqual(row.features, ['question_entry'])
        self.assertEqual(_entries().count(), 0)

    def test_not_found_writes_no_record(self):
        resp = self.client.patch(reverse('role-permission-detail', args=[999999]), {'features': []}, format='json')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(_entries().count(), 0)

    def test_rollback_leaves_neither_the_change_nor_the_record(self):
        """The audit write shares the mutation's transaction, so if the
        audit fails the permission change is rolled back too — the trail
        can never silently fall behind the thing it is evidence of."""
        row = RolePermission.objects.create(role='admin', features=['billing'])
        with patch('accounts.role_audit.record_admin_edit', side_effect=RuntimeError('audit down')):
            with self.assertRaises(RuntimeError):
                self.client.patch(
                    reverse('role-permission-detail', args=[row.pk]),
                    {'features': ['billing', 'exam_schedule']}, format='json',
                )
        row.refresh_from_db()
        self.assertEqual(row.features, ['billing'])
        self.assertEqual(_entries().count(), 0)


class RolePermissionAuthorizationTests(APITestCase):
    """Authorization is unchanged by this task — pinned so the audit work
    cannot have quietly widened it."""

    def setUp(self):
        self.list_url = reverse('role-permission-list')
        self.row = RolePermission.objects.create(role='editor', features=['question_entry'])
        self.detail_url = reverse('role-permission-detail', args=[self.row.pk])

        self.student = User.objects.create_user(username='stu', email='stu@x.com', password='pw12345!')
        self.teacher = User.objects.create_user(
            username='tea', email='tea@x.com', password='pw12345!', is_staff=True,
        )
        self.teacher.admin_role = 'teacher'
        self.teacher.save(update_fields=['admin_role'])
        self.editor = User.objects.create_user(
            username='ed', email='ed@x.com', password='pw12345!', is_staff=True,
        )
        self.editor.admin_role = 'editor'
        self.editor.save(update_fields=['admin_role'])
        self.admin = User.objects.create_user(
            username='ad', email='ad@x.com', password='pw12345!', is_staff=True,
        )
        self.admin.admin_role = 'admin'
        self.admin.save(update_fields=['admin_role'])

    def test_anonymous_cannot_mutate(self):
        self.assertIn(self.client.patch(self.detail_url, {'features': []}, format='json').status_code, (401, 403))
        self.assertEqual(_entries().count(), 0)

    def test_student_teacher_editor_and_admin_roles_are_all_denied(self):
        """IsSuperAdmin — is_staff alone is deliberately not enough, and an
        Admin-role account cannot escalate itself by rewriting the row."""
        for user in (self.student, self.teacher, self.editor, self.admin):
            with self.subTest(user=user.username):
                self.client.force_authenticate(user)
                resp = self.client.patch(self.detail_url, {'features': list(ALL_FEATURES)}, format='json')
                self.assertEqual(resp.status_code, 403)
        self.row.refresh_from_db()
        self.assertEqual(self.row.features, ['question_entry'])
        self.assertEqual(_entries().count(), 0)

    def test_a_denied_request_cannot_manufacture_an_audit_entry(self):
        """A rejected caller must not be able to write into the audit log at
        all — the record is a side effect of a successful mutation, never
        of a request."""
        self.client.force_authenticate(self.editor)
        self.client.post(self.list_url, {'role': 'admin', 'features': list(ALL_FEATURES)}, format='json')
        self.assertEqual(AdminEditAuditLog.objects.count(), 0)


class RolePermissionActorForgeryTests(APITestCase):
    """The actor comes from the authenticated principal, never the payload."""

    def setUp(self):
        self.superadmin = User.objects.create_user(
            username='sa', email='sa@x.com', password='pw12345!', is_staff=True, is_superuser=True,
        )
        self.victim = User.objects.create_user(
            username='victim', email='victim@x.com', password='pw12345!', is_staff=True, is_superuser=True,
        )
        self.client.force_authenticate(self.superadmin)

    def test_actor_fields_in_the_request_body_are_ignored(self):
        row = RolePermission.objects.create(role='admin', features=['billing'])
        self.client.patch(
            reverse('role-permission-detail', args=[row.pk]),
            {
                'features': ['billing', 'exam_schedule'],
                'actor': self.victim.id,
                'actor_id': self.victim.id,
                'actor_email': self.victim.email,
                'created_at': '2001-01-01T00:00:00Z',
            },
            format='json',
        )
        entry = _entries().get()
        self.assertEqual(entry.actor, self.superadmin)
        self.assertEqual(entry.actor_email, 'sa@x.com')
        self.assertNotEqual(entry.actor, self.victim)
        self.assertGreater(entry.created_at.year, 2001)

    def test_unknown_body_fields_do_not_reach_the_model(self):
        """Mass assignment: the serializer's field list is the allowlist."""
        resp = self.client.post(
            reverse('role-permission-list'),
            {'role': 'editor', 'features': ['question_entry'], 'id': 4242},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(RolePermission.objects.filter(pk=4242).exists())


class AuditRecordPrivacyAndImmutabilityTests(APITestCase):
    """What the record holds, and what can touch it afterwards."""

    def setUp(self):
        self.superadmin = User.objects.create_user(
            username='sa', email='sa@x.com', password='pw12345!', is_staff=True, is_superuser=True,
        )
        self.client.force_authenticate(self.superadmin)

    def test_record_holds_only_role_and_features(self):
        row = RolePermission.objects.create(role='admin', features=['billing'])
        self.client.patch(
            reverse('role-permission-detail', args=[row.pk]), {'features': ['billing', 'exam_schedule']}, format='json',
        )
        entry = _entries().get()
        self.assertEqual(set(entry.changed_fields), {'features'})
        blob = str(entry.changed_fields)
        self.assertNotIn('pw12345!', blob)
        self.assertNotIn('password', blob)

    def test_no_api_exposes_the_audit_log_for_reading_or_writing(self):
        """AdminEditAuditLog has no serializer, no route and no Django-admin
        registration, so there is no surface through which an entry can be
        edited or deleted. Immutability here is the absence of a write
        path, not a permission check — asserted so that adding one later is
        a deliberate act that fails this test first."""
        from django.contrib import admin as django_admin

        self.assertNotIn(AdminEditAuditLog, django_admin.site._registry)

        from django.urls import get_resolver

        routes = str(get_resolver().url_patterns)
        self.assertNotIn('admineditauditlog', routes.lower())

    def test_response_shape_is_unchanged_by_auditing(self):
        """The audit write is internal — no existing client sees a new or
        missing field."""
        resp = self.client.post(
            reverse('role-permission-list'), {'role': 'editor', 'features': ['question_entry']}, format='json',
        )
        self.assertEqual(set(resp.data), {'id', 'role', 'features'})


class RolePermissionDjangoAdminAuditTests(TestCase):
    """The Django admin site is the second real mutation path — add,
    change, and both delete routes."""

    def setUp(self):
        self.superadmin = User.objects.create_user(
            username='sa', email='sa@x.com', password='pw12345!', is_staff=True, is_superuser=True,
        )
        from django.contrib import admin as django_admin

        self.model_admin = django_admin.site._registry[RolePermission]

        from django.test import RequestFactory

        self.request = RequestFactory().post('/admin/')
        self.request.user = self.superadmin

    def test_admin_change_records_old_and_new(self):
        row = RolePermission.objects.create(role='admin', features=['billing'])
        row.features = ['billing', 'exam_schedule']
        self.model_admin.save_model(self.request, row, form=None, change=True)

        entry = _entries().get()
        self.assertEqual(entry.actor, self.superadmin)
        self.assertEqual(entry.changed_fields['features']['old'], ['billing'])
        self.assertEqual(entry.changed_fields['features']['new'], ['billing', 'exam_schedule'])

    def test_admin_add_records_the_new_state(self):
        row = RolePermission(role='editor', features=['question_entry'])
        self.model_admin.save_model(self.request, row, form=None, change=False)
        entry = _entries().get()
        self.assertEqual(entry.changed_fields['role'], {'old': None, 'new': 'editor'})

    def test_admin_delete_is_audited(self):
        row = RolePermission.objects.create(role='editor', features=['question_entry'])
        pk = row.pk
        self.model_admin.delete_model(self.request, row)

        entry = _entries().get()
        self.assertEqual(entry.resource_id, str(pk))
        self.assertEqual(entry.changed_fields['features'], {'old': ['question_entry'], 'new': None})
        self.assertFalse(RolePermission.objects.filter(pk=pk).exists())

    def test_admin_bulk_delete_audits_every_row(self):
        """The bulk action must not be the unaudited way to remove a role's
        feature grant."""
        RolePermission.objects.create(role='admin', features=['billing'])
        RolePermission.objects.create(role='editor', features=['question_entry'])
        self.model_admin.delete_queryset(self.request, RolePermission.objects.all())

        self.assertEqual(_entries().count(), 2)
        self.assertEqual(RolePermission.objects.count(), 0)
        self.assertEqual(
            {e.resource_label for e in _entries()}, {'admin', 'editor'},
        )
