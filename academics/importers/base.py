"""Shared shape every format-specific parser produces, plus the temp-image
storage helper used to stash extracted images until a row is actually
confirmed (so ImportRow.raw_data, a JSONField, never has to hold binary
data — just a stored-file path)."""
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage


def ParsedQuestion(
    text_html='', options=None, explanation_html='',
    remarks='', year=None, past_exam_years='', explanation_video_url='', references=None,
    question_image_path='', explanation_image_path='',
):
    """A plain-dict builder (not a class) since every instance is immediately
    JSON-serialized into ImportRow.raw_data. `options` is a list of
    {"text_html": str, "is_correct": bool, "image_path": str}.

    Deliberately carries no subject/chapter/topic/marks/negative_marks — the
    file no longer supplies them. Subject/Chapter/Topic are chosen once for
    the whole batch on the Preview & Validate screen; marks/negative marks
    are set later in Exam Management, so Question just takes its model
    defaults (1 / 0.25) at import time."""
    return {
        'text_html': text_html,
        'options': options or [],
        'explanation_html': explanation_html,
        'remarks': remarks,
        'year': year,
        'past_exam_years': past_exam_years,
        'explanation_video_url': explanation_video_url,
        'references': references or [],
        'question_image_path': question_image_path,
        'explanation_image_path': explanation_image_path,
    }


def save_temp_image(batch_id, slot, raw_bytes, ext='png'):
    """Stashes an extracted image under a batch-scoped temp path and returns
    the storage-relative path — read back and attached to the real
    Question/Option image field only once the row is confirmed for import.

    Production incident (MAT-4.docx bulk import, 2026-09-26): a real
    upload with 5 embedded images failed on the very first one with
    "An error occurred (403) when calling the HeadObject operation:
    Forbidden". Root cause, confirmed directly against the bucket:
    Django's default_storage.save() always calls get_available_name(),
    which calls .exists() — an S3 HeadObject — on this brand-new key
    BEFORE writing it. S3 returns 403 (not 404) for a HeadObject/GetObject
    on a key that doesn't exist whenever the caller's IAM policy lacks
    s3:ListBucket on the bucket (documented AWS behavior: without
    ListBucket, S3 won't tell an unauthenticated-for-that-purpose caller
    "not found" vs "no access", so it says neither) — this app's runtime
    IAM user has PutObject/GetObject but not ListBucket, confirmed via a
    live diagnostic against the actual bucket. So the existence pre-check
    was guaranteed to fail on every single call, for every prefix, not
    just import_temp/ — it just happened to be the first code path that
    actually exercised a fresh default_storage.save() in production.

    Fixed by writing directly through s3_storage (bypassing Django's
    Storage.save()/get_available_name() and its .exists() pre-check
    entirely) whenever S3 is actually configured — this key's uuid4
    component already makes a genuine collision astronomically unlikely,
    so that pre-check bought nothing here even on a bucket where IAM does
    allow it. Local dev/tests (AWS_S3_PUBLIC_BUCKET unset) are completely
    unaffected: they keep using default_storage.save() via the default
    FileSystemStorage below, which has no such permission model at all.

    The proper long-term fix is granting s3:ListBucket on this bucket (and
    its private counterpart) to the runtime IAM user — this is an AWS-side
    change outside anything this codebase's own credentials can safely
    self-grant, so it isn't done here; this fix is the resilient
    application-level workaround."""
    path = f'import_temp/{batch_id}/{slot}_{uuid.uuid4().hex}.{ext}'
    from django.conf import settings
    if settings.AWS_S3_PUBLIC_BUCKET:
        from media_library import s3_storage
        content_type = 'image/jpeg' if ext in ('jpg', 'jpeg') else f'image/{ext}'
        s3_storage.upload_bytes(settings.AWS_S3_PUBLIC_BUCKET, path, raw_bytes, content_type)
        return path
    return default_storage.save(path, ContentFile(raw_bytes))


def load_temp_image(path):
    if not path or not default_storage.exists(path):
        return None
    with default_storage.open(path, 'rb') as f:
        return ContentFile(f.read(), name=path.rsplit('/', 1)[-1])
