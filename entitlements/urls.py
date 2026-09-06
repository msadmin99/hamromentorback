from django.urls import path

from .views import MyEntitlementsView, SubjectQbankAccessView, TestAccessView

urlpatterns = [
    path('entitlements/mine/', MyEntitlementsView.as_view(), name='entitlements-mine'),
    path('entitlements/tests/<int:test_id>/access/', TestAccessView.as_view(), name='entitlements-test-access'),
    path(
        'entitlements/subjects/<int:subject_id>/qbank-access/',
        SubjectQbankAccessView.as_view(), name='entitlements-qbank-access',
    ),
]
