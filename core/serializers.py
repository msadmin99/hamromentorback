from rest_framework import serializers

from academics.serializers import QuestionSerializer
from courses.models import Course

from .models import Announcement, Banner, HomeFeature, MCQOfTheDay, SiteLink, SiteSettings


class BannerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Banner
        fields = ['id', 'title', 'subtitle', 'tag', 'image', 'link_url', 'background_color', 'order']


class MCQOfTheDaySerializer(serializers.ModelSerializer):
    question = QuestionSerializer(read_only=True)

    class Meta:
        model = MCQOfTheDay
        fields = ['date', 'label', 'question']


class AnnouncementSerializer(serializers.ModelSerializer):
    class Meta:
        model = Announcement
        fields = ['id', 'message', 'coupon_code']


class SiteSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = SiteSettings
        fields = [
            'app_badge_text', 'nav_cta_text',
            'hero_headline', 'hero_subtitle', 'hero_cta_primary_text', 'hero_cta_primary_link',
            'hero_cta_secondary_text', 'hero_cta_secondary_link', 'hero_badge_icon', 'hero_badge_title',
            'hero_badge_tag', 'hero_badge_subtitle', 'hero_badge_cta_text', 'hero_badge_link',
            'stats_icon', 'stats_headline', 'stats_headline_highlight', 'stats_body', 'stats_cta_text',
            'stats_cta_link', 'stats_image',
            'features_heading', 'features_heading_highlight',
            'courses_eyebrow', 'courses_heading', 'courses_subtitle',
            'footer_copyright',
        ]


class HomeFeatureSerializer(serializers.ModelSerializer):
    class Meta:
        model = HomeFeature
        fields = ['id', 'number', 'title', 'body', 'order']


class HomeCourseCardSerializer(serializers.ModelSerializer):
    """Shapes a real `courses.Course` row into the 'Choose your course' card the
    public homepage renders — so adding/editing/deleting a Course in the Admin
    panel is immediately reflected here, with no separate list to keep in sync."""
    label = serializers.CharField(source='name')
    tag = serializers.CharField(source='program_group')
    link_url = serializers.SerializerMethodField()

    class Meta:
        model = Course
        fields = ['id', 'label', 'tag', 'icon', 'color', 'description', 'link_url', 'order']

    def get_link_url(self, obj):
        return f'/register?course={obj.prefix}'


class SiteLinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = SiteLink
        fields = ['id', 'section', 'label', 'url', 'order']


class HomepageContentSerializer(serializers.Serializer):
    """Aggregate, read-only shape served to the public homepage in one call."""
    settings = SiteSettingsSerializer()
    features = HomeFeatureSerializer(many=True)
    courses = HomeCourseCardSerializer(many=True)
    nav_links = SiteLinkSerializer(many=True)
    footer_links = SiteLinkSerializer(many=True)
