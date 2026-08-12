"""
Upload validation. Never trusts the client-supplied filename, extension, or
Content-Type — every check here re-derives the truth from the actual file
bytes (Pillow's own format sniffing), per the security requirements this
app was built against.
"""
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from PIL import Image

# Formats we're willing to accept as an upload source. SVG is deliberately
# absent — it's handled by a separate, admin-only, sanitized code path
# (svg_utils.py), never through this general image validator.
ALLOWED_PIL_FORMATS = {'JPEG', 'PNG', 'WEBP', 'GIF', 'BMP', 'TIFF'}

# (image_type) -> (max_upload_bytes, max_dimension_px)
IMAGE_TYPE_LIMITS = {
    'student_avatar': (2 * 1024 * 1024, 800),
    'teacher_avatar': (5 * 1024 * 1024, 1600),
    'course_thumbnail': (5 * 1024 * 1024, 1200),
    'question_image': (10 * 1024 * 1024, 2400),
    'option_image': (10 * 1024 * 1024, 2400),
    'explanation_image': (10 * 1024 * 1024, 2400),
    'banner': (10 * 1024 * 1024, 2400),
    'logo': (5 * 1024 * 1024, 2400),
    'rich_text': (10 * 1024 * 1024, 2400),
    'other': (10 * 1024 * 1024, 2400),
}

# Hard ceiling regardless of image_type — guards against a misconfigured
# limit and against decompression-bomb-style dimension abuse.
ABSOLUTE_MAX_UPLOAD_BYTES = 15 * 1024 * 1024
ABSOLUTE_MAX_PIXELS = 4000 * 4000


@dataclass
class ValidatedImage:
    format: str  # 'JPEG' / 'PNG' / 'WEBP' / ...
    width: int
    height: int
    mime_type: str

    @property
    def extension(self):
        return {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp', 'GIF': 'gif', 'BMP': 'bmp', 'TIFF': 'tiff'}[self.format]


_MIME_BY_FORMAT = {
    'JPEG': 'image/jpeg', 'PNG': 'image/png', 'WEBP': 'image/webp',
    'GIF': 'image/gif', 'BMP': 'image/bmp', 'TIFF': 'image/tiff',
}


def validate_image_upload(file_obj, image_type):
    """
    Validates an uploaded file for the given image_type. Raises
    django.core.exceptions.ValidationError with a clear message on any
    failure. Returns a ValidatedImage describing the real, sniffed format
    and dimensions on success.

    `file_obj` must be a file-like object opened in binary mode (e.g. an
    UploadedFile). Its position is restored to 0 before returning.
    """
    if image_type not in IMAGE_TYPE_LIMITS:
        raise ValidationError(f'Unknown image_type "{image_type}".')

    max_bytes, max_dimension = IMAGE_TYPE_LIMITS[image_type]
    max_bytes = min(max_bytes, ABSOLUTE_MAX_UPLOAD_BYTES)

    file_obj.seek(0, 2)  # seek to end
    size = file_obj.tell()
    file_obj.seek(0)
    if size == 0:
        raise ValidationError('Uploaded file is empty.')
    if size > max_bytes:
        raise ValidationError(f'File is {size / 1024 / 1024:.1f}MB — maximum allowed is {max_bytes / 1024 / 1024:.0f}MB.')

    # Reject obvious non-image / script content before even handing bytes
    # to Pillow (belt-and-suspenders — Pillow would reject these too, but
    # this avoids parsing untrusted bytes with a heavier library for
    # clearly-malicious input).
    head = file_obj.read(512)
    file_obj.seek(0)
    lowered = head.lower()
    for marker in (b'<?php', b'<script', b'<html', b'#!/', b'\x4d\x5a'):  # MZ = PE/EXE header
        if lowered.startswith(marker) or marker in lowered[:64]:
            raise ValidationError('File does not appear to be a valid image.')

    try:
        with Image.open(file_obj) as img:
            img.verify()  # cheap structural check; re-open below for real use since verify() closes the image
    except Exception:
        raise ValidationError('File is not a valid, readable image.')

    file_obj.seek(0)
    try:
        with Image.open(file_obj) as img:
            fmt = img.format
            width, height = img.size
    except Exception:
        raise ValidationError('File is not a valid, readable image.')
    finally:
        file_obj.seek(0)

    if fmt not in ALLOWED_PIL_FORMATS:
        raise ValidationError(f'Image format "{fmt}" is not allowed. Allowed: {", ".join(sorted(ALLOWED_PIL_FORMATS))}.')

    if width <= 0 or height <= 0:
        raise ValidationError('Image has invalid dimensions.')
    if width * height > ABSOLUTE_MAX_PIXELS:
        raise ValidationError('Image dimensions are too large to process safely.')

    # Dimensions beyond max_dimension are fine — processing.py downsizes on
    # variant generation. We only hard-reject truly absurd uploads above.

    return ValidatedImage(format=fmt, width=width, height=height, mime_type=_MIME_BY_FORMAT[fmt])


def compute_content_hash(file_obj):
    """SHA-256 of the raw file bytes, used for dedup. Restores file position."""
    import hashlib
    file_obj.seek(0)
    h = hashlib.sha256()
    for chunk in iter(lambda: file_obj.read(65536), b''):
        h.update(chunk)
    file_obj.seek(0)
    return h.hexdigest()
