from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    BatchViewSet,
    CoursePackageViewSet,
    CourseViewSet,
    DashboardStatsView,
    EnrollmentRequestViewSet,
    EnrollmentViewSet,
    MyEnrollmentsView,
    PruneExpiredPackagesView,
)

router = DefaultRouter()
router.register('courses', CourseViewSet, basename='course')
router.register('course-packages', CoursePackageViewSet, basename='course-package')
router.register('batches', BatchViewSet, basename='batch')
router.register('enrollments', EnrollmentViewSet, basename='enrollment')
router.register('enrollment-requests', EnrollmentRequestViewSet, basename='enrollment-request')

urlpatterns = [
    path('enrollments/mine/', MyEnrollmentsView.as_view(), name='my-enrollments'),
    path('dashboard-stats/', DashboardStatsView.as_view(), name='dashboard-stats'),
    path('cron/prune-expired-packages/', PruneExpiredPackagesView.as_view(), name='cron-prune-packages'),
] + router.urls
