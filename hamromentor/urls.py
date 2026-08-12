from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/auth/', include('accounts.urls')),
    path('api/', include('academics.urls')),
    path('api/', include('tests_app.urls')),
    path('api/', include('videos_app.urls')),
    path('api/', include('core.urls')),
    path('api/', include('courses.urls')),
    path('api/', include('billing.urls')),
    path('api/', include('marketplace.urls')),
    path('api/media/', include('media_library.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
