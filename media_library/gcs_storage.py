"""
Thin wrapper around google-cloud-storage. Kept separate from processing.py
and views.py so the storage backend is swappable without touching business
logic (per the "keep storage abstraction separate" principle this app was
built against).
"""
import logging
import time
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

    Scalability audit: this is a real network round trip to the IAM
    Credentials API on every single call — confirmed via direct
    measurement (~65-90ms steady-state, ~1s on a cold credential refresh),
    with zero caching, even for the identical object_key called
    repeatedly. cached_signed_url() below is the fix; this function itself
    is intentionally left untouched so it stays the uncached ground truth
    every caller can still reach directly (used as the Redis-down and
    signing-failure fallback)."""
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


# Scalability audit: Question Bank pages with real images were paying a
# fresh signBlob round trip for every single (bucket, object_key) variant
# on every single request — confirmed 260 signBlob calls for one 500-
# question page (130 image-bearing questions × 2 variants each), ~15-18s
# of pure serialization time, with zero reuse even for the identical
# object across immediately-consecutive calls. A signed URL for a given
# object_key never points to different content (MediaAsset rows are
# immutable per upload — a re-upload creates a new MediaAsset/object_key,
# never overwrites), so caching the URL string itself is safe for any
# duration up to its own real expiration. Never store these in the
# database (they're time-limited bearer credentials, not durable data) —
# Redis-only, with a TTL short enough that nothing served from cache is
# ever near-expired.
SIGNED_URL_CACHE_SECONDS = 2700  # 45 min -- shorter than expires_seconds=3600 by design
_SIGNED_URL_LOCK_TIMEOUT = 10  # seconds -- a single signBlob call is ~1s worst-case; generous margin
_SIGNED_URL_WAIT_POLL_SECONDS = 0.1
_SIGNED_URL_WAIT_MAX_POLLS = 30  # 3s total -- safely covers the ~1s cold-credential worst case


def _signed_url_cache_key(bucket_name, object_key):
    return f'media:signed_url:{bucket_name}:{object_key}'


def _signed_url_lock_key(bucket_name, object_key):
    return f'media:signed_url:lock:{bucket_name}:{object_key}'


def cached_signed_url(bucket_name, object_key, expires_seconds=3600):
    """Redis-cached wrapper around signed_url() — same signature, same
    return value (a URL string), same errors on signing failure. Never
    changes *what* is signed or *who* can reach this function; only skips
    re-signing an object_key this process (or another one, via the shared
    Redis cache) already signed recently.

    Cache-stampede safety: cache.add() is atomic (SETNX-equivalent), so
    exactly one concurrent caller for a given (bucket, object_key) wins
    the lock and actually calls signBlob; every other concurrent caller
    polls the cache briefly and reuses the URL the winner just produced,
    instead of every one of them independently hitting the IAM API.

    Redis-down / signing-failure safety: any cache operation failing
    (get/add/set/delete) is swallowed and treated as a plain cache miss —
    this function then falls back to calling signed_url() directly, so a
    Redis outage degrades to today's (uncached, but correct) behavior
    rather than breaking image delivery. A signing failure is never
    cached and propagates exactly as it does from signed_url() today."""
    from django.core.cache import cache

    cache_key = _signed_url_cache_key(bucket_name, object_key)

    def _get_cached():
        try:
            return cache.get(cache_key)
        except Exception:  # noqa: BLE001 - Redis down: treat as a cache miss, never break image delivery
            logger.warning('cached_signed_url: cache.get failed for %s, falling back', cache_key, exc_info=True)
            return None

    cached = _get_cached()
    if cached:
        return cached

    lock_key = _signed_url_lock_key(bucket_name, object_key)
    try:
        got_lock = cache.add(lock_key, '1', timeout=_SIGNED_URL_LOCK_TIMEOUT)
    except Exception:  # noqa: BLE001 - Redis down: proceed as if unlocked, sign directly below
        logger.warning('cached_signed_url: cache.add (lock) failed for %s, signing directly', lock_key, exc_info=True)
        got_lock = True

    if not got_lock:
        # Someone else is signing this exact object right now — wait
        # briefly and reuse what they produce instead of also calling
        # signBlob ourselves (the cache-stampede case this exists for).
        for _ in range(_SIGNED_URL_WAIT_MAX_POLLS):
            time.sleep(_SIGNED_URL_WAIT_POLL_SECONDS)
            cached = _get_cached()
            if cached:
                return cached
        # Lock holder didn't finish in time (slow signBlob call, or it
        # crashed after taking the lock) — sign directly rather than wait
        # forever; the stale lock simply expires on its own timeout.

    try:
        url = signed_url(bucket_name, object_key, expires_seconds)
    except Exception:
        # Never cache a failed signing attempt. Release the lock (if we
        # hold it) so a retry isn't stuck waiting out the full lock
        # timeout, then propagate the exact same error signed_url() would
        # have raised on its own.
        if got_lock:
            try:
                cache.delete(lock_key)
            except Exception:  # noqa: BLE001
                pass
        raise

    try:
        cache.set(cache_key, url, SIGNED_URL_CACHE_SECONDS)
    except Exception:  # noqa: BLE001 - Redis down for the write: the URL is still valid and returned below,
        logger.warning('cached_signed_url: cache.set failed for %s', cache_key, exc_info=True)  # just not cached this time.
    if got_lock:
        try:
            cache.delete(lock_key)
        except Exception:  # noqa: BLE001
            pass
    return url
