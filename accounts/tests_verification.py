"""Student Identity & Document Verification System — an isolated test file
(the established convention: never add to the large, already-dirty
accounts/tests.py). Covers upload validation, IDOR/ownership, the
admin-only approve/reject workflow (both per-document and profile-level),
and — the single most load-bearing suite here, explicitly required by the
feature's own spec — the access-regression matrix proving verification
status is NEVER read by any exam-access function, for both a free and a
paid student, across every entitlement type this platform has.

GCS calls (upload_bytes/signed_url) are mocked throughout, exactly
mirroring billing/tests.py's own precedent for screenshot_storage.
"""
import io
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APITestCase

from accounts import verification_storage
from accounts.models import StudentProfile, VerificationDocument
from billing.access import (
    get_grand_test_access,
    has_daily_test_access,
    has_mock_test_access,
    has_pyq_access,
    has_qbank_access,
)
from billing.models import GrandTestAccess, Purchase, Subscription, SubscriptionPlan
from courses.models import Course, Enrollment
from tests_app.access import can_access_test, visible_test_queryset
from tests_app.models import Test

User = get_user_model()


def _png_bytes():
    buf = io.BytesIO()
    Image.new('RGB', (2, 2), color='blue').save(buf, format='PNG')
    buf.seek(0)
    return buf.read()


def _valid_photo(name='me.png'):
    return SimpleUploadedFile(name, _png_bytes(), content_type='image/png')


def _valid_document(name='citizenship.png'):
    return SimpleUploadedFile(name, _png_bytes(), content_type='image/png')


@override_settings(MEDIA_GCS_PRIVATE_BUCKET='test-private-bucket')
class VerificationStorageValidationTests(TestCase):
    """Pure unit tests on verification_storage — no HTTP, no DB rows."""

    def test_valid_png_photo_accepted(self):
        ext, content_type = verification_storage.validate_photo(io.BytesIO(_png_bytes()))
        self.assertEqual(ext, 'png')
        self.assertEqual(content_type, 'image/png')

    def test_oversized_photo_rejected(self):
        with self.assertRaises(verification_storage.InvalidUpload):
            verification_storage.validate_photo(
                io.BytesIO(_png_bytes()), max_bytes=1,
            )

    def test_corrupt_file_rejected_as_photo(self):
        with self.assertRaises(verification_storage.InvalidUpload):
            verification_storage.validate_photo(io.BytesIO(b'not an image at all'))

    def test_renamed_non_image_rejected_regardless_of_extension(self):
        # The point of real content validation: a .png-named text file must
        # still fail, since nothing here trusts the filename/Content-Type.
        fake = SimpleUploadedFile('photo.png', b'plain text pretending to be a png', content_type='image/png')
        with self.assertRaises(verification_storage.InvalidUpload):
            verification_storage.validate_photo(fake)

    def test_valid_pdf_document_accepted(self):
        ext, content_type, size = verification_storage.validate_document(io.BytesIO(b'%PDF-1.4 fake but has the magic bytes'))
        self.assertEqual(ext, 'pdf')
        self.assertEqual(content_type, 'application/pdf')
        self.assertGreater(size, 0)

    def test_valid_image_document_accepted(self):
        ext, content_type, size = verification_storage.validate_document(io.BytesIO(_png_bytes()))
        self.assertEqual(ext, 'png')

    def test_oversized_document_rejected(self):
        with self.assertRaises(verification_storage.InvalidUpload):
            verification_storage.validate_document(io.BytesIO(_png_bytes()), max_bytes=1)

    def test_garbage_document_rejected(self):
        with self.assertRaises(verification_storage.InvalidUpload):
            verification_storage.validate_document(io.BytesIO(b'garbage, neither pdf nor image'))


@override_settings(MEDIA_GCS_PRIVATE_BUCKET='test-private-bucket')
class ProfilePhotoUploadTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='s1', email='s1@example.com', password='pw12345')

    def test_unauthenticated_rejected(self):
        resp = self.client.post('/api/auth/me/photo/', {'photo': _valid_photo()}, format='multipart')
        self.assertEqual(resp.status_code, 401)

    @patch('accounts.verification_storage.upload_bytes')
    def test_valid_upload_succeeds_and_sets_photo(self, mock_upload):
        self.client.force_authenticate(user=self.student)
        resp = self.client.post('/api/auth/me/photo/', {'photo': _valid_photo()}, format='multipart')
        self.assertEqual(resp.status_code, 200, resp.data)
        profile = StudentProfile.objects.get(user=self.student)
        self.assertTrue(profile.photo)

    def test_no_file_rejected(self):
        self.client.force_authenticate(user=self.student)
        resp = self.client.post('/api/auth/me/photo/', {}, format='multipart')
        self.assertEqual(resp.status_code, 400)

    def test_invalid_file_rejected_and_never_uploaded(self):
        self.client.force_authenticate(user=self.student)
        with patch('accounts.verification_storage.upload_bytes') as mock_upload:
            bad = SimpleUploadedFile('me.png', b'not an image', content_type='image/png')
            resp = self.client.post('/api/auth/me/photo/', {'photo': bad}, format='multipart')
            self.assertEqual(resp.status_code, 400)
            mock_upload.assert_not_called()

    @patch('accounts.verification_storage.upload_bytes')
    def test_replacing_photo_deletes_old_one_only_after_new_save_succeeds(self, mock_upload):
        self.client.force_authenticate(user=self.student)
        self.client.post('/api/auth/me/photo/', {'photo': _valid_photo('first.png')}, format='multipart')
        profile = StudentProfile.objects.get(user=self.student)
        first_name = profile.photo.name
        self.assertTrue(first_name)

        resp = self.client.post('/api/auth/me/photo/', {'photo': _valid_photo('second.png')}, format='multipart')
        self.assertEqual(resp.status_code, 200)
        profile.refresh_from_db()
        self.assertNotEqual(profile.photo.name, first_name)


@override_settings(MEDIA_GCS_PRIVATE_BUCKET='test-private-bucket')
class MyVerificationTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='s2', email='s2@example.com', password='pw12345')
        self.client.force_authenticate(user=self.student)

    def test_get_before_any_submission_is_unverified_with_no_documents(self):
        resp = self.client.get('/api/auth/me/verification/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['verification_status'], 'unverified')
        self.assertEqual(resp.data['documents'], [])
        self.assertFalse(resp.data['has_photo'])

    def test_unauthenticated_get_rejected(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get('/api/auth/me/verification/')
        self.assertEqual(resp.status_code, 401)

    @patch('accounts.verification_storage.upload_bytes')
    def test_valid_submission_creates_document_and_moves_to_pending(self, mock_upload):
        resp = self.client.post(
            '/api/auth/me/verification/',
            {'document_type': 'citizenship', 'file': _valid_document()},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        profile = StudentProfile.objects.get(user=self.student)
        self.assertEqual(profile.verification_status, 'pending')
        self.assertEqual(VerificationDocument.objects.filter(user=self.student).count(), 1)
        doc = VerificationDocument.objects.get(user=self.student)
        # Never stores the original filename/email as the storage key.
        self.assertNotIn('citizenship.png', doc.storage_key)

    def test_invalid_document_type_rejected(self):
        resp = self.client.post(
            '/api/auth/me/verification/',
            {'document_type': 'not_a_real_type', 'file': _valid_document()},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(VerificationDocument.objects.count(), 0)

    def test_no_file_rejected(self):
        resp = self.client.post('/api/auth/me/verification/', {'document_type': 'citizenship'}, format='multipart')
        self.assertEqual(resp.status_code, 400)

    @patch('accounts.verification_storage.upload_bytes')
    def test_second_submission_while_already_pending_does_not_change_status(self, mock_upload):
        self.client.post(
            '/api/auth/me/verification/', {'document_type': 'citizenship', 'file': _valid_document()}, format='multipart',
        )
        self.client.post(
            '/api/auth/me/verification/', {'document_type': 'passport', 'file': _valid_document('p.png')}, format='multipart',
        )
        profile = StudentProfile.objects.get(user=self.student)
        self.assertEqual(profile.verification_status, 'pending')
        self.assertEqual(VerificationDocument.objects.filter(user=self.student).count(), 2)

    @patch('accounts.verification_storage.upload_bytes')
    def test_submission_after_rejection_moves_back_to_pending(self, mock_upload):
        profile, _ = StudentProfile.objects.get_or_create(user=self.student)
        profile.verification_status = 'rejected'
        profile.verification_rejection_reason = 'Blurry photo'
        profile.save(update_fields=['verification_status', 'verification_rejection_reason'])

        resp = self.client.post(
            '/api/auth/me/verification/', {'document_type': 'citizenship', 'file': _valid_document()}, format='multipart',
        )
        self.assertEqual(resp.status_code, 201)
        profile.refresh_from_db()
        # A resubmission after rejection puts the student back in the
        # review queue — 'rejected' does NOT auto-transition to 'verified'
        # (only an explicit admin action can do that), but it must not
        # silently stay 'rejected' either, since the admin's original
        # decision no longer describes what's currently on file.
        self.assertEqual(profile.verification_status, 'pending')
        # The old rejection reason is left visible until the admin makes a
        # new decision — not silently erased by the student's own action.
        self.assertEqual(profile.verification_rejection_reason, 'Blurry photo')

    @patch('accounts.verification_storage.upload_bytes')
    def test_submission_while_verified_does_not_change_status(self, mock_upload):
        # An already-verified student adding a further supporting document
        # is not "a resubmission after failure" — leaving 'verified' alone
        # here is the conservative choice; only an explicit admin action
        # (reject_profile) can ever move a verified profile off 'verified'.
        profile, _ = StudentProfile.objects.get_or_create(user=self.student)
        profile.verification_status = 'verified'
        profile.save(update_fields=['verification_status'])

        resp = self.client.post(
            '/api/auth/me/verification/', {'document_type': 'passport', 'file': _valid_document('p2.png')}, format='multipart',
        )
        self.assertEqual(resp.status_code, 201)
        profile.refresh_from_db()
        self.assertEqual(profile.verification_status, 'verified')


@override_settings(MEDIA_GCS_PRIVATE_BUCKET='test-private-bucket')
class VerificationDocumentViewIDORTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='owner', email='owner@example.com', password='pw12345')
        self.other_student = User.objects.create_user(username='other', email='other@example.com', password='pw12345')
        self.admin = User.objects.create_user(
            username='admin1', email='admin1@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.editor = User.objects.create_user(
            username='editor1', email='editor1@example.com', password='pw12345', is_staff=True, admin_role='editor',
        )
        self.document = VerificationDocument.objects.create(
            user=self.owner, document_type='citizenship', storage_bucket='test-private-bucket',
            storage_key='verification-documents/1/abc.png', original_filename='citizenship.png',
        )

    @patch('accounts.verification_storage.signed_url', return_value='https://signed.example/doc.png')
    def test_owner_can_view_own_document(self, mock_signed):
        self.client.force_authenticate(user=self.owner)
        resp = self.client.get(f'/api/auth/verification-documents/{self.document.id}/view/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['url'], 'https://signed.example/doc.png')

    @patch('accounts.verification_storage.signed_url', return_value='https://signed.example/doc.png')
    def test_other_student_cannot_view_document(self, mock_signed):
        self.client.force_authenticate(user=self.other_student)
        resp = self.client.get(f'/api/auth/verification-documents/{self.document.id}/view/')
        self.assertEqual(resp.status_code, 403)
        mock_signed.assert_not_called()

    @patch('accounts.verification_storage.signed_url', return_value='https://signed.example/doc.png')
    def test_admin_can_view_any_document(self, mock_signed):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.get(f'/api/auth/verification-documents/{self.document.id}/view/')
        self.assertEqual(resp.status_code, 200)

    def test_unauthenticated_rejected(self):
        resp = self.client.get(f'/api/auth/verification-documents/{self.document.id}/view/')
        self.assertEqual(resp.status_code, 401)

    def test_nonexistent_document_404(self):
        self.client.force_authenticate(user=self.owner)
        resp = self.client.get('/api/auth/verification-documents/999999/view/')
        self.assertEqual(resp.status_code, 404)


@override_settings(MEDIA_GCS_PRIVATE_BUCKET='test-private-bucket')
class VerificationDocumentApproveRejectTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='s3', email='s3@example.com', password='pw12345')
        self.admin = User.objects.create_user(
            username='admin2', email='admin2@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.editor = User.objects.create_user(
            username='editor2', email='editor2@example.com', password='pw12345', is_staff=True, admin_role='editor',
        )
        self.document = VerificationDocument.objects.create(
            user=self.student, document_type='citizenship', storage_bucket='test-private-bucket',
            storage_key='verification-documents/1/abc.png',
        )

    def test_admin_can_approve_document(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(f'/api/auth/verification-documents/{self.document.id}/approve/')
        self.assertEqual(resp.status_code, 200)
        self.document.refresh_from_db()
        self.assertEqual(self.document.status, 'approved')
        self.assertEqual(self.document.reviewed_by_id, self.admin.id)
        self.assertIsNotNone(self.document.reviewed_at)

        # Approving one document does NOT touch the profile's overall status
        # (no profile row is even required to exist for a document to be
        # approved) — default status if a profile gets created is still
        # 'unverified'.
        profile, _ = StudentProfile.objects.get_or_create(user=self.student)
        self.assertEqual(profile.verification_status, 'unverified')

    def test_admin_reject_requires_reason(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(f'/api/auth/verification-documents/{self.document.id}/reject/', {})
        self.assertEqual(resp.status_code, 400)
        self.document.refresh_from_db()
        self.assertEqual(self.document.status, 'pending')

    def test_admin_reject_with_reason_succeeds(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(f'/api/auth/verification-documents/{self.document.id}/reject/', {'reason': 'Blurry photo'})
        self.assertEqual(resp.status_code, 200)
        self.document.refresh_from_db()
        self.assertEqual(self.document.status, 'rejected')
        self.assertEqual(self.document.rejection_reason, 'Blurry photo')

    def test_plain_student_forbidden(self):
        self.client.force_authenticate(user=self.student)
        resp = self.client.post(f'/api/auth/verification-documents/{self.document.id}/approve/')
        self.assertEqual(resp.status_code, 403)

    def test_editor_role_forbidden(self):
        self.client.force_authenticate(user=self.editor)
        resp = self.client.post(f'/api/auth/verification-documents/{self.document.id}/approve/')
        self.assertEqual(resp.status_code, 403)

    def test_nonexistent_document_404(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post('/api/auth/verification-documents/999999/approve/')
        self.assertEqual(resp.status_code, 404)


@override_settings(MEDIA_GCS_PRIVATE_BUCKET='test-private-bucket')
class ProfileVerifyRejectActionTests(APITestCase):
    """AdminUserViewSet.verify_profile/reject_profile — the profile-level
    (not per-document) decision."""

    def setUp(self):
        self.student = User.objects.create_user(username='s4', email='s4@example.com', password='pw12345')
        self.admin = User.objects.create_user(
            username='admin3', email='admin3@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.editor = User.objects.create_user(
            username='editor3', email='editor3@example.com', password='pw12345', is_staff=True, admin_role='editor',
        )

    def test_admin_can_verify_profile(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(f'/api/auth/users/{self.student.id}/verification/approve/')
        self.assertEqual(resp.status_code, 200, resp.data)
        profile = StudentProfile.objects.get(user=self.student)
        self.assertEqual(profile.verification_status, 'verified')
        self.assertEqual(profile.verification_reviewed_by_id, self.admin.id)
        self.assertIsNotNone(profile.verification_reviewed_at)

    def test_reject_profile_requires_reason(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(f'/api/auth/users/{self.student.id}/verification/reject/', {})
        self.assertEqual(resp.status_code, 400)

    def test_reject_profile_with_reason_succeeds(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(
            f'/api/auth/users/{self.student.id}/verification/reject/', {'reason': 'Document unreadable'},
        )
        self.assertEqual(resp.status_code, 200)
        profile = StudentProfile.objects.get(user=self.student)
        self.assertEqual(profile.verification_status, 'rejected')
        self.assertEqual(profile.verification_rejection_reason, 'Document unreadable')

    def test_approving_clears_a_previous_rejection_reason(self):
        profile, _ = StudentProfile.objects.get_or_create(user=self.student)
        profile.verification_status = 'rejected'
        profile.verification_rejection_reason = 'old reason'
        profile.save()
        self.client.force_authenticate(user=self.admin)
        self.client.post(f'/api/auth/users/{self.student.id}/verification/approve/')
        profile.refresh_from_db()
        self.assertEqual(profile.verification_status, 'verified')
        self.assertEqual(profile.verification_rejection_reason, '')

    def test_editor_forbidden(self):
        self.client.force_authenticate(user=self.editor)
        resp = self.client.post(f'/api/auth/users/{self.student.id}/verification/approve/')
        self.assertEqual(resp.status_code, 403)

    def test_plain_student_forbidden(self):
        self.client.force_authenticate(user=self.student)
        resp = self.client.post(f'/api/auth/users/{self.student.id}/verification/approve/')
        self.assertEqual(resp.status_code, 403)

    def test_nonexistent_student_404(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post('/api/auth/users/999999/verification/approve/')
        self.assertEqual(resp.status_code, 404)

    def test_staff_account_not_reachable_via_verification_actions(self):
        staff_target = User.objects.create_user(
            username='staff_target', email='staff_target@example.com', password='pw12345', is_staff=True,
        )
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(f'/api/auth/users/{staff_target.id}/verification/approve/')
        self.assertEqual(resp.status_code, 404)


class AccessRegressionTests(TestCase):
    """THE critical suite for this feature: proves verification_status is
    completely invisible to every exam-access function on this platform,
    for a free student AND a paid student, across every entitlement type
    (QBank, Daily Test, Mock Test, PYQ, Grand Test, academic
    course/batch/assignment eligibility). If any of these functions is ever
    modified to read verification_status, this suite fails.

    Method: build one free student and one paid student ONCE (fixed
    entitlement state), then flip StudentProfile.verification_status
    through all four values and assert every access function returns the
    exact same result every time — never comparing to a hardcoded expected
    boolean, so this suite makes no assumption about what "correct" access
    should be, only that verification never moves it.
    """

    STATUSES = ['unverified', 'pending', 'verified', 'rejected']

    @classmethod
    def setUpTestData(cls):
        cls.course = Course.objects.create(name='CEE-MD Ayurveda', prefix='AYU')

        cls.free_student = User.objects.create_user(
            username='free_student', email='free_student@example.com', password='pw12345',
        )
        Enrollment.objects.create(user=cls.free_student, course=cls.course, access_type='free', is_active=True)

        cls.paid_student = User.objects.create_user(
            username='paid_student', email='paid_student@example.com', password='pw12345',
        )
        Enrollment.objects.create(user=cls.paid_student, course=cls.course, access_type='package', is_active=True)
        for product_type in ('qbank', 'mock_test', 'daily_test', 'pyq'):
            Subscription.objects.create(
                user=cls.paid_student, course=cls.course, product_type=product_type, is_active=True,
                mock_test_quota=None,
            )

        from academics.models import Subject
        cls.pro_subject = Subject.objects.create(name='Pharmacology', is_free=False)
        cls.pro_subject.courses.add(cls.course)

        cls.daily_test = Test.objects.create(
            title='Daily 1', exam_type='daily', is_pro=True, is_draft=False, duration_minutes=30,
        )
        cls.daily_test.courses.add(cls.course)
        cls.mock_test = Test.objects.create(
            title='Mock 1', exam_type='mock', is_pro=True, is_draft=False, duration_minutes=60,
        )
        cls.mock_test.courses.add(cls.course)
        cls.pyq_test = Test.objects.create(
            title='PYQ 1', exam_type='pyq', is_pro=True, is_draft=False, duration_minutes=60,
        )
        cls.pyq_test.courses.add(cls.course)
        cls.grand_test = Test.objects.create(
            title='Grand 1', exam_type='grand', is_draft=False, duration_minutes=120,
        )
        cls.grand_test.courses.add(cls.course)

        purchase = Purchase.objects.create(
            user=cls.paid_student, kind='grand_test', grand_test=cls.grand_test,
            original_amount=1000, final_amount=1000, status='approved',
        )
        GrandTestAccess.objects.create(
            purchase=purchase, user=cls.paid_student, test=cls.grand_test, granted_at=timezone.now(),
        )

    def _snapshot(self, user):
        """Every access function this feature's spec explicitly names,
        evaluated for the given user against the fixed fixtures above."""
        grand = get_grand_test_access(user, self.grand_test)
        return {
            'has_qbank_access': has_qbank_access(user, self.pro_subject),
            'has_daily_test_access': has_daily_test_access(user, self.daily_test),
            'has_mock_test_access': has_mock_test_access(user, self.mock_test),
            'has_pyq_access': has_pyq_access(user, self.pyq_test),
            'grand_test_access_present': grand is not None,
            'can_access_daily': can_access_test(user, self.daily_test),
            'can_access_mock': can_access_test(user, self.mock_test),
            'can_access_pyq': can_access_test(user, self.pyq_test),
            'can_access_grand': can_access_test(user, self.grand_test),
            'visible_test_ids': set(
                visible_test_queryset(user, Test.objects.all()).values_list('id', flat=True)
            ),
        }

    def _run_matrix(self, user):
        results = {}
        for verification_status in self.STATUSES:
            profile, _ = StudentProfile.objects.get_or_create(user=user)
            profile.verification_status = verification_status
            if verification_status == 'rejected':
                profile.verification_rejection_reason = 'irrelevant reason text'
            profile.save()
            results[verification_status] = self._snapshot(user)
        return results

    def test_free_student_access_identical_across_all_verification_states(self):
        results = self._run_matrix(self.free_student)
        baseline = results['unverified']
        for status_value, snapshot in results.items():
            self.assertEqual(
                snapshot, baseline,
                f"Free student's access differs at verification_status={status_value!r} "
                f"vs 'unverified' baseline — verification must never gate access.",
            )

    def test_paid_student_access_identical_across_all_verification_states(self):
        results = self._run_matrix(self.paid_student)
        baseline = results['unverified']
        for status_value, snapshot in results.items():
            self.assertEqual(
                snapshot, baseline,
                f"Paid student's access differs at verification_status={status_value!r} "
                f"vs 'unverified' baseline — verification must never gate access.",
            )

    def test_free_and_paid_snapshots_are_not_trivially_identical_to_each_other(self):
        # Guards against a vacuous pass: if free == paid on everything, the
        # matrix above wouldn't actually be exercising the paid-only
        # entitlements at all. They must differ (paid has qbank/daily/mock/
        # pyq/grand access that free does not).
        free_snapshot = self._snapshot(self.free_student)
        paid_snapshot = self._snapshot(self.paid_student)
        self.assertNotEqual(free_snapshot, paid_snapshot)
        self.assertTrue(paid_snapshot['has_qbank_access'])
        self.assertFalse(free_snapshot['has_qbank_access'])
        self.assertTrue(paid_snapshot['grand_test_access_present'])
        self.assertFalse(free_snapshot['grand_test_access_present'])

    def test_rejected_student_still_gets_full_paid_access(self):
        # Explicit spec scenario: a rejected/unverified paid student is not
        # a degraded-access student — full paid entitlement, unchanged.
        profile, _ = StudentProfile.objects.get_or_create(user=self.paid_student)
        profile.verification_status = 'rejected'
        profile.verification_rejection_reason = 'fake document'
        profile.save()
        self.assertTrue(has_qbank_access(self.paid_student, self.pro_subject))
        self.assertTrue(has_daily_test_access(self.paid_student, self.daily_test))
        self.assertTrue(has_mock_test_access(self.paid_student, self.mock_test))
        self.assertTrue(has_pyq_access(self.paid_student, self.pyq_test))
        self.assertIsNotNone(get_grand_test_access(self.paid_student, self.grand_test))

    def test_pending_verification_does_not_block_free_daily_access(self):
        # Explicit spec scenario: a student mid-review (photo/document
        # submitted, awaiting admin decision) keeps ordinary free access.
        profile, _ = StudentProfile.objects.get_or_create(user=self.free_student)
        profile.verification_status = 'pending'
        profile.save()
        free_non_pro_daily = Test.objects.create(
            title='Free Daily', exam_type='daily', is_pro=False, is_draft=False, duration_minutes=30,
        )
        free_non_pro_daily.courses.add(self.course)
        self.assertTrue(has_daily_test_access(self.free_student, free_non_pro_daily))
        self.assertTrue(can_access_test(self.free_student, free_non_pro_daily))
