from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from core.models import DeletionAuditLog
from media_library.models import MediaAsset
from media_library.serializers import MediaAssetSerializer
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


class MediaAssetDetailViewGetPermissionTests(APITestCase):
    """P0 security-audit regression: GET /media/{uuid}/ previously had no
    ownership or staff check at all — any authenticated user could poll
    ANY asset by UUID, not just their own uploads, despite this class's own
    docstring claiming staff-only intent (which .delete() already correctly
    enforced). Fixed as owner-or-staff rather than staff-only, since
    STUDENT_ALLOWED_TYPES (permissions_util.py) lets a plain student account
    create a 'student_avatar' MediaAsset and must still be able to poll its
    own processing status."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='media_get_staff', email='media_get_staff@example.com', password='pw12345',
            is_staff=True, admin_role='admin',
        )
        self.owner = User.objects.create_user(
            username='media_get_owner', email='media_get_owner@example.com', password='pw12345',
        )
        self.other_student = User.objects.create_user(
            username='media_get_other', email='media_get_other@example.com', password='pw12345',
        )
        self.asset = _make_asset(owner=self.owner, image_type='student_avatar')

    def test_anonymous_caller_is_rejected(self):
        resp = self.client.get(f'/api/media/{self.asset.id}/')
        self.assertEqual(resp.status_code, 401)

    def test_other_authenticated_student_cannot_read_someone_elses_asset(self):
        self.client.force_authenticate(user=self.other_student)

        resp = self.client.get(f'/api/media/{self.asset.id}/')

        self.assertEqual(resp.status_code, 404)

    def test_owner_can_read_their_own_asset(self):
        self.client.force_authenticate(user=self.owner)

        resp = self.client.get(f'/api/media/{self.asset.id}/')

        self.assertEqual(resp.status_code, 200)

    def test_staff_can_read_any_asset(self):
        self.client.force_authenticate(user=self.staff)

        resp = self.client.get(f'/api/media/{self.asset.id}/')

        self.assertEqual(resp.status_code, 200)


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
    production. A first fix attempt (reusing the storage client's own
    credentials) traded that for a 403 ACCESS_TOKEN_SCOPE_INSUFFICIENT,
    also confirmed live — the storage client's credentials are scoped
    narrowly for GCS, not broad enough for the IAM signBlob call.
    gcs_storage.signed_url() must fetch its own cloud-platform-scoped
    credentials via _iam_signing_credentials() and sign through the IAM
    Credentials API (service_account_email + access_token)."""

    def setUp(self):
        from media_library import gcs_storage

        gcs_storage._iam_signing_credentials.cache_clear()

    def _mock_bucket(self, mock_bucket_fn):
        from unittest.mock import MagicMock

        mock_blob = MagicMock()
        mock_blob.generate_signed_url.return_value = 'https://signed.example/payment_screenshots/proof.jpg'
        mock_bucket_fn.return_value.blob.return_value = mock_blob
        return mock_blob

    @patch('media_library.gcs_storage._iam_signing_credentials')
    @patch('media_library.gcs_storage._bucket')
    def test_signs_via_iam_access_token_with_compute_engine_credentials(self, mock_bucket_fn, mock_credentials_fn):
        from unittest.mock import MagicMock

        from media_library import gcs_storage

        mock_blob = self._mock_bucket(mock_bucket_fn)
        credentials = MagicMock()
        credentials.valid = True
        credentials.token = 'fake-access-token'
        credentials.service_account_email = 'default-sa@example.iam.gserviceaccount.com'
        mock_credentials_fn.return_value = credentials

        url = gcs_storage.signed_url('private-bucket', 'payment_screenshots/proof.jpg', expires_seconds=300)

        self.assertEqual(url, 'https://signed.example/payment_screenshots/proof.jpg')
        mock_blob.generate_signed_url.assert_called_once_with(
            version='v4', expiration=300, method='GET',
            service_account_email='default-sa@example.iam.gserviceaccount.com', access_token='fake-access-token',
        )

    @patch('media_library.gcs_storage._iam_signing_credentials')
    @patch('media_library.gcs_storage._bucket')
    def test_falls_back_to_default_signing_without_a_service_account_email(self, mock_bucket_fn, mock_credentials_fn):
        """A local key-file-backed credential (e.g. a developer's own gcloud
        ADC) has no service_account_email attribute at all — must not break
        the normal case that already worked."""
        from unittest.mock import MagicMock

        from media_library import gcs_storage

        mock_blob = self._mock_bucket(mock_bucket_fn)
        mock_blob.generate_signed_url.return_value = 'https://signed.example/normal.jpg'
        credentials = MagicMock(spec=['valid'])
        credentials.valid = True
        mock_credentials_fn.return_value = credentials

        url = gcs_storage.signed_url('private-bucket', 'normal.jpg')

        self.assertEqual(url, 'https://signed.example/normal.jpg')
        mock_blob.generate_signed_url.assert_called_once_with(version='v4', expiration=3600, method='GET')


class CachedSignedUrlTests(TestCase):
    """Scalability audit: Question Bank pages with real images were paying
    a fresh IAM signBlob round trip for every single (bucket, object_key)
    variant on every single request — confirmed 260 signBlob calls / ~15-
    18s for one 500-question page. cached_signed_url() wraps signed_url()
    with a Redis cache (LocMemCache in this test suite — same public API,
    same semantics) plus single-flight stampede protection, Redis-failure
    fallback, and signing-failure passthrough. signed_url() itself is
    mocked throughout — these tests are entirely about the caching/
    locking wrapper, not GCS/IAM signing mechanics (already covered by
    SignedUrlComputeEngineCredentialsTests above)."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def test_cold_cache_calls_signed_url_once_and_caches_result(self):
        from media_library import gcs_storage

        with patch('media_library.gcs_storage.signed_url', return_value='https://signed.example/a.jpg') as mock_sign:
            url = gcs_storage.cached_signed_url('bucket', 'a.jpg')

        self.assertEqual(url, 'https://signed.example/a.jpg')
        mock_sign.assert_called_once_with('bucket', 'a.jpg', 3600)

    def test_warm_cache_never_calls_signed_url_again(self):
        from media_library import gcs_storage

        with patch('media_library.gcs_storage.signed_url', return_value='https://signed.example/a.jpg') as mock_sign:
            first = gcs_storage.cached_signed_url('bucket', 'a.jpg')
            second = gcs_storage.cached_signed_url('bucket', 'a.jpg')
            third = gcs_storage.cached_signed_url('bucket', 'a.jpg')

        self.assertEqual(first, second, third)
        mock_sign.assert_called_once()  # only the first (cold) call actually signed

    def test_different_object_keys_never_collide(self):
        """Cache keys must include bucket + object_key exactly."""
        from media_library import gcs_storage

        def fake_sign(bucket, object_key, expires_seconds=3600):
            return f'https://signed.example/{bucket}/{object_key}'

        with patch('media_library.gcs_storage.signed_url', side_effect=fake_sign) as mock_sign:
            url_a = gcs_storage.cached_signed_url('bucket', 'a.jpg')
            url_b = gcs_storage.cached_signed_url('bucket', 'b.jpg')
            url_a_other_bucket = gcs_storage.cached_signed_url('other-bucket', 'a.jpg')

        self.assertEqual(mock_sign.call_count, 3)  # three distinct (bucket, key) pairs, no cross-contamination
        self.assertEqual(url_a, 'https://signed.example/bucket/a.jpg')
        self.assertEqual(url_b, 'https://signed.example/bucket/b.jpg')
        self.assertEqual(url_a_other_bucket, 'https://signed.example/other-bucket/a.jpg')
        self.assertNotEqual(url_a, url_a_other_bucket)

    def test_multiple_variants_of_same_image_each_cached_independently(self):
        """Mirrors MediaAssetSerializer.get_urls()'s loop over an asset's
        several width/format variants — each variant is a different
        object_key, so each gets its own cache entry and its own single
        signBlob call, but repeat requests for the SAME variant set hit
        cache for all of them."""
        from media_library import gcs_storage

        variants = {'480_webp': 'q/1/480.webp', '800_webp': 'q/1/800.webp', '1200_webp': 'q/1/1200.webp'}

        with patch('media_library.gcs_storage.signed_url', side_effect=lambda b, k, e=3600: f'https://signed/{k}') as mock_sign:
            first_pass = {name: gcs_storage.cached_signed_url('bucket', key) for name, key in variants.items()}
            second_pass = {name: gcs_storage.cached_signed_url('bucket', key) for name, key in variants.items()}

        self.assertEqual(first_pass, second_pass)
        self.assertEqual(mock_sign.call_count, 3)  # 3 distinct variants signed once each, not 6

    def test_concurrent_requests_for_same_object_cause_only_one_signblob_call(self):
        """Cache-stampede protection: 100 threads racing for the same
        (bucket, object_key) must result in exactly one real signBlob call
        — every other thread waits briefly and reuses the winner's cached
        URL, never independently hitting the IAM API."""
        import threading

        from media_library import gcs_storage

        call_count = {'n': 0}
        call_lock = threading.Lock()

        def slow_sign(bucket, object_key, expires_seconds=3600):
            with call_lock:
                call_count['n'] += 1
            import time
            time.sleep(0.2)  # simulate a real signBlob round trip long enough for other threads to queue up
            return 'https://signed.example/stampede.jpg'

        results = []
        results_lock = threading.Lock()

        def worker():
            url = gcs_storage.cached_signed_url('bucket', 'stampede.jpg')
            with results_lock:
                results.append(url)

        # patch() applied ONCE around the whole threaded section (not per
        # thread — unittest.mock.patch's enter/exit is not safe to race
        # across threads, confirmed the hard way: an earlier per-thread-
        # patch version of this test leaked a corrupted signed_url mock
        # into unrelated tests later in the same run). This still
        # exercises the real race this test is for: the cache.add() lock
        # inside cached_signed_url() itself, called concurrently by 100
        # real threads.
        with patch('media_library.gcs_storage.signed_url', side_effect=slow_sign):
            threads = [threading.Thread(target=worker) for _ in range(100)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        self.assertEqual(len(results), 100)
        self.assertTrue(all(r == 'https://signed.example/stampede.jpg' for r in results))
        self.assertEqual(call_count['n'], 1, f'expected exactly 1 signBlob call, got {call_count["n"]}')

    def test_redis_failure_falls_back_to_direct_signed_url_and_still_delivers_image(self):
        """Every cache.get/add/set/delete failing must never break image
        delivery — falls back to calling signed_url() directly each time,
        exactly today's (uncached) behavior, not an error."""
        from media_library import gcs_storage

        with patch('media_library.gcs_storage.signed_url', return_value='https://signed.example/a.jpg') as mock_sign, \
                patch('django.core.cache.cache.get', side_effect=Exception('redis down')), \
                patch('django.core.cache.cache.add', side_effect=Exception('redis down')), \
                patch('django.core.cache.cache.set', side_effect=Exception('redis down')), \
                patch('django.core.cache.cache.delete', side_effect=Exception('redis down')):
            url = gcs_storage.cached_signed_url('bucket', 'a.jpg')

        self.assertEqual(url, 'https://signed.example/a.jpg')
        mock_sign.assert_called_once()

    def test_signing_failure_is_never_cached_and_propagates(self):
        """A failed signing attempt must not be cached (so the next
        request retries for real, not returns a broken/empty result
        forever), and must raise the same way signed_url() itself does."""
        from media_library import gcs_storage

        with patch('media_library.gcs_storage.signed_url', side_effect=RuntimeError('IAM signBlob failed')):
            with self.assertRaises(RuntimeError):
                gcs_storage.cached_signed_url('bucket', 'a.jpg')

        # A subsequent, successful call must actually retry signing (not
        # return a cached failure/None).
        with patch('media_library.gcs_storage.signed_url', return_value='https://signed.example/a.jpg') as mock_sign:
            url = gcs_storage.cached_signed_url('bucket', 'a.jpg')
        self.assertEqual(url, 'https://signed.example/a.jpg')
        mock_sign.assert_called_once()

    def test_cache_ttl_is_shorter_than_the_signed_url_expiration(self):
        """Requirement: the Redis cache entry must expire well before the
        signed URL itself does, so nothing served from cache is ever
        near-expired."""
        from media_library import gcs_storage

        self.assertLess(gcs_storage.SIGNED_URL_CACHE_SECONDS, 3600)
        self.assertEqual(gcs_storage.SIGNED_URL_CACHE_SECONDS, 2700)


class MediaAssetSerializerImageUrlCachingTests(TestCase):
    """The actual integration point Question/Option serialization goes
    through — get_urls() must keep using cached_signed_url() for private
    assets (behavior change under test) while public assets keep bypassing
    signing entirely (must NOT change)."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    @override_settings(MEDIA_GCS_PRIVATE_BUCKET='private-bucket')
    def test_private_asset_urls_are_cached_across_repeated_serialization(self):
        asset = _make_asset(visibility='private', processing_status='ready')

        with patch('media_library.gcs_storage.signed_url', return_value='https://signed.example/x') as mock_sign:
            first = MediaAssetSerializer(asset).data['urls']
            second = MediaAssetSerializer(asset).data['urls']

        self.assertEqual(first, second)
        # 2 variants in _make_asset()'s fixture -- signed once each across
        # BOTH serializations, not once per serialization.
        self.assertEqual(mock_sign.call_count, 2)

    def test_public_asset_still_bypasses_signing_entirely(self):
        asset = _make_asset(visibility='public', processing_status='ready')

        with patch('media_library.gcs_storage.cached_signed_url') as mock_cached_sign, \
                patch('media_library.gcs_storage.signed_url') as mock_sign:
            urls = MediaAssetSerializer(asset).data['urls']

        mock_cached_sign.assert_not_called()
        mock_sign.assert_not_called()
        self.assertTrue(all(u.startswith('https://storage.googleapis.com/') for u in urls.values()))

    def test_response_shape_unchanged(self):
        """Same keys, same value type (a plain URL string) as before —
        the caching change must be invisible to API consumers."""
        asset = _make_asset(visibility='private', processing_status='ready')

        with patch('media_library.gcs_storage.signed_url', return_value='https://signed.example/x'):
            urls = MediaAssetSerializer(asset).data['urls']

        self.assertEqual(set(urls.keys()), {'480', '480_public'})
        for v in urls.values():
            self.assertIsInstance(v, str)
