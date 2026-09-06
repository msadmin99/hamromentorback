"""Secure storage for identity verification documents — the private GCS
bucket via media_library's existing gcs_storage wrapper, exactly mirroring
billing/screenshot_storage.py's already-proven pattern (reused, not
duplicated): real content-based validation, a random non-guessable key,
never a public/predictable URL. A document is only ever readable through
an explicit, authorized, short-lived signed URL — see
accounts.views.VerificationDocumentViewView, the one sanctioned read path.

Deliberately independent of media_library's MediaAsset pipeline (that one
backs question/exam media with its own processing/variant pipeline this
feature has no use for) and of Google Drive (no service account, no
external API, no new credentials — see the architecture decision recorded
in the Students + Enrollment Requests audit / this feature's own task
history: the existing GCS pattern already covers this exact need).

Documents are never deleted once submitted, even on rejection — matching
screenshot_storage.py's own retention rule and this feature's own
"student can resubmit, admin can see history" requirement. Replacing a
document (re-upload) creates a new VerificationDocument row; it never
overwrites or deletes the old object.
"""
import uuid

from django.conf import settings
from PIL import Image

from media_library.gcs_storage import signed_url, upload_bytes

# Keyed by the *actual* decoded content, never a client-supplied filename or
# Content-Type header. Images: same discipline as screenshot_storage.py
# (Image.open()+.verify(), not trusting the header). PDFs: no PIL support,
# so verified by magic bytes only — sufficient to reject a renamed
# executable/script, which is the actual threat model here (this is not a
# PDF-parsing security boundary, just a "is this really a PDF" content check).
ALLOWED_IMAGE_FORMATS = {'JPEG': ('jpg', 'image/jpeg'), 'PNG': ('png', 'image/png'), 'WEBP': ('webp', 'image/webp')}
PDF_MAGIC = b'%PDF-'
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024  # 10MB — generous for a phone-camera photo of a document, still bounded
MAX_PHOTO_BYTES = 5 * 1024 * 1024  # 5MB, per spec


class InvalidUpload(Exception):
    """Not a decodable/valid file of an allowed type, or too large."""


def _detect_image_format(file_obj):
    file_obj.seek(0)
    try:
        img = Image.open(file_obj)
        img.verify()
        fmt = (img.format or '').upper()
    except Exception as exc:  # noqa: BLE001 - any decode failure means "not a valid image"
        raise InvalidUpload('Could not read this file as an image.') from exc
    finally:
        file_obj.seek(0)
    return fmt if fmt in ALLOWED_IMAGE_FORMATS else None


def _is_pdf(file_obj):
    file_obj.seek(0)
    header = file_obj.read(5)
    file_obj.seek(0)
    return header == PDF_MAGIC


def validate_photo(file_obj, max_bytes=MAX_PHOTO_BYTES):
    """Returns (ext, content_type) or raises InvalidUpload. Photos are
    images only — no PDF."""
    file_obj.seek(0, 2)
    size = file_obj.tell()
    file_obj.seek(0)
    if size > max_bytes:
        raise InvalidUpload(f'Photo is too large — the limit is {max_bytes // (1024 * 1024)}MB.')
    fmt = _detect_image_format(file_obj)
    if not fmt:
        raise InvalidUpload('Only JPG, JPEG, PNG, or WebP images are allowed for the profile photo.')
    return ALLOWED_IMAGE_FORMATS[fmt]


def validate_document(file_obj, max_bytes=MAX_DOCUMENT_BYTES):
    """Returns (ext, content_type, size) or raises InvalidUpload. Documents
    may be an image or a PDF."""
    file_obj.seek(0, 2)
    size = file_obj.tell()
    file_obj.seek(0)
    if size > max_bytes:
        raise InvalidUpload(f'File is too large — the limit is {max_bytes // (1024 * 1024)}MB.')
    if _is_pdf(file_obj):
        return 'pdf', 'application/pdf', size
    fmt = _detect_image_format(file_obj)
    if not fmt:
        raise InvalidUpload('Only JPG, PNG, WebP, or PDF files are allowed for verification documents.')
    ext, content_type = ALLOWED_IMAGE_FORMATS[fmt]
    return ext, content_type, size


def store_verification_document(file_obj, user_id, document_type):
    """Validates and uploads to the private bucket under a random,
    non-guessable key — never the original filename or anything derived
    from user_id/document_type beyond the path prefix (never used to guess
    another student's key). Returns (bucket, key, content_type, size)."""
    ext, content_type, size = validate_document(file_obj)
    key = f'verification-documents/{user_id}/{uuid.uuid4().hex}.{ext}'
    bucket = settings.MEDIA_GCS_PRIVATE_BUCKET
    file_obj.seek(0)
    upload_bytes(bucket, key, file_obj.read(), content_type)
    return bucket, key, content_type, size


def verification_document_view_url(bucket, key, expires_seconds=600):
    """A short-lived (10 minute default) signed URL — never persisted,
    generated fresh on every authorized request. Returns None if this
    document has no stored object (shouldn't normally happen, but a caller
    should treat that as "not available" rather than erroring)."""
    if not (bucket and key):
        return None
    return signed_url(bucket, key, expires_seconds=expires_seconds)
