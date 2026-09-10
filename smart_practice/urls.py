from django.urls import path

from .views import (
    EligibilityView,
    GrandTestPracticeSessionCreateView,
    RecommendationsView,
    SessionAnswerView,
    SessionCompleteView,
    SessionCreateView,
    SessionDetailView,
)

urlpatterns = [
    path('student/smart-practice/eligibility/', EligibilityView.as_view(), name='smart-practice-eligibility'),
    path('student/smart-practice/recommendations/', RecommendationsView.as_view(), name='smart-practice-recommendations'),
    path('student/smart-practice/sessions/', SessionCreateView.as_view(), name='smart-practice-session-create'),
    path('student/smart-practice/sessions/<int:session_id>/', SessionDetailView.as_view(), name='smart-practice-session-detail'),
    path('student/smart-practice/sessions/<int:session_id>/answer/', SessionAnswerView.as_view(), name='smart-practice-session-answer'),
    path('student/smart-practice/sessions/<int:session_id>/complete/', SessionCompleteView.as_view(), name='smart-practice-session-complete'),
    # GT3-6 — a separate, narrower creation route for a Grand Test's own
    # 'Practice Now' CTA; see GrandTestPracticeSessionCreateView's own
    # docstring for why this is intentionally not folded into the plain
    # sessions/ endpoint above.
    path(
        'student/smart-practice/grand-test-sessions/', GrandTestPracticeSessionCreateView.as_view(),
        name='smart-practice-grand-test-session-create',
    ),
]
