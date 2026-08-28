from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AttemptComparativeView,
    AttemptDetailView,
    ExamSessionViewSet,
    ExamTemplateViewSet,
    ExamTypeStatsView,
    MyAttemptsView,
    PerformanceCalendarView,
    SavedExamViewViewSet,
    StudentPerformanceOverviewView,
    SubjectPerformanceDetailView,
    SubmitAnswerView,
    SubmitTestView,
    TestResultView,
    TestViewSet,
)

router = DefaultRouter()
router.register('tests', TestViewSet, basename='test')
router.register('exam-templates', ExamTemplateViewSet, basename='exam-template')
router.register('exam-sessions', ExamSessionViewSet, basename='exam-session')
router.register('saved-exam-views', SavedExamViewViewSet, basename='saved-exam-view')

urlpatterns = router.urls + [
    path('attempts/mine/', MyAttemptsView.as_view(), name='my-attempts'),
    path('attempts/<int:attempt_id>/', AttemptDetailView.as_view(), name='attempt-detail'),
    path('attempts/<int:attempt_id>/answer/', SubmitAnswerView.as_view(), name='attempt-answer'),
    path('attempts/<int:attempt_id>/submit/', SubmitTestView.as_view(), name='attempt-submit'),
    path('attempts/<int:attempt_id>/result/', TestResultView.as_view(), name='attempt-result'),
    path('attempts/<int:attempt_id>/comparative/', AttemptComparativeView.as_view(), name='attempt-comparative'),
    path('performance/overview/', StudentPerformanceOverviewView.as_view(), name='performance-overview'),
    path('performance/subjects/<int:subject_id>/', SubjectPerformanceDetailView.as_view(), name='performance-subject-detail'),
    path('performance/calendar/', PerformanceCalendarView.as_view(), name='performance-calendar'),
    path('performance/exam-type/<str:exam_type>/', ExamTypeStatsView.as_view(), name='performance-exam-type'),
]
