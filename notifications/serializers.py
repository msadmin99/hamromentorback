from rest_framework import serializers

from .events import MANDATORY_CATEGORIES
from .models import Notification, NotificationPreference, PushSubscription


class NotificationSerializer(serializers.ModelSerializer):
    course_name = serializers.CharField(source='course.name', read_only=True, default=None)
    is_read = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            'id', 'event_type', 'category', 'priority', 'title', 'body', 'action_url',
            'course', 'course_name', 'metadata', 'created_at', 'read_at', 'clicked_at', 'is_read',
        ]
        read_only_fields = fields

    def get_is_read(self, obj):
        return obj.read_at is not None


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPreference
        fields = ['id', 'category', 'channel', 'course', 'enabled']
        read_only_fields = ['id']

    def validate(self, attrs):
        # Server-side enforcement (§27, §39's spirit — never rely on the
        # frontend UI alone to hide the toggle): a mandatory category can be
        # *stored* as a row (harmless), but never saved with enabled=False,
        # since services.is_channel_enabled() would ignore it anyway — this
        # just stops a confusing "off" toggle that has no actual effect
        # from ever being persisted in the first place.
        category = attrs.get('category', getattr(self.instance, 'category', None))
        enabled = attrs.get('enabled', True)
        if category in MANDATORY_CATEGORIES and not enabled:
            raise serializers.ValidationError(
                {'enabled': f'"{category}" notifications are mandatory and cannot be disabled.'}
            )
        return attrs


class PushSubscriptionRegisterSerializer(serializers.Serializer):
    """Input-only — validates the raw payload the browser's own
    `pushManager.subscribe()` result produces. Deliberately a plain
    Serializer, not a ModelSerializer: `user` is never accepted from the
    client (P0-04 — derived from request.user in the view), so there is no
    model instance this serializer should ever be allowed to construct or
    update directly."""
    endpoint = serializers.URLField(max_length=500)
    keys = serializers.DictField(child=serializers.CharField())
    device_label = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')

    def validate_keys(self, value):
        missing = {'p256dh', 'auth'} - set(value)
        if missing:
            raise serializers.ValidationError(f'Missing key(s): {", ".join(sorted(missing))}.')
        return value


class PushSubscriptionSerializer(serializers.ModelSerializer):
    """Output-only, for the device-management UI (P1-02). Deliberately
    excludes `endpoint`/`p256dh`/`auth` — a push subscription's
    credentials are never returned to any client once stored (P0-14's
    spirit applies here too: nothing that could resubscribe a browser on a
    student's behalf should ever round-trip back out)."""

    class Meta:
        model = PushSubscription
        fields = ['id', 'browser', 'browser_version', 'os', 'device_label', 'status', 'last_seen_at', 'created_at']
        read_only_fields = fields
