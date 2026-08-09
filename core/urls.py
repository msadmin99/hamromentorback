from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    HomeDashboardView,
    HomeFeatureViewSet,
    HomepageContentView,
    RichImageUploadView,
    SiteLinkViewSet,
    SiteSettingsView,
)

router = DefaultRouter()
router.register('home-features', HomeFeatureViewSet, basename='home-feature')
router.register('site-links', SiteLinkViewSet, basename='site-link')

urlpatterns = [
    path('home/', HomeDashboardView.as_view(), name='home-dashboard'),
    path('homepage/', HomepageContentView.as_view(), name='homepage-content'),
    path('site-settings/', SiteSettingsView.as_view(), name='site-settings'),
    path('uploads/image/', RichImageUploadView.as_view(), name='rich-image-upload'),
] + router.urls
