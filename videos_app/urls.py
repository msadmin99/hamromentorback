from rest_framework.routers import DefaultRouter

from .views import VideoCategoryViewSet, VideoNoteViewSet, VideoResourceViewSet, VideoViewSet

router = DefaultRouter()
router.register('video-categories', VideoCategoryViewSet, basename='video-category')
router.register('video-resources', VideoResourceViewSet, basename='video-resource')
router.register('video-notes', VideoNoteViewSet, basename='video-note')
router.register('videos', VideoViewSet, basename='video')

urlpatterns = router.urls
