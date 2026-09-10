from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AttemptComparativeView,
    AttemptDetailView,
    ExamSessionViewSet,
    ExamTemplateViewSet,
    ExamTypeStatsView,
    GrandTestMissedReviewView,
    GrandTestSeriesView,
    MarkForReviewView,
    FinalizeExpiredAttemptsView,
    MyAttemptsView,
    PerformanceCalendarView,
    QuestionStatsProcessingHandlerView,
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

urlpatterns = [
    path('attempts/mine/', MyAttemptsView.as_view(), name='my-attempts'),
    path('cron/finalize-expired-attempts/', FinalizeExpiredAttemptsView.as_view(), name='cron-finalize-expired-attempts'),
    path('attempts/stats-process/', QuestionStatsProcessingHandlerView.as_view(), name='attempt-stats-process'),
    path('attempts/<int:attempt_id>/', AttemptDetailView.as_view(), name='attempt-detail'),
    path('attempts/<int:attempt_id>/answer/', SubmitAnswerView.as_view(), name='attempt-answer'),
    path('attempts/<int:attempt_id>/mark-review/', MarkForReviewView.as_view(), name='attempt-mark-review'),
    path('attempts/<int:attempt_id>/submit/', SubmitTestView.as_view(), name='attempt-submit'),
    path('attempts/<int:attempt_id>/result/', TestResultView.as_view(), name='attempt-result'),
    path('attempts/<int:attempt_id>/comparative/', AttemptComparativeView.as_view(), name='attempt-comparative'),
    path('tests/<int:pk>/missed-review/', GrandTestMissedReviewView.as_view(), name='test-missed-review'),
    # GT3-6 — must be listed before router.urls below: DefaultRouter's
    # own 'tests/<pk>/$' detail route (TestViewSet has no restricted
    # lookup_value_regex) would otherwise swallow this literal path,
    # treating 'grand-series' as a pk and 404ing inside TestViewSet
    # instead of ever reaching this view.
    path('tests/grand-series/', GrandTestSeriesView.as_view(), name='grand-test-series'),
    path('performance/overview/', StudentPerformanceOverviewView.as_view(), name='performance-overview'),
    path('performance/subjects/<int:subject_id>/', SubjectPerformanceDetailView.as_view(), name='performance-subject-detail'),
    path('performance/calendar/', PerformanceCalendarView.as_view(), name='performance-calendar'),
    path('performance/exam-type/<str:exam_type>/', ExamTypeStatsView.as_view(), name='performance-exam-type'),
] + router.urls
