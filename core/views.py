import uuid

from django.core.files.storage import default_storage
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from courses.models import Course
from hamromentor.permissions import IsAdminRoleOrAboveOrReadOnly

from .models import Announcement, Banner, HomeFeature, MCQOfTheDay, SiteLink, SiteSettings
from .serializers import (
    AnnouncementSerializer,
    BannerSerializer,
    HomeFeatureSerializer,
    HomepageContentSerializer,
    MCQOfTheDaySerializer,
    SiteLinkSerializer,
    SiteSettingsSerializer,
)


class RichImageUploadView(APIView):
    """Generic image upload for rich-text content (question/option/explanation
    editors) — returns a URL to embed inline, independent of any specific model."""
    permission_classes = [IsAdminUser]

    def post(self, request):
        file = request.FILES.get('file')
        if not file:
            return Response({'detail': 'No file uploaded.'}, status=400)
        ext = file.name.rsplit('.', 1)[-1] if '.' in file.name else 'png'
        path = default_storage.save(f'rich_text/{uuid.uuid4().hex}.{ext}', file)
        return Response({'url': request.build_absolute_uri(default_storage.url(path)), 'size': file.size})


def _select_relevant_mcq(queryset, active_course_id):
    """From an already-ordered MCQOfTheDay queryset, return the first entry
    whose question is tagged to active_course_id, else the first whose
    question has no course tags at all (applies to everyone), else None —
    never a question tagged to a *different* course than the viewer's."""
    global_fallback = None
    for entry in queryset:
        course_ids = {c.id for c in entry.question.courses.all()}
        if active_course_id and active_course_id in course_ids:
            return entry
        if not course_ids and global_fallback is None:
            global_fallback = entry
    return global_fallback


class HomeDashboardView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        banners = Banner.objects.filter(is_active=True)
        announcement = Announcement.objects.filter(is_active=True).first()

        active_course_id = request.user.active_course_id if request.user.is_authenticated else None
        mcq_base = MCQOfTheDay.objects.select_related('question').prefetch_related('question__courses')
        mcq = _select_relevant_mcq(mcq_base.filter(date=timezone.localdate()), active_course_id)
        if not mcq:
            mcq = _select_relevant_mcq(mcq_base.order_by('-date')[:60], active_course_id)

        return Response({
            'banners': BannerSerializer(banners, many=True, context={'request': request}).data,
            'mcq_of_the_day': MCQOfTheDaySerializer(mcq, context={'request': request}).data if mcq else None,
            'announcement': AnnouncementSerializer(announcement).data if announcement else None,
        })


class HomepageContentView(APIView):
    """Everything the public marketing homepage needs, in one request."""
    permission_classes = [AllowAny]

    def get(self, request):
        data = {
            'settings': SiteSettings.load(),
            'features': HomeFeature.objects.all(),
            'courses': Course.objects.filter(is_active=True).order_by('program_group', 'order', 'name'),
            'nav_links': SiteLink.objects.filter(section='nav'),
            'footer_links': SiteLink.objects.filter(section='footer'),
        }
        return Response(HomepageContentSerializer(data, context={'request': request}).data)


class SiteSettingsView(APIView):
    permission_classes = [IsAdminRoleOrAboveOrReadOnly]

    def get(self, request):
        return Response(SiteSettingsSerializer(SiteSettings.load(), context={'request': request}).data)

    def patch(self, request):
        settings = SiteSettings.load()
        serializer = SiteSettingsSerializer(settings, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class HomeFeatureViewSet(viewsets.ModelViewSet):
    queryset = HomeFeature.objects.all()
    serializer_class = HomeFeatureSerializer
    permission_classes = [IsAdminRoleOrAboveOrReadOnly]


class SiteLinkViewSet(viewsets.ModelViewSet):
    queryset = SiteLink.objects.all()
    serializer_class = SiteLinkSerializer
    permission_classes = [IsAdminRoleOrAboveOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        section = self.request.query_params.get('section')
        if section:
            qs = qs.filter(section=section)
        return qs
