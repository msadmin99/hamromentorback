"""Secure storage for payment-proof screenshots — the private GCS bucket via
media_library's existing gcs_storage wrapper (reused, not duplicated). Never
served through a public/predictable URL; PurchaseViewSet.screenshot is the
only sanctioned read path (a short-lived signed URL, owner/staff only).

Screenshots are never deleted once submitted, even on rejection — they're
the evidence a dispute would be resolved against, and Purchase rows
themselves are permanent financial records (see PurchaseAdmin)."""
import uuid

from django.conf import settings
from PIL import Image

from media_library.gcs_storage import signed_url, upload_bytes

# Keyed by the *actual* decoded PIL format, never a client-supplied filename
# or Content-Type header — an executable renamed to "receipt.jpg" fails
# Image.open()/.verify() outright, and a real-but-disallowed format (BMP,
# TIFF, GIF, ...) is rejected here even if its header claims otherwise.
ALLOWED_FORMATS = {'JPEG': ('jpg', 'image/jpeg'), 'PNG': ('png', 'image/png'), 'WEBP': ('webp', 'image/webp')}


class InvalidScreenshot(Exception):
    """Not a decodable image, or not one of the allowed formats."""


def detect_image_format(file_obj):
    file_obj.seek(0)
    try:
        img = Image.open(file_obj)
        img.verify()
        fmt = (img.format or '').upper()
    except Exception as exc:  # noqa: BLE001 - any decode failure means "not a valid image"
        raise InvalidScreenshot('Could not read this file as an image.') from exc
    finally:
        file_obj.seek(0)
    if fmt not in ALLOWED_FORMATS:
        raise InvalidScreenshot('Only JPG, JPEG, PNG, or WebP images are allowed.')
    return fmt


def store_screenshot(file_obj):
    """Uploads a validated image file to the private bucket under a random,
    non-guessable key (never the original filename). Returns (bucket, key)."""
    fmt = detect_image_format(file_obj)
    ext, content_type = ALLOWED_FORMATS[fmt]
    key = f'payment_screenshots/{uuid.uuid4().hex}.{ext}'
    bucket = settings.MEDIA_GCS_PRIVATE_BUCKET
    file_obj.seek(0)
    upload_bytes(bucket, key, file_obj.read(), content_type)
    return bucket, key


def screenshot_view_url(bucket, key, expires_seconds=300):
    if not (bucket and key):
        return None
    return signed_url(bucket, key, expires_seconds=expires_seconds)
