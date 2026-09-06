from rest_framework import serializers

from .gcs_storage import cached_signed_url, public_url
from .models import MediaAsset


class MediaAssetSerializer(serializers.ModelSerializer):
    urls = serializers.SerializerMethodField()

    class Meta:
        model = MediaAsset
        fields = [
            'id', 'image_type', 'category', 'width', 'height', 'file_size', 'format',
            'processing_status', 'processing_error', 'visibility', 'urls', 'created_at',
        ]
        read_only_fields = fields

    def get_urls(self, obj):
        if obj.processing_status != 'ready':
            return {}
        urls = {}
        for name, object_key in obj.variants.items():
            if obj.visibility == 'public':
                # Public objects were never signed at all — unaffected by
                # the caching change below, still a static CDN-style URL.
                urls[name] = public_url(object_key)
            else:
                # Scalability audit: was a bare signed_url() call here —
                # a fresh, uncached IAM signBlob round trip on every
                # single serialization (~65-90ms steady-state, up to ~1s
                # cold), confirmed as the dominant cost of Question Bank
                # pages containing real images (260 calls / ~15-18s for
                # one 500-question page with 130 image-bearing
                # questions). cached_signed_url() is a pure wrapper —
                # same signature, same return value, same errors on
                # signing failure, same private/public and authorization
                # behavior (nothing about *who* can reach this code
                # changed) — it only skips re-signing an object_key this
                # or another process already signed within the last 45
                # minutes. See media_library/gcs_storage.py for the full
                # cache/single-flight/Redis-failure design.
                urls[name] = cached_signed_url(_private_bucket(), object_key)
        return urls


def _private_bucket():
    from django.conf import settings
    return settings.MEDIA_GCS_PRIVATE_BUCKET


def resolve_image_data(media_asset, legacy_field):
    """
    Shared by academics.serializers (Question/Option image, explanation_image)
    to expose one consistent shape regardless of whether an image was
    uploaded through the new media_library pipeline (`image_asset`, with
    responsive variants) or is still the legacy plain ImageField — so the
    frontend can render either without caring which path produced it.

    Returns None if there's no image at all, else:
      {"url": <best single fallback url>, "variants": {...} or {}, "width": int|None, "height": int|None}
    """
    if media_asset is not None and media_asset.processing_status == 'ready':
        serialized = MediaAssetSerializer(media_asset).data
        urls = serialized['urls']
        # Prefer the largest webp variant as the plain fallback `url`.
        webp_urls = {k: v for k, v in urls.items() if k.endswith('_webp')}
        fallback = None
        if webp_urls:
            largest_key = max(webp_urls, key=lambda k: int(k.split('_')[0]))
            fallback = webp_urls[largest_key]
        return {
            'url': fallback,
            'variants': urls,
            'width': media_asset.width,
            'height': media_asset.height,
        }
    if legacy_field:
        try:
            return {'url': legacy_field.url, 'variants': {}, 'width': None, 'height': None}
        except ValueError:
            return None
    return None
