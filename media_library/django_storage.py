"""Django Storage adapter for the public GCS bucket — makes plain model
ImageField/FileField uploads (PaymentMethod.qr_code_image, Purchase
payment_screenshot, Video thumbnails, profile photos, etc.) durable on
Cloud Run instead of silently vanishing.

Root cause this fixes: Django's default FileSystemStorage writes to local
disk under MEDIA_ROOT. Cloud Run's local disk is ephemeral — wiped on every
new revision deploy, container restart, or when traffic scales to a
different instance — so a file uploaded through the Admin panel (e.g. a
payment QR code) could 404 for students within hours, with no error at
upload time. media_library's own MediaAsset/gcs_storage.py pipeline already
solves this for the fields explicitly wired through it; this module reuses
those same functions (and the same lazy google-cloud-storage client) as a
proper django.core.files.storage.Storage, so any *plain* ImageField/
FileField across the project persists correctly too, via STORAGES['default']
in settings.py — no per-field wiring needed.
"""
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible

from . import gcs_storage


@deconstructible
class PublicGCSStorage(Storage):
    """Uploads to MEDIA_GCS_PUBLIC_BUCKET, served directly from GCS's public
    URL — appropriate for this project's plain ImageField/FileField uploads
    (banners, thumbnails, payment QR codes/screenshots, profile photos),
    none of which are access-controlled the way media_library's private-
    bucket assets are."""

    def _open(self, name, mode='rb'):
        return ContentFile(gcs_storage.download_bytes(settings.MEDIA_GCS_PUBLIC_BUCKET, name), name=name)

    def _save(self, name, content):
        content_type = getattr(content, 'content_type', None) or 'application/octet-stream'
        gcs_storage.upload_bytes(settings.MEDIA_GCS_PUBLIC_BUCKET, name, content.read(), content_type)
        return name

    def exists(self, name):
        return gcs_storage._bucket(settings.MEDIA_GCS_PUBLIC_BUCKET).blob(name).exists()

    def size(self, name):
        blob = gcs_storage._bucket(settings.MEDIA_GCS_PUBLIC_BUCKET).blob(name)
        blob.reload()
        return blob.size

    def url(self, name):
        return f'https://storage.googleapis.com/{settings.MEDIA_GCS_PUBLIC_BUCKET}/{name}'

    def delete(self, name):
        gcs_storage.delete_object(settings.MEDIA_GCS_PUBLIC_BUCKET, name)
