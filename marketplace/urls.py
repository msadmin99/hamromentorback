from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    CourseCategoryViewSet,
    CourseEnrollmentViewSet,
    CourseLessonViewSet,
    CourseSectionViewSet,
    MyCourseEnrollmentsView,
    TeacherApplicationViewSet,
    TeacherCourseViewSet,
)

router = DefaultRouter()
router.register('teacher-applications', TeacherApplicationViewSet, basename='teacher-application')
router.register('course-categories', CourseCategoryViewSet, basename='course-category')
router.register('teacher-courses', TeacherCourseViewSet, basename='teacher-course')
router.register('course-sections', CourseSectionViewSet, basename='course-section')
router.register('course-lessons', CourseLessonViewSet, basename='course-lesson')
router.register('course-enrollments', CourseEnrollmentViewSet, basename='course-enrollment')

urlpatterns = [
    path('course-enrollments/mine/', MyCourseEnrollmentsView.as_view(), name='my-course-enrollments'),
] + router.urls
