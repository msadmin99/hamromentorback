from django.urls import path

from .views import MediaAssetDetailView, MediaProcessingHandlerView, MediaUploadView

urlpatterns = [
    path('upload/', MediaUploadView.as_view(), name='media-upload'),
    path('process/', MediaProcessingHandlerView.as_view(), name='media-process'),
    path('<uuid:pk>/', MediaAssetDetailView.as_view(), name='media-detail'),
]
