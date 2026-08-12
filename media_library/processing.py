"""
Variant generation: takes a validated source image and produces the set of
resized WebP (and AVIF, where the codec is available) files that get served
to browsers. EXIF is always stripped. Quality is chosen per medical-image
category so we don't over-compress an X-ray/ECG the same way we would a
diagram.
"""
import io
import logging

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

try:
    import pillow_avif  # noqa: F401  — registers the AVIF codec with Pillow, if installed
    AVIF_AVAILABLE = True
except ImportError:
    AVIF_AVAILABLE = False

# category -> (webp_quality, avif_quality)
# X-ray/CT-MRI/histology/ECG get higher quality so fine clinical detail and
# thin ECG trace lines survive compression; ordinary diagrams/photos/screens
# don't need it and would just waste bandwidth at that quality.
QUALITY_PROFILES = {
    'diagram': (81, 76),
    'photograph': (83, 78),
    'xray': (90, 86),
    'ct_mri': (90, 86),
    'histology': (90, 86),
    'ecg': (92, 88),
    'screenshot_table': (90, 85),
    'other': (82, 77),
}

# image_type -> list of target widths to generate (largest first is not
# required; order doesn't matter, generate_variants sorts as needed).
VARIANT_WIDTHS = {
    'student_avatar': [400],
    'teacher_avatar': [800],
    'course_thumbnail': [480, 768, 1200],
    'question_image': [480, 768, 1200, 1600, 2400],
    'option_image': [480, 768, 1200, 1600, 2400],
    'explanation_image': [480, 768, 1200, 1600, 2400],
    'banner': [768, 1200, 2000],
    'logo': [800],
    'rich_text': [480, 768, 1200],
    'other': [480, 768, 1200],
}

SQUARE_CROP_TYPES = {'student_avatar'}


def _strip_exif_and_normalize(img):
    """Applies EXIF orientation (so the pixels are physically upright),
    then drops all EXIF/ICC metadata by re-creating the image from raw pixel
    data — the standard, reliable way to strip metadata with Pillow."""
    img = ImageOps.exif_transpose(img)
    if img.mode not in ('RGB', 'RGBA'):
        img = img.convert('RGBA' if 'A' in img.mode else 'RGB')
    clean = Image.new(img.mode, img.size)
    clean.putdata(list(img.getdata()))
    return clean


def _square_crop(img):
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return img.crop((left, top, left + side, top + side))


def _resize_to_width(img, target_width):
    if img.width <= target_width:
        return img
    ratio = target_width / img.width
    target_height = max(1, round(img.height * ratio))
    return img.resize((target_width, target_height), Image.LANCZOS)


def generate_variants(source_bytes, image_type, category='other'):
    """
    Returns {"480.webp": bytes, "480.avif": bytes, ...} for every breakpoint
    at or below the source image's own width (never upscales). AVIF entries
    are omitted if the AVIF codec isn't available in this environment.
    """
    webp_q, avif_q = QUALITY_PROFILES.get(category, QUALITY_PROFILES['other'])
    widths = VARIANT_WIDTHS.get(image_type, VARIANT_WIDTHS['other'])

    with Image.open(io.BytesIO(source_bytes)) as raw:
        clean = _strip_exif_and_normalize(raw)

    if image_type in SQUARE_CROP_TYPES:
        clean = _square_crop(clean)

    variants = {}
    applicable_widths = [w for w in widths if w <= clean.width] or [clean.width]
    # Always include the source's own (capped) width so we don't only ever
    # ship down-sized variants when the original is smaller than the
    # smallest breakpoint.
    if clean.width not in applicable_widths:
        applicable_widths.append(clean.width)

    for width in sorted(set(applicable_widths)):
        resized = _resize_to_width(clean, width)

        webp_buf = io.BytesIO()
        resized.save(webp_buf, format='WEBP', quality=webp_q, method=6)
        variants[f'{width}.webp'] = webp_buf.getvalue()

        if AVIF_AVAILABLE:
            try:
                avif_buf = io.BytesIO()
                resized.save(avif_buf, format='AVIF', quality=avif_q)
                variants[f'{width}.avif'] = avif_buf.getvalue()
            except Exception:
                logger.warning('AVIF encode failed for width=%s, skipping AVIF for this variant', width, exc_info=True)

    return variants
