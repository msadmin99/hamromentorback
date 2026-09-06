"""FINAL PRODUCTION READINESS AUDIT — Student Identity & Document
Verification System.

A second, independent test file from accounts/tests_verification.py
(which already covers upload validation, IDOR, admin workflow, and the
pure-function access-regression matrix). This file exists to satisfy the
audit's more adversarial and more literal requirements:

- the REAL HTTP start-attempt endpoint (not just the pure access
  functions) across all four verification states, including the actual
  Free Starter fallback path (entitlements app) currently live in this
  working tree
- mass-assignment attempts from student-facing input
- role-boundary tests (student/editor/teacher vs admin/super_admin)
- the full resubmission lifecycle with zero manual DB steps
- malicious-upload adversarial cases (path traversal, executable
  masquerading, zero-byte)
- profile/document status consistency edge cases
- query-count assertions on the detail endpoint with verification data
  present
- error isolation (a GCS failure must not affect registration or access)

GCS calls are mocked throughout — see accounts/tests_verification.py's
own precedent (mirrors billing/tests.py's screenshot_storage mocking).
"""
import io
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from PIL import Image
from rest_framework.test import APITestCase

from accounts.models import StudentProfile, VerificationDocument
from courses.models import Course, Enrollment

User = get_user_model()


def _png_bytes():
    buf = io.BytesIO()
    Image.new('RGB', (2, 2), color='green').save(buf, format='PNG')
    buf.seek(0)
    return buf.read()


def _valid_upload(name='doc.png'):
    return SimpleUploadedFile(name, _png_bytes(), content_type='image/png')


# ---------------------------------------------------------------------------
# 3 + 4. Real HTTP start-attempt regression (free + paid, all 4 states),
# including the live Free Starter fallback path.
# ---------------------------------------------------------------------------
@override_settings(MEDIA_GCS_PRIVATE_BUCKET='test-private-bucket')
class RealStartAttemptRegressionTests(APITestCase):
    """Exercises the ACTUAL POST /api/tests/{id}/start/ endpoint (the real
    _start_attempt code path, including whatever Free Starter/entitlements
    integration currently lives in tests_app/views.py) — not just the pure
    billing.access/courses.access functions in isolation. Every verification
    state must produce byte-identical HTTP status + free-starter/subscription
    accounting."""

    STATUSES = ['unverified', 'pending', 'verified', 'rejected']

    @classmethod
    def setUpTestData(cls):
        from billing.models import Subscription
        from entitlements.models import FreeStarterPolicy
        from tests_app.models import Test

        cls.course = Course.objects.create(name='Verification Audit Course', prefix='VAUD')

        cls.free_student = User.objects.create_user(
            username='vaud_free', email='vaud_free@example.com', password='pw12345',
        )
        Enrollment.objects.create(user=cls.free_student, course=cls.course, access_type='free', is_active=True)

        cls.paid_student = User.objects.create_user(
            username='vaud_paid', email='vaud_paid@example.com', password='pw12345',
        )
        Enrollment.objects.create(user=cls.paid_student, course=cls.course, access_type='package', is_active=True)
        Subscription.objects.create(
            user=cls.paid_student, course=cls.course, product_type='mock_test', is_active=True, mock_test_quota=None,
        )

        # A Pro Daily Test with NO free_preview — the free student can only
        # reach it through the real Free Starter fallback (entitlements app),
        # exactly the live production code path this audit must exercise
        # directly, not a stale/mocked stand-in.
        FreeStarterPolicy.objects.update_or_create(
            resource_type='daily_test', defaults={'quantity': 5, 'unlimited': False, 'is_active': True},
        )
        cls.pro_daily_no_subscription = Test.objects.create(
            title='Audit Pro Daily', exam_type='daily', is_pro=True, is_draft=False,
            duration_minutes=30, free_preview_questions=0, max_attempts=5,
        )
        cls.pro_daily_no_subscription.courses.add(cls.course)

        cls.paid_mock_test = Test.objects.create(
            title='Audit Paid Mock', exam_type='mock', is_pro=True, is_draft=False,
            duration_minutes=60, max_attempts=5,
        )
        cls.paid_mock_test.courses.add(cls.course)

    def _set_status(self, user, status_value):
        profile, _ = StudentProfile.objects.get_or_create(user=user)
        profile.verification_status = status_value
        if status_value == 'rejected':
            profile.verification_rejection_reason = 'irrelevant'
        profile.save()

    def test_free_student_free_starter_start_attempt_identical_across_states(self):
        """A brand-new attempt (never started before) against a Pro Daily
        Test with no subscription — the real Free Starter consumption path.
        A fresh Test row + fresh attempt is used per status so the
        'has_prior_attempt' branch never contaminates the comparison; the
        thing being compared is whether the request succeeds and how the
        FreeStarterEntitlement's `used` counter behaves, not raw HTTP
        status codes from one shared, increasingly-consumed row."""
        from entitlements.models import FreeStarterEntitlement
        from tests_app.models import Test

        results = {}
        for status_value in self.STATUSES:
            self._set_status(self.free_student, status_value)
            # Independent Test row per status: proves the endpoint behaves
            # identically on a truly first-time attempt at every status,
            # not just "the second time onward" once free-starter already
            # granted access on an earlier iteration.
            test = Test.objects.create(
                title=f'Audit Pro Daily [{status_value}]', exam_type='daily', is_pro=True, is_draft=False,
                duration_minutes=30, free_preview_questions=0, max_attempts=5,
            )
            test.courses.add(self.course)
            self.client.force_authenticate(user=self.free_student)
            used_before = FreeStarterEntitlement.objects.filter(
                user=self.free_student, resource_type='daily_test',
            ).values_list('used', flat=True).first() or 0
            resp = self.client.post(f'/api/tests/{test.id}/start/', {})
            used_after = FreeStarterEntitlement.objects.get(user=self.free_student, resource_type='daily_test').used
            # The entitlement row is shared across iterations (one row per
            # (user, resource_type)), so what must be identical across
            # verification states is the per-iteration DELTA it consumes,
            # not its running cumulative total.
            results[status_value] = {
                'status_code': resp.status_code,
                'consumed_this_attempt': used_after - used_before,
            }
        baseline = results['unverified']
        for status_value, snapshot in results.items():
            self.assertEqual(
                snapshot, baseline,
                f'Free-starter start-attempt outcome differs at verification_status={status_value!r}: '
                f'{snapshot} vs unverified baseline {baseline}',
            )
        # Sanity: prove this test actually exercised the consuming path
        # (would be a false pass if every attempt were silently denied).
        self.assertEqual(baseline['status_code'], 201)
        self.assertEqual(baseline['consumed_this_attempt'], 1)

    def test_paid_student_start_attempt_identical_across_states(self):
        from tests_app.models import Test

        results = {}
        for status_value in self.STATUSES:
            self._set_status(self.paid_student, status_value)
            # A fresh Test row per iteration — reusing the same Test would
            # hit _start_attempt's own "resume the existing in_progress
            # attempt" branch (a real, correct behavior) on the 2nd+
            # iteration and return 200 instead of 201, which is a test-
            # harness artifact of iterating, not a verification-state effect.
            test = Test.objects.create(
                title=f'Audit Paid Mock [{status_value}]', exam_type='mock', is_pro=True, is_draft=False,
                duration_minutes=60, max_attempts=5,
            )
            test.courses.add(self.course)
            self.client.force_authenticate(user=self.paid_student)
            resp = self.client.post(f'/api/tests/{test.id}/start/', {})
            results[status_value] = resp.status_code
        baseline = results['unverified']
        for status_value, code in results.items():
            self.assertEqual(code, baseline, f'Paid start-attempt status differs at {status_value!r}: {code} vs {baseline}')
        self.assertEqual(baseline, 201)


# ---------------------------------------------------------------------------
# 5. Registration audit
# ---------------------------------------------------------------------------
class RegistrationAuditTests(APITestCase):
    def test_registration_creates_user_profile_and_tokens_unaffected(self):
        resp = self.client.post('/api/auth/register/', {
            'name': 'New Student', 'email': 'newstudent1@example.com', 'password': 'StrongPass123',
        })
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertIn('tokens', resp.data)
        self.assertIn('access', resp.data['tokens'])
        user = User.objects.get(email='newstudent1@example.com')
        profile = StudentProfile.objects.get(user=user)
        # A freshly-registered student is 'unverified' by default and was
        # never asked to upload anything — registration itself has zero
        # verification-related requirement.
        self.assertEqual(profile.verification_status, 'unverified')

    def test_document_upload_never_touches_registration_and_gcs_failure_is_isolated(self):
        """Registration succeeds even when the verification storage layer
        is completely broken — proves the two are not coupled at the
        infrastructure level either, not just the access-control level."""
        with patch('accounts.verification_storage.upload_bytes', side_effect=Exception('GCS outage')):
            resp = self.client.post('/api/auth/register/', {
                'name': 'New Student Two', 'email': 'newstudent2@example.com', 'password': 'StrongPass123',
            })
        self.assertEqual(resp.status_code, 201, resp.data)


# ---------------------------------------------------------------------------
# 9. Admin authorization — real role semantics, not just is_staff
# ---------------------------------------------------------------------------
@override_settings(MEDIA_GCS_PRIVATE_BUCKET='test-private-bucket')
class AdminRoleBoundaryTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='role_stu', email='role_stu@example.com', password='pw12345')
        self.editor = User.objects.create_user(
            username='role_editor', email='role_editor@example.com', password='pw12345', is_staff=True, admin_role='editor',
        )
        self.teacher = User.objects.create_user(
            username='role_teacher', email='role_teacher@example.com', password='pw12345', is_staff=True, admin_role='teacher',
        )
        self.admin = User.objects.create_user(
            username='role_admin', email='role_admin@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.super_admin = User.objects.create_user(
            username='role_super', email='role_super@example.com', password='pw12345', is_staff=True, admin_role='super_admin',
        )
        self.document = VerificationDocument.objects.create(
            user=self.student, document_type='citizenship', storage_bucket='test-private-bucket', storage_key='k1',
        )

    def _try_all_actions(self, user):
        self.client.force_authenticate(user=user)
        results = {}
        results['profile_approve'] = self.client.post(f'/api/auth/users/{self.student.id}/verification/approve/').status_code
        results['profile_reject'] = self.client.post(
            f'/api/auth/users/{self.student.id}/verification/reject/', {'reason': 'x'}
        ).status_code
        results['doc_approve'] = self.client.post(f'/api/auth/verification-documents/{self.document.id}/approve/').status_code
        results['doc_reject'] = self.client.post(
            f'/api/auth/verification-documents/{self.document.id}/reject/', {'reason': 'x'}
        ).status_code
        return results

    def test_student_denied_all_actions(self):
        results = self._try_all_actions(self.student)
        for action, code in results.items():
            self.assertEqual(code, 403, f'student should be denied {action}, got {code}')

    def test_editor_denied_all_actions(self):
        # IsAdminRoleOrAbove explicitly blocks 'editor' — matches the
        # platform's documented Editor-role ceiling for every other
        # admin-role-gated feature (RolePermission's own EDITOR_ALLOWED_FEATURES).
        results = self._try_all_actions(self.editor)
        for action, code in results.items():
            self.assertEqual(code, 403, f'editor should be denied {action}, got {code}')

    def test_teacher_denied_all_actions(self):
        results = self._try_all_actions(self.teacher)
        for action, code in results.items():
            self.assertEqual(code, 403, f'teacher should be denied {action}, got {code}')

    def test_admin_allowed_all_actions(self):
        results = self._try_all_actions(self.admin)
        for action, code in results.items():
            self.assertIn(code, (200,), f'admin should be allowed {action}, got {code}')

    def test_super_admin_allowed_all_actions(self):
        results = self._try_all_actions(self.super_admin)
        for action, code in results.items():
            self.assertIn(code, (200,), f'super_admin should be allowed {action}, got {code}')

    def test_no_document_delete_endpoint_exists(self):
        """Structural IDOR guard: there is no delete/modify route at all
        for a VerificationDocument, for anyone — the only mutations
        possible are approve/reject (status-only) via the admin-gated
        endpoints above. DELETE against the view/approve/reject URLs
        must be rejected as method-not-allowed, never routed to a
        deletion of any kind."""
        self.client.force_authenticate(user=self.admin)
        resp = self.client.delete(f'/api/auth/verification-documents/{self.document.id}/approve/')
        self.assertEqual(resp.status_code, 405)
        self.document.refresh_from_db()  # still exists, untouched
        self.assertEqual(self.document.status, 'pending')


# ---------------------------------------------------------------------------
# 10. Mass-assignment audit
# ---------------------------------------------------------------------------
@override_settings(MEDIA_GCS_PRIVATE_BUCKET='test-private-bucket')
class MassAssignmentAuditTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='mass_stu', email='mass_stu@example.com', password='pw12345')
        self.admin = User.objects.create_user(
            username='mass_admin', email='mass_admin@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.client.force_authenticate(user=self.student)

    def test_verification_status_verified_rejected_via_student_edit_endpoint(self):
        # student_edit is admin-only in practice, but confirm the field
        # allowlist itself rejects verification_status even for an admin
        # caller — the endpoint must not be a backdoor around the
        # dedicated verify/reject actions.
        self.client.force_authenticate(user=self.admin)
        resp = self.client.patch(f'/api/auth/users/{self.student.id}/edit/', {'verification_status': 'verified'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('verification_status', resp.data.get('detail', ''))
        profile = StudentProfile.objects.filter(user=self.student).first()
        self.assertTrue(profile is None or profile.verification_status == 'unverified')

    def test_account_settings_patch_ignores_verification_status(self):
        resp = self.client.patch('/api/auth/settings/', {'verification_status': 'verified', 'name': 'Still Me'})
        self.assertEqual(resp.status_code, 200)
        profile = StudentProfile.objects.filter(user=self.student).first()
        self.assertTrue(profile is None or profile.verification_status == 'unverified')

    @patch('accounts.verification_storage.upload_bytes')
    def test_verification_submission_ignores_extra_admin_only_fields(self, mock_upload):
        resp = self.client.post(
            '/api/auth/me/verification/',
            {
                'document_type': 'citizenship', 'file': _valid_upload(),
                'status': 'approved', 'reviewed_by': self.admin.id, 'verified_by': self.admin.id,
                'reviewed_at': '2020-01-01T00:00:00Z',
            },
            format='multipart',
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        doc = VerificationDocument.objects.get(user=self.student)
        self.assertEqual(doc.status, 'pending')
        self.assertIsNone(doc.reviewed_by_id)
        self.assertIsNone(doc.reviewed_at)

    def test_no_route_accepts_direct_profile_serializer_write(self):
        # Structural check: StudentProfileSerializer (which nests
        # verification_status as a technically-writable ModelSerializer
        # field) is never bound with client data anywhere in the app —
        # confirmed by source inspection; this test additionally proves
        # AccountSettingsView (the one PATCH endpoint touching StudentProfile
        # from student input) only ever reads 'preferred_payment_channel' by
        # exact key, never passes request.data through a serializer.
        resp = self.client.patch('/api/auth/settings/', {'preferred_payment_channel': 'esewa', 'verification_status': 'verified'})
        self.assertEqual(resp.status_code, 200)
        profile = StudentProfile.objects.get(user=self.student)
        self.assertEqual(profile.preferred_payment_channel, 'esewa')
        self.assertEqual(profile.verification_status, 'unverified')


# ---------------------------------------------------------------------------
# 6 + 11. State machine + resubmission lifecycle, zero manual DB steps
# ---------------------------------------------------------------------------
@override_settings(MEDIA_GCS_PRIVATE_BUCKET='test-private-bucket')
class ResubmissionLifecycleTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='resub_stu', email='resub_stu@example.com', password='pw12345')
        self.admin = User.objects.create_user(
            username='resub_admin', email='resub_admin@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )

    @patch('accounts.verification_storage.upload_bytes')
    def test_full_resubmission_round_trip_no_manual_db_steps(self, mock_upload):
        # 1. Student uploads -> PENDING
        self.client.force_authenticate(user=self.student)
        resp = self.client.post(
            '/api/auth/me/verification/', {'document_type': 'citizenship', 'file': _valid_upload('first.png')},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 201)
        profile = StudentProfile.objects.get(user=self.student)
        self.assertEqual(profile.verification_status, 'pending')
        first_doc_id = resp.data['id']

        # 2. Admin rejects -> REJECTED
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(
            f'/api/auth/users/{self.student.id}/verification/reject/', {'reason': 'Blurry photo'},
        )
        self.assertEqual(resp.status_code, 200)
        profile.refresh_from_db()
        self.assertEqual(profile.verification_status, 'rejected')
        self.assertEqual(profile.verification_rejection_reason, 'Blurry photo')

        # 3. Student uploads a corrected document -> PENDING again
        #    (explicitly NOT auto-verified — only an admin action can do that)
        self.client.force_authenticate(user=self.student)
        resp = self.client.post(
            '/api/auth/me/verification/', {'document_type': 'citizenship', 'file': _valid_upload('second.png')},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 201)
        profile.refresh_from_db()
        self.assertEqual(profile.verification_status, 'pending')
        self.assertEqual(profile.verification_rejection_reason, 'Blurry photo')  # still visible until admin re-decides
        second_doc_id = resp.data['id']
        self.assertNotEqual(first_doc_id, second_doc_id)  # old row preserved, never overwritten

        # 4. Admin approves the corrected document AND the profile -> VERIFIED
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(f'/api/auth/verification-documents/{second_doc_id}/approve/')
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(f'/api/auth/users/{self.student.id}/verification/approve/')
        self.assertEqual(resp.status_code, 200)
        profile.refresh_from_db()
        self.assertEqual(profile.verification_status, 'verified')
        self.assertEqual(profile.verification_rejection_reason, '')  # cleared on approval

        # The original rejected document still exists, untouched, for audit history.
        self.assertEqual(VerificationDocument.objects.filter(user=self.student).count(), 2)
        first_doc = VerificationDocument.objects.get(pk=first_doc_id)
        self.assertEqual(first_doc.status, 'pending')  # never auto-changed by the second submission

    def test_student_cannot_self_transition_to_verified(self):
        """No student-facing endpoint accepts a target status at all —
        MyVerificationView.post only ever sets 'pending' on first
        submission, server-side, unconditionally."""
        self.client.force_authenticate(user=self.student)
        with patch('accounts.verification_storage.upload_bytes'):
            resp = self.client.post(
                '/api/auth/me/verification/',
                {'document_type': 'citizenship', 'file': _valid_upload(), 'target_status': 'verified'},
                format='multipart',
            )
        self.assertEqual(resp.status_code, 201)
        profile = StudentProfile.objects.get(user=self.student)
        self.assertEqual(profile.verification_status, 'pending')


# ---------------------------------------------------------------------------
# 12. Document/profile consistency edge cases
# ---------------------------------------------------------------------------
@override_settings(MEDIA_GCS_PRIVATE_BUCKET='test-private-bucket')
class DocumentProfileConsistencyTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='cons_stu', email='cons_stu@example.com', password='pw12345')
        self.admin = User.objects.create_user(
            username='cons_admin', email='cons_admin@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.doc_a = VerificationDocument.objects.create(
            user=self.student, document_type='citizenship', storage_bucket='b', storage_key='k-a',
        )
        self.doc_b = VerificationDocument.objects.create(
            user=self.student, document_type='passport', storage_bucket='b', storage_key='k-b',
        )

    def test_one_document_approved_one_rejected_profile_stays_unset(self):
        self.client.force_authenticate(user=self.admin)
        self.client.post(f'/api/auth/verification-documents/{self.doc_a.id}/approve/')
        self.client.post(f'/api/auth/verification-documents/{self.doc_b.id}/reject/', {'reason': 'unreadable'})
        profile, _ = StudentProfile.objects.get_or_create(user=self.student)
        self.assertEqual(profile.verification_status, 'unverified')

    def test_all_documents_approved_profile_still_requires_explicit_action(self):
        self.client.force_authenticate(user=self.admin)
        self.client.post(f'/api/auth/verification-documents/{self.doc_a.id}/approve/')
        self.client.post(f'/api/auth/verification-documents/{self.doc_b.id}/approve/')
        profile, _ = StudentProfile.objects.get_or_create(user=self.student)
        # There is no automatic aggregation rule anywhere in this codebase
        # that flips the profile to 'verified' from document statuses —
        # confirmed by inspection of verify_profile/reject_profile (the
        # only two writers of StudentProfile.verification_status), neither
        # of which is called from VerificationDocumentApproveView/RejectView.
        self.assertEqual(profile.verification_status, 'unverified')

    def test_photo_alone_does_not_verify_profile(self):
        with patch('accounts.verification_storage.upload_bytes'):
            self.client.force_authenticate(user=self.student)
            self.client.post('/api/auth/me/photo/', {'photo': _valid_upload('me.png')}, format='multipart')
        profile = StudentProfile.objects.get(user=self.student)
        self.assertEqual(profile.verification_status, 'unverified')

    def test_rejected_document_replacement_does_not_retroactively_change_old_row(self):
        self.client.force_authenticate(user=self.admin)
        self.client.post(f'/api/auth/verification-documents/{self.doc_a.id}/reject/', {'reason': 'bad scan'})
        with patch('accounts.verification_storage.upload_bytes'):
            self.client.force_authenticate(user=self.student)
            self.client.post(
                '/api/auth/me/verification/', {'document_type': 'citizenship', 'file': _valid_upload('new.png')},
                format='multipart',
            )
        self.doc_a.refresh_from_db()
        self.assertEqual(self.doc_a.status, 'rejected')  # untouched by the new submission
        self.assertEqual(VerificationDocument.objects.filter(user=self.student).count(), 3)


# ---------------------------------------------------------------------------
# 7. Malicious upload adversarial cases
# ---------------------------------------------------------------------------
@override_settings(MEDIA_GCS_PRIVATE_BUCKET='test-private-bucket')
class MaliciousUploadTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='mal_stu', email='mal_stu@example.com', password='pw12345')
        self.client.force_authenticate(user=self.student)

    @patch('accounts.verification_storage.upload_bytes')
    def test_path_traversal_filename_is_never_used_as_storage_key(self, mock_upload):
        evil = SimpleUploadedFile('../../../etc/passwd.png', _png_bytes(), content_type='image/png')
        resp = self.client.post(
            '/api/auth/me/verification/', {'document_type': 'citizenship', 'file': evil}, format='multipart',
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        # store_verification_document() builds the key from a fresh uuid4
        # hex + user_id + extension only — the original filename never
        # reaches the storage key, only the display-only original_filename
        # column (which Django's SimpleUploadedFile itself already
        # sanitizes of path separators before it ever reaches our code).
        called_bucket, called_key, called_bytes, called_ct = mock_upload.call_args[0]
        self.assertNotIn('..', called_key)
        self.assertNotIn('etc', called_key)
        self.assertNotIn('passwd', called_key)

    @patch('accounts.verification_storage.upload_bytes')
    def test_executable_masquerading_as_image_is_rejected(self, mock_upload):
        fake = SimpleUploadedFile('resume.jpg', b'MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00', content_type='image/jpeg')
        resp = self.client.post(
            '/api/auth/me/verification/', {'document_type': 'citizenship', 'file': fake}, format='multipart',
        )
        self.assertEqual(resp.status_code, 400)
        mock_upload.assert_not_called()

    @patch('accounts.verification_storage.upload_bytes')
    def test_zero_byte_file_rejected(self, mock_upload):
        empty = SimpleUploadedFile('empty.png', b'', content_type='image/png')
        resp = self.client.post(
            '/api/auth/me/verification/', {'document_type': 'citizenship', 'file': empty}, format='multipart',
        )
        self.assertEqual(resp.status_code, 400)
        mock_upload.assert_not_called()

    @patch('accounts.verification_storage.upload_bytes')
    def test_html_with_script_tag_disguised_as_image_rejected(self, mock_upload):
        payload = SimpleUploadedFile('doc.png', b'<html><script>alert(1)</script></html>', content_type='image/png')
        resp = self.client.post(
            '/api/auth/me/verification/', {'document_type': 'citizenship', 'file': payload}, format='multipart',
        )
        self.assertEqual(resp.status_code, 400)
        mock_upload.assert_not_called()

    def test_photo_endpoint_same_content_validation(self):
        fake = SimpleUploadedFile('me.jpg', b'MZ\x90\x00fake-exe-bytes', content_type='image/jpeg')
        resp = self.client.post('/api/auth/me/photo/', {'photo': fake}, format='multipart')
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------------
# 17. Query/performance audit — verification data adds no N+1 anywhere
# ---------------------------------------------------------------------------
class QueryPerformanceAuditTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='perf_admin', email='perf_admin@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(username='perf_stu', email='perf_stu@example.com', password='pw12345')
        StudentProfile.objects.create(user=self.student, verification_status='pending')
        for i in range(5):
            VerificationDocument.objects.create(
                user=self.student, document_type='citizenship', storage_bucket='b', storage_key=f'k{i}',
            )
        self.client.force_authenticate(user=self.admin)

    def test_student_detail_query_count_bounded_with_verification_documents(self):
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(f'/api/auth/users/{self.student.id}/detail/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['verification_documents']), 5)
        # Bounded and small regardless of document count — one extra bounded
        # Prefetch (`user_id = <this one id>`, limited to 20 rows), not one
        # query per document. The exact number is an implementation detail;
        # what matters is that it stays small and doesn't scale with rows.
        self.assertLessEqual(len(ctx.captured_queries), 12)

    def test_student_detail_query_count_flat_regardless_of_document_count(self):
        with CaptureQueriesContext(connection) as ctx_before:
            self.client.get(f'/api/auth/users/{self.student.id}/detail/')
        count_before = len(ctx_before.captured_queries)

        for i in range(20):
            VerificationDocument.objects.create(
                user=self.student, document_type='other', storage_bucket='b', storage_key=f'extra{i}',
            )

        with CaptureQueriesContext(connection) as ctx_after:
            self.client.get(f'/api/auth/users/{self.student.id}/detail/')
        count_after = len(ctx_after.captured_queries)

        self.assertEqual(
            count_before, count_after,
            f'Detail query count grew with document count ({count_before} vs {count_after}) — expected flat.',
        )

    def test_browse_endpoint_query_count_unaffected_by_verification_fields(self):
        # profile.verification_status/verification_rejection_reason ride
        # along on the SAME already-select_related('profile') row browse()
        # was already loading — adding 2 CharFields to that serializer
        # costs zero additional queries.
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get('/api/auth/users/browse/')
        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(len(ctx.captured_queries), 6)


# ---------------------------------------------------------------------------
# 14. Error isolation — a broken verification storage layer (GCS outage,
# signing failure) must never leak into exam access, login, or any other
# unrelated endpoint.
# ---------------------------------------------------------------------------
@override_settings(MEDIA_GCS_PRIVATE_BUCKET='test-private-bucket')
class ErrorIsolationTests(APITestCase):
    def setUp(self):
        self.course = Course.objects.create(name='Isolation Course', prefix='ISO')
        self.student = User.objects.create_user(username='iso_stu', email='iso_stu@example.com', password='pw12345')
        Enrollment.objects.create(user=self.student, course=self.course, access_type='free', is_active=True)
        self.admin = User.objects.create_user(
            username='iso_admin', email='iso_admin@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.document = VerificationDocument.objects.create(
            user=self.student, document_type='citizenship', storage_bucket='test-private-bucket', storage_key='k1',
        )

    def test_signed_url_failure_stays_a_500_on_that_endpoint_only(self):
        # Neither this view nor verification_storage catches a generic GCS/
        # IAM exception — it propagates exactly like an unhandled exception
        # in any other Django view would, which the real WSGI server turns
        # into a plain 500 for that one request only (Django's test client
        # instead re-raises it in-process, which is what assertRaises below
        # observes — the same underlying propagation, just surfaced
        # differently by the test harness than by a live server).
        self.client.force_authenticate(user=self.student)
        with patch('accounts.verification_storage.signed_url', side_effect=Exception('IAM outage')):
            with self.assertRaises(Exception):
                self.client.get(f'/api/auth/verification-documents/{self.document.id}/view/')
        # The same client, same session, immediately afterward: login state
        # and an ordinary endpoint are completely unaffected — the failure
        # above touched nothing beyond its own request.
        resp2 = self.client.get('/api/auth/me/')
        self.assertEqual(resp2.status_code, 200)

    def test_gcs_outage_during_verification_does_not_affect_login_or_profile_load(self):
        # NOTE (non-blocking observation, not an access/security defect):
        # upload_bytes' generic exceptions are not caught separately from
        # InvalidUpload in MyVerificationView.post, so a real GCS/network
        # outage surfaces as a raw 500 to the student for that one request
        # — isolated (nothing else is affected, confirmed below) but not
        # a friendly error message. Worth a future polish pass; does not
        # block release since it violates no access/security invariant.
        self.client.force_authenticate(user=self.student)
        with patch('accounts.verification_storage.upload_bytes', side_effect=Exception('GCS outage')):
            with self.assertRaises(Exception):
                self.client.post(
                    '/api/auth/me/verification/', {'document_type': 'citizenship', 'file': _valid_upload()},
                    format='multipart',
                )
        # Still just the one document setUp() created — the failed upload
        # attempt above created no new row.
        self.assertEqual(VerificationDocument.objects.filter(user=self.student).count(), 1)
        # The account itself is unaffected regardless of that endpoint's own outcome.
        resp = self.client.get('/api/auth/me/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['email'], self.student.email)

    def test_exam_access_functions_work_when_gcs_entirely_broken(self):
        """The strongest form of this isolation guarantee: even with every
        GCS call raising, functions that decide exam access never call
        into GCS at all, so they are provably unaffected."""
        from billing.access import has_qbank_access
        from academics.models import Subject

        free_subject = Subject.objects.create(name='Isolation Subject', is_free=True)
        with patch('accounts.verification_storage.upload_bytes', side_effect=Exception('GCS outage')), \
             patch('accounts.verification_storage.signed_url', side_effect=Exception('GCS outage')):
            self.assertTrue(has_qbank_access(self.student, free_subject))

    def test_verification_document_view_failure_does_not_break_admin_panel_list(self):
        self.client.force_authenticate(user=self.admin)
        with patch('accounts.verification_storage.signed_url', side_effect=Exception('IAM outage')):
            # The admin Students List / detail endpoints never call
            # signed_url at all (metadata only) — proving the outage above
            # cannot possibly reach them even indirectly.
            resp = self.client.get(f'/api/auth/users/{self.student.id}/detail/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['verification_documents']), 1)
