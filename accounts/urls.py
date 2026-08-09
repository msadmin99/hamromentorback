from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    AccountSettingsView,
    ActiveCourseView,
    AdminAccountViewSet,
    AdminUserViewSet,
    ChangePasswordView,
    LoginView,
    MeView,
    RegisterView,
    RolePermissionViewSet,
    TeacherListView,
)

router = DefaultRouter()
router.register('users', AdminUserViewSet, basename='admin-user')
router.register('admin-accounts', AdminAccountViewSet, basename='admin-account')
router.register('role-permissions', RolePermissionViewSet, basename='role-permission')

urlpatterns = [
    path('register/', RegisterView.as_view(), name='register'),
    path('login/', LoginView.as_view(), name='login'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('me/', MeView.as_view(), name='me'),
    path('active-course/', ActiveCourseView.as_view(), name='active-course'),
    path('settings/', AccountSettingsView.as_view(), name='account-settings'),
    path('change-password/', ChangePasswordView.as_view(), name='change-password'),
    path('teachers/', TeacherListView.as_view(), name='teachers'),
    path('', include(router.urls)),
]
