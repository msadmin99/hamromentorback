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
