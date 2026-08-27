"""
Thin wrapper around google-cloud-storage. Kept separate from processing.py
and views.py so the storage backend is swappable without touching business
logic (per the "keep storage abstraction separate" principle this app was
built against).
"""
import logging
from functools import lru_cache

from django.conf import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _client():
    from google.cloud import storage
    return storage.Client()


def _bucket(name):
    return _client().bucket(name)


def upload_bytes(bucket_name, object_key, data, content_type):
    blob = _bucket(bucket_name).blob(object_key)
    blob.upload_from_string(data, content_type=content_type)
    return object_key


def download_bytes(bucket_name, object_key):
    blob = _bucket(bucket_name).blob(object_key)
    return blob.download_as_bytes()


def delete_object(bucket_name, object_key):
    blob = _bucket(bucket_name).blob(object_key)
    blob.delete(if_exists=True) if hasattr(blob, 'delete') else blob.delete()


def public_url(object_key):
    """Public bucket objects are served directly from GCS's public URL —
    fine for stage 1; swap for a Cloud CDN / backend-bucket URL later
    without touching any caller of this function."""
    return f'https://storage.googleapis.com/{settings.MEDIA_GCS_PUBLIC_BUCKET}/{object_key}'


@lru_cache(maxsize=1)
def _iam_signing_credentials():
    """Separate from _client()'s own credentials on purpose — google-cloud-
    storage mints/caches its client credentials scoped narrowly for GCS
    operations, which the IAM Credentials API's signBlob call then rejects
    with ACCESS_TOKEN_SCOPE_INSUFFICIENT (confirmed live in production: the
    very first fix attempt here, reusing client._credentials, traded the
    original AttributeError for exactly this 403). Signing needs its own
    credentials fetched with the broad cloud-platform scope."""
    import google.auth

    credentials, _project = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    return credentials


def signed_url(bucket_name, object_key, expires_seconds=3600):
    """For private assets that need temporary browser access (e.g. an
    admin previewing an unpublished question's image, or a payment
    screenshot on the Payments verification page).

    blob.generate_signed_url() needs a private key to sign with by
    default — fine with a service-account JSON key file, but Cloud Run's
    attached service account is Application Default Credentials backed by
    the metadata server, which "just contains a token" (confirmed live in
    production — every "View screenshot" click was silently 500ing on the
    resulting AttributeError). The fix Google documents for exactly this
    environment: sign via the IAM Credentials API's signBlob using a
    cloud-platform-scoped access token, rather than a local private key.
    Requires the runtime service account to have
    roles/iam.serviceAccountTokenCreator on itself.
    """
    blob = _bucket(bucket_name).blob(object_key)
    credentials = _iam_signing_credentials()
    if not credentials.valid:
        from google.auth.transport import requests as google_auth_requests
        credentials.refresh(google_auth_requests.Request())
    service_account_email = getattr(credentials, 'service_account_email', None)
    if service_account_email and service_account_email != 'default':
        return blob.generate_signed_url(
            version='v4', expiration=expires_seconds, method='GET',
            service_account_email=service_account_email, access_token=credentials.token,
        )
    return blob.generate_signed_url(version='v4', expiration=expires_seconds, method='GET')
