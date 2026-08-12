"""
Async processing via Cloud Tasks. A task is just an authenticated HTTP POST
back to this same Cloud Run service — no broker/worker process to run,
consistent with there being no existing queue infra in this project.

Falls back to processing inline (synchronously) when Cloud Tasks isn't
configured, e.g. local development — see IMAGE_PROCESSING_ASYNC.
"""
import json
import logging

from django.conf import settings

from .gcs_storage import download_bytes, upload_bytes
from .processing import generate_variants

logger = logging.getLogger(__name__)


def enqueue_processing_task(media_asset_id):
    if not settings.IMAGE_PROCESSING_ASYNC:
        process_media_asset(str(media_asset_id))
        return

    from google.cloud import tasks_v2

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(settings.GCP_PROJECT_ID, settings.GCP_REGION, settings.CLOUD_TASKS_QUEUE)
    task = {
        'http_request': {
            'http_method': tasks_v2.HttpMethod.POST,
            'url': f'{settings.BACKEND_INTERNAL_URL}/api/media/process/',
            'headers': {
                'Content-Type': 'application/json',
                'X-Media-Processing-Secret': settings.MEDIA_PROCESSING_SECRET,
            },
            'body': json.dumps({'media_asset_id': str(media_asset_id)}).encode(),
        }
    }
    client.create_task(request={'parent': parent, 'task': task})


def process_media_asset(media_asset_id):
    """The actual work: download original, generate variants, upload them,
    flip status to ready/failed. Idempotent — safe to retry."""
    from .models import MediaAsset

    try:
        asset = MediaAsset.objects.get(id=media_asset_id)
    except MediaAsset.DoesNotExist:
        logger.error('process_media_asset: MediaAsset %s not found', media_asset_id)
        return

    asset.processing_status = 'processing'
    asset.processing_attempts += 1
    asset.save(update_fields=['processing_status', 'processing_attempts'])

    try:
        original_bytes = download_bytes(asset.bucket, f'{asset.storage_key}original.{_ext(asset.format)}')
        variant_bytes = generate_variants(original_bytes, asset.image_type, asset.category)

        target_bucket = settings.MEDIA_GCS_PUBLIC_BUCKET if asset.visibility == 'public' else settings.MEDIA_GCS_PRIVATE_BUCKET
        variant_keys = {}
        for name, data in variant_bytes.items():
            object_key = f'public/{asset.storage_key}{name}' if asset.visibility == 'public' else f'private/{asset.storage_key}{name}'
            content_type = 'image/avif' if name.endswith('.avif') else 'image/webp'
            upload_bytes(target_bucket, object_key, data, content_type)
            variant_keys[name.replace('.', '_')] = object_key

        asset.variants = variant_keys
        asset.processing_status = 'ready'
        asset.processing_error = ''
        asset.save(update_fields=['variants', 'processing_status', 'processing_error'])
    except Exception as exc:
        logger.exception('process_media_asset failed for %s', media_asset_id)
        asset.processing_status = 'failed'
        asset.processing_error = str(exc)[:2000]
        asset.save(update_fields=['processing_status', 'processing_error'])


def _ext(fmt):
    return {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp', 'GIF': 'gif', 'BMP': 'bmp', 'TIFF': 'tiff'}.get(fmt, 'bin')
