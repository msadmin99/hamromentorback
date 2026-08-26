from django.urls import path
from rest_framework.routers import DefaultRouter

from .import_views import (
    ImportBatchCreateTestView,
    ImportBatchListView,
    ImportBatchRowsView,
    ImportBatchTaxonomyView,
    ImportConfirmView,
    ImportRollbackView,
    ImportRowDetailView,
    ImportStatusView,
    ImportTemplateView,
    ImportUploadView,
)
from .views import (
    ChapterViewSet,
    QuestionExcelImportView,
    QuestionExcelTemplateView,
    QuestionReportViewSet,
    QuestionViewSet,
    ReferenceBookViewSet,
    SubjectViewSet,
    TopicViewSet,
)

router = DefaultRouter()
router.register('subjects', SubjectViewSet, basename='subject')
router.register('chapters', ChapterViewSet, basename='chapter')
router.register('topics', TopicViewSet, basename='topic')
router.register('questions', QuestionViewSet, basename='question')
router.register('reference-books', ReferenceBookViewSet, basename='reference-book')
router.register('question-reports', QuestionReportViewSet, basename='question-report')

urlpatterns = [
    path('questions-excel/template/', QuestionExcelTemplateView.as_view(), name='question-excel-template'),
    path('questions-excel/import/', QuestionExcelImportView.as_view(), name='question-excel-import'),

    path('import-batches/upload/', ImportUploadView.as_view(), name='import-upload'),
    path('import-batches/', ImportBatchListView.as_view(), name='import-batch-list'),
    path('import-batches/<int:batch_id>/rows/', ImportBatchRowsView.as_view(), name='import-batch-rows'),
    path('import-batches/<int:batch_id>/taxonomy/', ImportBatchTaxonomyView.as_view(), name='import-batch-taxonomy'),
    path('import-batches/<int:batch_id>/rows/<int:row_id>/', ImportRowDetailView.as_view(), name='import-row-detail'),
    path('import-batches/<int:batch_id>/confirm/', ImportConfirmView.as_view(), name='import-confirm'),
    path('import-batches/<int:batch_id>/create-test/', ImportBatchCreateTestView.as_view(), name='import-batch-create-test'),
    path('import-batches/<int:batch_id>/status/', ImportStatusView.as_view(), name='import-status'),
    path('import-batches/<int:batch_id>/rollback/', ImportRollbackView.as_view(), name='import-rollback'),
    path('import-templates/<str:file_format>/', ImportTemplateView.as_view(), name='import-template'),
] + router.urls
