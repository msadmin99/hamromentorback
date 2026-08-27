from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from core.models import DeletionAuditLog
from media_library.models import MediaAsset
from media_library.service import delete_media_asset

User = get_user_model()


def _make_asset(**overrides):
    defaults = dict(
        image_type='question_image',
        storage_key='questions/img_1/',
        bucket='private-bucket',
        format='WEBP',
        variants={
            '480': 'private/questions/img_1/480.webp',
            '480_public': 'public/questions/img_1/480.webp',
        },
    )
    defaults.update(overrides)
    return MediaAsset.objects.create(**defaults)


class DeleteMediaAssetServiceTests(TestCase):
    @override_settings(MEDIA_GCS_PUBLIC_BUCKET='public-bucket', MEDIA_GCS_PRIVATE_BUCKET='private-bucket')
    @patch('media_library.service.delete_object')
    def test_deletes_gcs_objects_when_not_shared(self, mock_delete_object):
        asset = _make_asset()
        asset_id = asset.id

        result = delete_media_asset(asset)

        self.assertTrue(result)
        self.assertFalse(MediaAsset.objects.filter(id=asset_id).exists())
        called_keys = {call.args[1] for call in mock_delete_object.call_args_list}
        self.assertIn('questions/img_1/original.webp', called_keys)
        self.assertIn('private/questions/img_1/480.webp', called_keys)
        self.assertIn('public/questions/img_1/480.webp', called_keys)

    @patch('media_library.service.delete_object')
    def test_skips_gcs_delete_when_storage_key_is_shared(self, mock_delete_object):
        """Content-hash dedup: two MediaAsset rows can point at the same GCS
        objects. Deleting one must remove only its DB row, never the shared
        files still referenced by the other."""
        shared_key = 'questions/img_shared/'
        first = _make_asset(storage_key=shared_key)
        second = _make_asset(storage_key=shared_key)

        result = delete_media_asset(first)

        self.assertFalse(result)
        mock_delete_object.assert_not_called()
        self.assertFalse(MediaAsset.objects.filter(id=first.id).exists())
        self.assertTrue(MediaAsset.objects.filter(id=second.id).exists())

    @patch('media_library.service.delete_object', side_effect=Exception('gcs unavailable'))
    def test_gcs_failure_does_not_block_db_deletion(self, mock_delete_object):
        asset = _make_asset()
        asset_id = asset.id

        delete_media_asset(asset)  # must not raise

        self.assertFalse(MediaAsset.objects.filter(id=asset_id).exists())


class MediaAssetDetailViewDeleteTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='staff1', email='staff1@example.com', password='pw12345', is_staff=True, admin_role='admin',
        )
        self.student = User.objects.create_user(username='student1', email='student1@example.com', password='pw12345')

    @patch('media_library.views.delete_media_asset')
    def test_delete_requires_staff(self, mock_delete):
        asset = _make_asset()
        self.client.force_authenticate(user=self.student)

        resp = self.client.delete(f'/api/media/{asset.id}/')

        self.assertEqual(resp.status_code, 403)
        mock_delete.assert_not_called()
        self.assertTrue(MediaAsset.objects.filter(id=asset.id).exists())

    @patch('media_library.views.delete_media_asset')
    def test_delete_success_returns_confirmation_and_logs_audit(self, mock_delete):
        asset = _make_asset(original_filename='xray.jpg')
        self.client.force_authenticate(user=self.staff)

        resp = self.client.delete(f'/api/media/{asset.id}/')

        self.assertEqual(resp.status_code, 200)
        self.assertIn('permanently removed', resp.data['detail'])
        mock_delete.assert_called_once()
        entry = DeletionAuditLog.objects.get(resource_type='MediaAsset', resource_id=str(asset.id))
        self.assertEqual(entry.result, 'success')

    @patch('media_library.views.delete_media_asset', side_effect=Exception('boom'))
    def test_delete_failure_returns_clean_error_and_logs_audit(self, mock_delete):
        asset = _make_asset()
        self.client.force_authenticate(user=self.staff)

        resp = self.client.delete(f'/api/media/{asset.id}/')

        self.assertEqual(resp.status_code, 500)
        self.assertIn('Deletion failed', resp.data['detail'])
        entry = DeletionAuditLog.objects.get(resource_type='MediaAsset', resource_id=str(asset.id))
        self.assertEqual(entry.result, 'failure')

    def test_delete_of_unknown_asset_returns_404(self):
        import uuid
        self.client.force_authenticate(user=self.staff)

        resp = self.client.delete(f'/api/media/{uuid.uuid4()}/')

        self.assertEqual(resp.status_code, 404)


class PublicGCSStorageTests(TestCase):
    """The Django Storage adapter fixing the "payment QR code / any plain
    ImageField upload silently disappears on the next Cloud Run deploy"
    bug — Cloud Run's local disk is ephemeral, so anything saved with the
    default FileSystemStorage isn't durable in production."""

    @override_settings(MEDIA_GCS_PUBLIC_BUCKET='public-bucket')
    @patch('media_library.gcs_storage._bucket')
    @patch('media_library.gcs_storage.upload_bytes')
    def test_save_uploads_to_the_public_bucket(self, mock_upload, mock_bucket):
        from django.core.files.base import ContentFile

        from media_library.django_storage import PublicGCSStorage

        # Storage.save() calls get_available_name() -> exists() first (name-
        # collision check) before _save() — not under test here, so just
        # make it report "doesn't exist yet" via the same _bucket() call
        # exists()/size() go through.
        mock_bucket.return_value.blob.return_value.exists.return_value = False

        storage = PublicGCSStorage()
        name = storage.save('payment_method_qr/fonepay.png', ContentFile(b'fake-png-bytes', name='fonepay.png'))

        self.assertEqual(name, 'payment_method_qr/fonepay.png')
        mock_upload.assert_called_once()
        bucket_arg, key_arg, data_arg, content_type_arg = mock_upload.call_args[0]
        self.assertEqual(bucket_arg, 'public-bucket')
        self.assertEqual(key_arg, 'payment_method_qr/fonepay.png')
        self.assertEqual(data_arg, b'fake-png-bytes')

    @override_settings(MEDIA_GCS_PUBLIC_BUCKET='public-bucket')
    def test_url_is_always_https_direct_to_gcs(self):
        from media_library.django_storage import PublicGCSStorage

        url = PublicGCSStorage().url('payment_method_qr/fonepay.png')

        self.assertEqual(url, 'https://storage.googleapis.com/public-bucket/payment_method_qr/fonepay.png')
        self.assertTrue(url.startswith('https://'))

    @override_settings(MEDIA_GCS_PUBLIC_BUCKET='public-bucket')
    @patch('media_library.gcs_storage.delete_object')
    def test_delete_removes_from_the_public_bucket(self, mock_delete):
        from media_library.django_storage import PublicGCSStorage

        PublicGCSStorage().delete('payment_method_qr/fonepay.png')

        mock_delete.assert_called_once_with('public-bucket', 'payment_method_qr/fonepay.png')

    def test_default_file_storage_falls_back_to_local_disk_without_a_bucket_configured(self):
        """MEDIA_GCS_PUBLIC_BUCKET is blank in local dev/CI (matching every
        other MEDIA_GCS_* setting's convention) — settings.py must not force
        GCS storage (and a real google-cloud-storage client) in that case."""
        from django.conf import settings

        self.assertEqual(settings.MEDIA_GCS_PUBLIC_BUCKET, '')
        self.assertEqual(getattr(settings, 'DEFAULT_FILE_STORAGE', ''), 'django.core.files.storage.FileSystemStorage')


class SignedUrlComputeEngineCredentialsTests(TestCase):
    """The exact bug behind "clicking View on a payment screenshot does
    nothing": Cloud Run's attached service account has no private key —
    blob.generate_signed_url() raises AttributeError with plain Compute
    Engine credentials ("just contains a token"), confirmed live in
    production. gcs_storage.signed_url() must sign via the IAM Credentials
    API (service_account_email + access_token) instead whenever the
    credentials don't carry a private key of their own."""

    def _mock_client_with_compute_engine_credentials(self, mock_client_fn):
        from unittest.mock import MagicMock

        credentials = MagicMock()
        credentials.valid = True
        credentials.token = 'fake-access-token'
        credentials.service_account_email = 'default-sa@example.iam.gserviceaccount.com'

        client = MagicMock()
        client._credentials = credentials
        mock_bucket = MagicMock()
        mock_blob = MagicMock()
        mock_blob.generate_signed_url.return_value = 'https://signed.example/payment_screenshots/proof.jpg'
        mock_bucket.blob.return_value = mock_blob
        client.bucket.return_value = mock_bucket
        mock_client_fn.return_value = client
        return mock_blob

    @patch('media_library.gcs_storage._client')
    def test_signs_via_iam_access_token_with_compute_engine_credentials(self, mock_client_fn):
        from media_library import gcs_storage

        mock_blob = self._mock_client_with_compute_engine_credentials(mock_client_fn)

        url = gcs_storage.signed_url('private-bucket', 'payment_screenshots/proof.jpg', expires_seconds=300)

        self.assertEqual(url, 'https://signed.example/payment_screenshots/proof.jpg')
        mock_blob.generate_signed_url.assert_called_once_with(
            version='v4', expiration=300, method='GET',
            service_account_email='default-sa@example.iam.gserviceaccount.com', access_token='fake-access-token',
        )

    @patch('media_library.gcs_storage._client')
    def test_falls_back_to_default_signing_without_a_service_account_email(self, mock_client_fn):
        """A local key-file-backed credential (e.g. a developer's own gcloud
        ADC) has no service_account_email attribute at all — must not break
        the normal case that already worked."""
        from unittest.mock import MagicMock

        from media_library import gcs_storage

        credentials = MagicMock(spec=['valid'])
        credentials.valid = True
        client = MagicMock()
        client._credentials = credentials
        mock_blob = MagicMock()
        mock_blob.generate_signed_url.return_value = 'https://signed.example/normal.jpg'
        client.bucket.return_value.blob.return_value = mock_blob
        mock_client_fn.return_value = client

        url = gcs_storage.signed_url('private-bucket', 'normal.jpg')

        self.assertEqual(url, 'https://signed.example/normal.jpg')
        mock_blob.generate_signed_url.assert_called_once_with(version='v4', expiration=3600, method='GET')
