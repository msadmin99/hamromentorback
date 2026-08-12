import uuid

from django.conf import settings
from django.db import models

# Responsive breakpoints generated for every "question_image"-class asset.
# Other image types use a narrower profile (see processing.py).
QUESTION_IMAGE_VARIANT_WIDTHS = [480, 768, 1200, 1600, 2400]


class MediaAsset(models.Model):
    OWNER_ROLE_CHOICES = [
        ('student', 'Student'),
        ('teacher', 'Teacher'),
        ('admin', 'Admin'),
        ('system', 'System'),
    ]

    IMAGE_TYPE_CHOICES = [
        ('student_avatar', 'Student Avatar'),
        ('teacher_avatar', 'Teacher Avatar'),
        ('course_thumbnail', 'Course Thumbnail'),
        ('question_image', 'Question Image'),
        ('option_image', 'Option Image'),
        ('explanation_image', 'Explanation Image'),
        ('banner', 'Banner'),
        ('logo', 'Logo'),
        ('rich_text', 'Rich Text Inline'),
        ('other', 'Other'),
    ]

    # Drives the WebP/AVIF quality used when generating variants — medical
    # image categories (x-ray, ECG, histology) get higher quality than a
    # typical diagram/photo so fine clinical detail isn't lost to compression.
    CATEGORY_CHOICES = [
        ('diagram', 'Diagram'),
        ('photograph', 'Photograph'),
        ('xray', 'X-ray'),
        ('ct_mri', 'CT/MRI'),
        ('histology', 'Histology'),
        ('ecg', 'ECG'),
        ('screenshot_table', 'Screenshot/Table'),
        ('other', 'Other'),
    ]

    STATUS_CHOICES = [
        ('uploading', 'Uploading'),
        ('processing', 'Processing'),
        ('ready', 'Ready'),
        ('failed', 'Failed'),
    ]

    VISIBILITY_CHOICES = [
        ('public', 'Public'),
        ('private', 'Private'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='media_assets',
    )
    owner_role = models.CharField(max_length=10, choices=OWNER_ROLE_CHOICES, default='system')

    question = models.ForeignKey(
        'academics.Question', on_delete=models.SET_NULL, null=True, blank=True, related_name='media_assets',
    )
    option = models.ForeignKey(
        'academics.Option', on_delete=models.SET_NULL, null=True, blank=True, related_name='media_assets',
    )

    image_type = models.CharField(max_length=32, choices=IMAGE_TYPE_CHOICES)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='other')

    # GCS object key prefix for this asset, e.g. "questions/img_<uuid>/" —
    # actual files live at "{bucket}/{storage_key}original.<ext>" and
    # "{bucket}/{storage_key}{width}.webp" etc (see processing.py).
    storage_key = models.CharField(max_length=512)
    bucket = models.CharField(max_length=100, blank=True, help_text='Which GCS bucket the original lives in.')

    original_filename = models.CharField(max_length=255, blank=True)
    mime_type = models.CharField(max_length=100, blank=True)
    format = models.CharField(max_length=10, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    file_size = models.PositiveIntegerField(null=True, blank=True)

    content_hash = models.CharField(max_length=64, db_index=True, blank=True)

    processing_status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='uploading')
    processing_error = models.TextField(blank=True)
    processing_attempts = models.PositiveIntegerField(default=0)

    visibility = models.CharField(max_length=10, choices=VISIBILITY_CHOICES, default='private')

    # {"480": "questions/img_<uuid>/480.webp", "768": "...", "2400": "...",
    #  "480_avif": "questions/img_<uuid>/480.avif", ...}
    variants = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['content_hash']),
            models.Index(fields=['processing_status']),
            models.Index(fields=['image_type']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.image_type}:{self.id}'
