from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from hamromentor.permissions import IsStaffOrReadOnly, IsStaffOrReadOnlyExcludingTeacherWrites

from .excel import import_workbook, template_response
from .models import Chapter, Option, Question, QuestionAttempt, Subject, Topic
from .serializers import (
    AnswerSubmitSerializer,
    ChapterSerializer,
    QuestionAdminSerializer,
    QuestionSerializer,
    SubjectDetailSerializer,
    SubjectListSerializer,
    TopicSerializer,
)


class SubjectViewSet(viewsets.ModelViewSet):
    queryset = Subject.objects.all().prefetch_related('courses')
    permission_classes = [IsStaffOrReadOnlyExcludingTeacherWrites]
    lookup_field = 'slug'

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return SubjectDetailSerializer
        return SubjectListSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        course_id = self.request.query_params.get('course')
        if course_id:
            qs = qs.filter(courses__id=course_id)
        return qs.distinct()


class ChapterViewSet(viewsets.ModelViewSet):
    queryset = Chapter.objects.all()
    serializer_class = ChapterSerializer
    permission_classes = [IsStaffOrReadOnlyExcludingTeacherWrites]

    def get_queryset(self):
        qs = super().get_queryset()
        subject_slug = self.request.query_params.get('subject')
        if subject_slug:
            qs = qs.filter(subject__slug=subject_slug)
        return qs


class TopicViewSet(viewsets.ModelViewSet):
    queryset = Topic.objects.all()
    serializer_class = TopicSerializer
    permission_classes = [IsStaffOrReadOnlyExcludingTeacherWrites]

    def get_queryset(self):
        qs = super().get_queryset()
        chapter_id = self.request.query_params.get('chapter')
        if chapter_id:
            qs = qs.filter(chapter_id=chapter_id)
        return qs


class QuestionViewSet(viewsets.ModelViewSet):
    queryset = Question.objects.all().select_related('subject', 'chapter').prefetch_related('options')
    permission_classes = [IsStaffOrReadOnly]

    def get_serializer_class(self):
        if self.request.user.is_authenticated and self.request.user.is_staff:
            return QuestionAdminSerializer
        return QuestionSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        subject = self.request.query_params.get('subject')
        chapter = self.request.query_params.get('chapter')  # Chapter model — "Unit" in the UI
        topic = self.request.query_params.get('topic')  # Topic model — "Chapter" in the UI
        year = self.request.query_params.get('year')
        course = self.request.query_params.get('course')
        teacher = self.request.query_params.get('teacher')
        search = self.request.query_params.get('search')
        if subject:
            qs = qs.filter(subject__slug=subject)
        if chapter:
            qs = qs.filter(chapter_id=chapter)
        if topic:
            qs = qs.filter(topic_id=topic)
        if year:
            qs = qs.filter(year=year)
        if course:
            qs = qs.filter(courses__id=course)
        if teacher:
            qs = qs.filter(created_by_id=teacher)
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(public_id__icontains=search) | Q(text__icontains=search)
                | Q(subject__name__icontains=search) | Q(chapter__name__icontains=search)
                | Q(topic__name__icontains=search)
            )

        user = self.request.user
        if not (user.is_authenticated and user.is_staff):
            from billing.access import has_qbank_access
            locked_subject_ids = [s.id for s in Subject.objects.all() if not has_qbank_access(user, s)]
            if locked_subject_ids:
                qs = qs.exclude(subject_id__in=locked_subject_ids)
        elif getattr(user, 'admin_role', None) == 'teacher' and not user.can_manage_all_content:
            qs = qs.filter(created_by=user)
        return qs.distinct()

    @action(detail=False, methods=['get'], permission_classes=[IsAdminUser])
    def summary(self, request):
        """Question Bank Summary page: total count + a by-course breakdown.
        Subject/Unit/Chapter breakdowns are served by the existing /subjects/
        (and /subjects/{slug}/) endpoints' question_count/mcq_count fields."""
        from django.db.models import Count

        from courses.models import Course

        courses = Course.objects.annotate(question_count=Count('questions')).order_by('-question_count')
        return Response({
            'total_questions': Question.objects.count(),
            'by_course': [
                {'id': c.id, 'name': c.name, 'question_count': c.question_count} for c in courses
            ],
        })

    @action(detail=True, methods=['patch'], permission_classes=[IsAdminUser])
    def upload_images(self, request, pk=None):
        """One multipart call for every image a question can carry: its own
        image, the explanation image, and up to 4 option images (option_image_0..3)."""
        question = self.get_object()
        if 'image' in request.FILES:
            question.image = request.FILES['image']
        if 'explanation_image' in request.FILES:
            question.explanation_image = request.FILES['explanation_image']
        question.save()

        options = list(question.options.order_by('order'))
        for i, opt in enumerate(options):
            key = f'option_image_{i}'
            if key in request.FILES:
                opt.image = request.FILES[key]
                opt.save()

        return Response(QuestionAdminSerializer(question, context={'request': request}).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def answer(self, request, pk=None):
        question = self.get_object()
        serializer = AnswerSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        option_id = serializer.validated_data.get('option_id')
        bookmark = serializer.validated_data.get('bookmark', False)

        selected_option = None
        is_correct = False
        if option_id:
            selected_option = get_object_or_404(Option, pk=option_id, question=question)
            is_correct = selected_option.is_correct

        attempt, _ = QuestionAttempt.objects.update_or_create(
            user=request.user, question=question,
            defaults={
                'selected_option': selected_option,
                'is_correct': is_correct,
                'is_bookmarked': bookmark,
            },
        )

        correct_option = question.options.filter(is_correct=True).first()
        return Response({
            'is_correct': is_correct,
            'correct_option_id': correct_option.id if correct_option else None,
            'explanation': question.explanation,
            'explanation_image': request.build_absolute_uri(question.explanation_image.url) if question.explanation_image else None,
            'explanation_latex': question.explanation_latex,
            'explanation_video_url': question.explanation_video_url,
            'references': question.references,
        })


class QuestionExcelTemplateView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        return template_response()


class QuestionExcelImportView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request):
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({'detail': 'No file uploaded.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            summary = import_workbook(file_obj)
        except Exception as exc:  # noqa: BLE001
            return Response({'detail': f'Could not read that file: {exc}'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(summary)
