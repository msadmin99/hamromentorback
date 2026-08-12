"""
Shared "turn a file into a MediaAsset" path, used by both the direct
upload API (media_library.views.MediaUploadView) and the CSV/bulk-import
pipeline (academics.import_engine) — one implementation, two callers, so
dedup/validation/processing behave identically regardless of upload route.
"""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError

from .gcs_storage import upload_bytes
from .models import MediaAsset
from .permissions_util import DEFAULT_VISIBILITY
from .tasks import enqueue_processing_task
from .validators import compute_content_hash, validate_image_upload


def create_media_asset_from_file(
    file_obj, image_type, owner=None, owner_role='system', category='other',
    question_id=None, option_id=None, original_filename=None,
):
    """
    Validates `file_obj`, dedups against existing MediaAssets by content
    hash, and either reuses an already-processed asset's variants or
    uploads + enqueues processing for a new one.

    Raises django.core.exceptions.ValidationError on invalid input.
    Returns the created/reused MediaAsset (never None).
    """
    validated = validate_image_upload(file_obj, image_type)
    content_hash = compute_content_hash(file_obj)

    existing = (
        MediaAsset.objects.filter(content_hash=content_hash, image_type=image_type, processing_status='ready')
        .order_by('-created_at')
        .first()
    )
    visibility = DEFAULT_VISIBILITY.get(image_type, 'private')
    file_obj.seek(0, 2)
    file_size = file_obj.tell()
    file_obj.seek(0)
    name = original_filename or getattr(file_obj, 'name', '') or ''

    if existing:
        return MediaAsset.objects.create(
            owner=owner, owner_role=owner_role, question_id=question_id, option_id=option_id,
            image_type=image_type, category=category,
            storage_key=existing.storage_key, bucket=existing.bucket,
            original_filename=name, mime_type=validated.mime_type, format=validated.format,
            width=existing.width, height=existing.height, file_size=file_size,
            content_hash=content_hash, processing_status='ready',
            visibility=visibility, variants=existing.variants,
        )

    storage_key = f'{image_type}/img_{uuid.uuid4().hex}/'
    upload_bytes(
        settings.MEDIA_GCS_PRIVATE_BUCKET,
        f'{storage_key}original.{validated.extension}',
        file_obj.read(),
        validated.mime_type,
    )

    asset = MediaAsset.objects.create(
        owner=owner, owner_role=owner_role, question_id=question_id, option_id=option_id,
        image_type=image_type, category=category,
        storage_key=storage_key, bucket=settings.MEDIA_GCS_PRIVATE_BUCKET,
        original_filename=name, mime_type=validated.mime_type, format=validated.format,
        width=validated.width, height=validated.height, file_size=file_size,
        content_hash=content_hash, processing_status='uploading', visibility=visibility,
    )
    enqueue_processing_task(asset.id)
    asset.processing_status = 'processing'
    asset.save(update_fields=['processing_status'])
    return asset
