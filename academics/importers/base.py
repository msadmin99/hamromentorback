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
    Question/Option image field only once the row is confirmed for import."""
    path = f'import_temp/{batch_id}/{slot}_{uuid.uuid4().hex}.{ext}'
    return default_storage.save(path, ContentFile(raw_bytes))


def load_temp_image(path):
    if not path or not default_storage.exists(path):
        return None
    with default_storage.open(path, 'rb') as f:
        return ContentFile(f.read(), name=path.rsplit('/', 1)[-1])
