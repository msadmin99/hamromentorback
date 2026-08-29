from django.db.models import Count, Exists, OuterRef, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from hamromentor.permissions import IsStaffOrReadOnly, IsStaffOrReadOnlyExcludingTeacherWrites

from .access import locked_subject_ids as _locked_subject_ids
from .access import question_course_scoped as _question_course_scoped
from .excel import import_workbook, template_response
from .models import (
    Chapter, Option, Question, QuestionAttempt, QuestionBankConfig, QuestionDifficultyRating,
    QuestionEvent, QuestionReport, ReferenceBook, Subject, Topic,
)
from .serializers import (
    AnswerSubmitSerializer,
    ChapterSerializer,
    QuestionAdminSerializer,
    QuestionReportAdminSerializer,
    QuestionReportSerializer,
    QuestionSerializer,
    ReferenceBookSerializer,
    SubjectDetailSerializer,
    SubjectListSerializer,
    TopicSerializer,
)
from .services import record_question_result


def _course_scoped(qs, user, *, courses_lookup):
    """Always-applied (not opt-in on a query param) course-eligibility
    filter for non-staff — the same fix already made to Test/Question
    (tests_app.access.visible_test_queryset / academics QuestionViewSet):
    a row with NO courses assigned is treated as shared/ungated (matches
    Subject's own 'can be shared across courses' design and today's real
    data, where every subject is explicitly scoped), a row WITH courses
    assigned is only visible to a student actually enrolled in one of
    them. `courses_lookup` is the ORM path to the M2M from `qs`'s model,
    e.g. 'courses' for Subject itself, 'subject__courses' for Chapter."""
    if user and user.is_authenticated and user.is_staff:
        return qs
    from django.db.models import Q

    from courses.access import eligible_course_ids

    course_ids = eligible_course_ids(user)
    isnull_lookup = f'{courses_lookup}__isnull'
    in_lookup = f'{courses_lookup}__id__in'
    return qs.filter(Q(**{isnull_lookup: True}) | Q(**{in_lookup: course_ids})).distinct()


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
        qs = _course_scoped(qs, self.request.user, courses_lookup='courses')
        course_id = self.request.query_params.get('course')
        if course_id:
            # Narrows within the already-eligible set above for staff too
            # (e.g. the Admin Subjects page filtering by course) — never a
            # substitute for the eligibility filter for non-staff.
            qs = qs.filter(courses__id=course_id)
        return qs.distinct()


class ChapterViewSet(viewsets.ModelViewSet):
    queryset = Chapter.objects.all()
    serializer_class = ChapterSerializer
    permission_classes = [IsStaffOrReadOnlyExcludingTeacherWrites]

    def get_queryset(self):
        qs = super().get_queryset()
        qs = _course_scoped(qs, self.request.user, courses_lookup='subject__courses')
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
        qs = _course_scoped(qs, self.request.user, courses_lookup='chapter__subject__courses')
        chapter_id = self.request.query_params.get('chapter')
        if chapter_id:
            qs = qs.filter(chapter_id=chapter_id)
        return qs


def _status_question_ids(user, statuses, base_qs):
    """Resolves the spec's independent status flags (New/Mastered/Weak/
    Need Practice/Incorrect/Bookmarked/Need Revision) to a set of matching
    question ids, OR'd together — 'New + Incorrect' means either, not both.
    base_qs is the already subject/chapter/topic/difficulty-filtered
    Question queryset, so 'new' only considers questions actually in scope."""
    from django.utils import timezone as tz

    attempt_qs = QuestionAttempt.objects.filter(user=user)
    ids = set()
    if 'new' in statuses:
        attempted_ids = set(attempt_qs.values_list('question_id', flat=True))
        ids |= set(base_qs.exclude(id__in=attempted_ids).values_list('id', flat=True))
    mastery_map = {'mastered': 'mastered', 'weak': 'weak', 'need_practice': 'need_practice', 'learning': 'learning'}
    wanted_mastery = [mastery_map[s] for s in statuses if s in mastery_map]
    if wanted_mastery:
        ids |= set(attempt_qs.filter(mastery_status__in=wanted_mastery).values_list('question_id', flat=True))
    if 'incorrect' in statuses:
        ids |= set(attempt_qs.filter(last_result=False).values_list('question_id', flat=True))
    if 'bookmarked' in statuses:
        ids |= set(attempt_qs.filter(is_bookmarked=True).values_list('question_id', flat=True))
    if 'need_revision' in statuses:
        ids |= set(attempt_qs.filter(revision_due_at__lte=tz.now()).values_list('question_id', flat=True))
    return ids


class _BrowsePagination(PageNumberPagination):
    page_size = 20
    max_page_size = 50
    page_size_query_param = 'page_size'


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
        bookmarked = self.request.query_params.get('bookmarked')
        difficulty = self.request.query_params.get('difficulty')
        question_type = self.request.query_params.get('question_type')
        status_param = self.request.query_params.get('status')
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
                | Q(topic__name__icontains=search) | Q(tags__icontains=search)
            )
        if bookmarked in ('true', '1'):
            if self.request.user.is_authenticated:
                qs = qs.filter(attempts__user=self.request.user, attempts__is_bookmarked=True)
            else:
                qs = qs.none()
        if difficulty:
            from django.db.models import Q
            qs = qs.filter(Q(instructor_difficulty=difficulty) | Q(actual_difficulty=difficulty))
        if question_type:
            qs = qs.filter(question_type=question_type)

        user = self.request.user
        if not (user.is_authenticated and user.is_staff):
            locked_subject_ids = _locked_subject_ids(user)
            if locked_subject_ids:
                qs = qs.exclude(subject_id__in=locked_subject_ids)
            qs = _question_course_scoped(qs, user)
        elif getattr(user, 'admin_role', None) == 'teacher' and not user.can_manage_all_content:
            qs = qs.filter(created_by=user)

        if status_param and user.is_authenticated:
            statuses = [s.strip() for s in status_param.split(',') if s.strip()]
            if statuses:
                qs = qs.filter(id__in=_status_question_ids(user, statuses, qs))

        if user.is_authenticated:
            # Subqueries joined into the main SELECT, not a query per row —
            # QuestionSerializer's get_is_bookmarked/get_mastery_status/
            # get_last_result just read these annotations, so fetching a
            # whole chapter's (or a search page's) worth of questions stays
            # a handful of queries total, not one per question.
            from django.db.models import Subquery

            attempt_for_user = QuestionAttempt.objects.filter(user=user, question=OuterRef('pk'))
            qs = qs.annotate(
                is_bookmarked_by_user=Exists(attempt_for_user.filter(is_bookmarked=True)),
                mastery_status_for_user=Subquery(attempt_for_user.values('mastery_status')[:1]),
                last_result_for_user=Subquery(attempt_for_user.values('last_result')[:1]),
                revision_due_at_for_user=Subquery(attempt_for_user.values('revision_due_at')[:1]),
            )
        return qs.distinct()

    def destroy(self, request, *args, **kwargs):
        """Permanent delete — blocked if the question has practice-attempt
        history or is used in an exam students have already attempted
        (mirrors TestViewSet's own attempt-guard). On success, also cleans
        up every associated image (both the newer MediaAsset pipeline and
        any legacy ImageField) and writes a DeletionAuditLog entry either
        way."""
        from core.deletion_audit import delete_file_field, record_deletion
        from media_library.service import delete_media_asset

        question = self.get_object()
        label = question.public_id

        if question.attempts.exists():
            msg = 'This question has practice-attempt history and cannot be deleted — consider unpublishing it instead.'
            record_deletion(request, 'Question', question.id, label, result='failure', failure_reason=msg)
            return Response({'detail': msg}, status=status.HTTP_400_BAD_REQUEST)

        tests_in_use = question.testquestion_set.filter(test__attempts__isnull=False).select_related('test').distinct()
        if tests_in_use.exists():
            titles = ', '.join(tq.test.title for tq in tests_in_use[:3])
            msg = f'This question is used in an exam with student attempts ({titles}) and cannot be deleted.'
            record_deletion(request, 'Question', question.id, label, result='failure', failure_reason=msg)
            return Response({'detail': msg}, status=status.HTTP_400_BAD_REQUEST)

        options = list(question.options.all())
        media_assets = [question.image_asset, question.explanation_image_asset] + [o.image_asset for o in options]

        try:
            for asset in media_assets:
                if asset:
                    delete_media_asset(asset)
            delete_file_field(question.image)
            delete_file_field(question.explanation_image)
            for opt in options:
                delete_file_field(opt.image)
            response = super().destroy(request, *args, **kwargs)
        except Exception as exc:
            record_deletion(request, 'Question', question.id, label, result='failure', failure_reason=str(exc)[:500])
            return Response({'detail': 'Deletion failed. No partial deletion should remain.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        record_deletion(request, 'Question', question.id, label, result='success')
        return response

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
        """One call for every image a question can carry: its own image, the
        explanation image, and up to 4 option images (option_image_0..3).

        Two ways to set an image, both supported here:
        - Legacy: multipart file under `image`/`explanation_image`/`option_image_{i}`
          — stored directly on the plain ImageField, unoptimized (kept for
          back-compat with any existing caller).
        - New (preferred): `image_asset_id`/`explanation_image_asset_id`/
          `option_image_asset_id_{i}` — the id of an already-processed
          MediaAsset from POST /api/media/upload/ (validated, optimized,
          responsive variants). The Admin question form uploads via that
          endpoint first, polls until ready, then attaches the id here.
        - Clearing: `clear_image`/`clear_explanation_image`/`clear_option_image_{i}`
          (any truthy value) removes both the legacy field and the asset FK —
          used when an admin removes a previously-set image without replacing it.
        """
        from media_library.models import MediaAsset

        def truthy(value):
            return str(value).lower() in ('1', 'true', 'yes')

        question = self.get_object()
        if truthy(request.data.get('clear_image')):
            question.image = None
            question.image_asset = None
        elif 'image' in request.FILES:
            question.image = request.FILES['image']
        elif request.data.get('image_asset_id'):
            question.image_asset = MediaAsset.objects.filter(id=request.data['image_asset_id']).first()

        if truthy(request.data.get('clear_explanation_image')):
            question.explanation_image = None
            question.explanation_image_asset = None
        elif 'explanation_image' in request.FILES:
            question.explanation_image = request.FILES['explanation_image']
        elif request.data.get('explanation_image_asset_id'):
            question.explanation_image_asset = MediaAsset.objects.filter(id=request.data['explanation_image_asset_id']).first()
        question.save()

        options = list(question.options.order_by('order'))
        for i, opt in enumerate(options):
            file_key = f'option_image_{i}'
            asset_key = f'option_image_asset_id_{i}'
            clear_key = f'clear_option_image_{i}'
            changed = False
            if truthy(request.data.get(clear_key)):
                opt.image = None
                opt.image_asset = None
                changed = True
            elif file_key in request.FILES:
                opt.image = request.FILES[file_key]
                changed = True
            elif request.data.get(asset_key):
                opt.image_asset = MediaAsset.objects.filter(id=request.data[asset_key]).first()
                changed = True
            if changed:
                opt.save()

        return Response(QuestionAdminSerializer(question, context={'request': request}).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def answer(self, request, pk=None):
        question = self.get_object()
        serializer = AnswerSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        option_id = serializer.validated_data.get('option_id')
        time_taken_seconds = serializer.validated_data.get('time_taken_seconds')
        confidence = serializer.validated_data.get('confidence') or None

        selected_option = None
        is_correct = False
        if option_id:
            selected_option = get_object_or_404(Option, pk=option_id, question=question)
            is_correct = selected_option.is_correct
            # bookmark is intentionally untouched here — it has its own dedicated
            # action below; folding it into this call previously reset a prior
            # bookmark to False on every plain answer (bool("False") is True, and
            # the serializer's bookmark default is always present in
            # validated_data even when the client never sent the key).
            record_question_result(
                request.user, question, is_correct, source='qbank',
                selected_option=selected_option, time_taken_seconds=time_taken_seconds, confidence=confidence,
            )
            # record_question_result updates Question.total_attempts/correct_attempts
            # and Option.pick_count/pick_percentage via .update() on the DB rows
            # directly (for atomicity under concurrent answers) — the in-memory
            # `question`/its .options here predate that write, so re-fetch fresh.
            question.refresh_from_db(fields=['total_attempts', 'correct_attempts'])

        correct_option = question.options.filter(is_correct=True).first()
        config = QuestionBankConfig.load()
        stats_available = bool(option_id) and question.total_attempts >= config.min_attempts_for_option_stats

        options_payload = None
        if option_id:
            # Option.objects.filter(...), not question.options.all() — the
            # latter reuses this queryset's .prefetch_related('options')
            # cache from before record_question_result() just updated
            # pick_count/pick_percentage via .update(), which would silently
            # serve stale (pre-answer) percentages.
            options_payload = [
                {
                    'id': opt.id,
                    'pick_percentage': opt.pick_percentage if stats_available else None,
                    'explanation': opt.explanation,
                }
                for opt in Option.objects.filter(question_id=question.id).order_by('order')
            ]

        return Response({
            'is_correct': is_correct,
            'correct_option_id': correct_option.id if correct_option else None,
            'explanation': question.explanation,
            'explanation_image': request.build_absolute_uri(question.explanation_image.url) if question.explanation_image else None,
            'explanation_latex': question.explanation_latex,
            'explanation_video_url': question.explanation_video_url,
            'references': question.references,
            'key_takeaway': question.key_takeaway,
            'reference_book_name': question.reference_book.name if question.reference_book_id else '',
            'reference_edition': question.reference_edition,
            'reference_chapter': question.reference_chapter,
            'reference_page': question.reference_page,
            'reference_url': question.reference_url,
            'options': options_payload,
            'stats_available': stats_available,
            'students_correct_percent': (
                round(question.correct_attempts / question.total_attempts * 100) if stats_available else None
            ),
            'total_responses': question.total_attempts if stats_available else None,
        })

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def bookmark(self, request, pk=None):
        """Toggles a bookmark independently of answer() — deliberately a
        separate action, not `answer` called with only `bookmark`, because
        answer()'s update_or_create always writes selected_option/is_correct
        from the request (None/False when option_id is omitted), which would
        silently blank out a previously-recorded answer. Bookmarking must be
        safe to do before, during, or after answering, so this only ever
        touches is_bookmarked."""
        question = self.get_object()
        # bool("False") is True — request.data.get() can come back as a
        # form-encoded string as well as a real JSON boolean, so a plain
        # bool() cast would make "turn bookmark off" silently turn it on.
        is_bookmarked = request.data.get('bookmark') in (True, 'true', 'True', '1', 1)
        QuestionAttempt.objects.update_or_create(
            user=request.user, question=question,
            defaults={'is_bookmarked': is_bookmarked},
        )
        return Response({'is_bookmarked': is_bookmarked})

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def confidence(self, request, pk=None):
        """Self-reported 'how confident were you?' (guess/unsure/confident),
        set AFTER the student has already seen their answer result — a
        second call to `answer` itself would double-count attempts_count via
        record_question_result(), so this is a separate, narrow action that
        only ever touches QuestionAttempt.confidence (same pattern as
        `bookmark` above touching only is_bookmarked)."""
        question = self.get_object()
        value = request.data.get('confidence')
        if value not in dict(QuestionAttempt.CONFIDENCE_CHOICES):
            return Response({'detail': 'Invalid confidence value.'}, status=status.HTTP_400_BAD_REQUEST)
        QuestionAttempt.objects.update_or_create(
            user=request.user, question=question,
            defaults={'confidence': value},
        )
        return Response({'confidence': value})

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def report(self, request, pk=None):
        """Flag a problem with a question for staff review
        (Admin/src/app/question-reports/). self.get_object() already runs
        through get_queryset()'s course-eligibility filter, so a student
        can only report a question they're legitimately allowed to see —
        no separate authorization check needed here."""
        question = self.get_object()
        serializer = QuestionReportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report = QuestionReport.objects.create(
            question=question, user=request.user,
            reason=serializer.validated_data['reason'],
            comment=serializer.validated_data.get('comment', ''),
        )
        return Response(QuestionReportSerializer(report).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='rate-difficulty', permission_classes=[IsAuthenticated])
    def rate_difficulty(self, request, pk=None):
        """A student's own subjective difficulty rating — separate from the
        objectively-computed Question.actual_difficulty. Re-rating updates
        the same row rather than accumulating duplicates."""
        question = self.get_object()
        rating = request.data.get('rating')
        if rating not in dict(QuestionDifficultyRating.RATING_CHOICES):
            return Response({'detail': 'Invalid rating.'}, status=status.HTTP_400_BAD_REQUEST)
        QuestionDifficultyRating.objects.update_or_create(
            question=question, user=request.user, defaults={'rating': rating},
        )
        return Response({'rating': rating})

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def browse(self, request):
        """Paginated variant of the list endpoint for the Question Bank
        search/browse UI — deliberately a separate opt-in action rather than
        turning on pagination globally, since every existing caller of
        GET /questions/ (QuestionSolver, the bookmarks page, the Admin
        QuestionPicker) expects a plain array back."""
        qs = self.filter_queryset(self.get_queryset())
        paginator = _BrowsePagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        serializer = self.get_serializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def dashboard(self, request):
        """Question Bank dashboard stat cards — total/attempted/correct/
        incorrect/accuracy/bookmarked/mastered/need-revision, scoped to an
        optional subject/course. Two aggregate queries, not one per question."""
        user = request.user
        subject = request.query_params.get('subject')
        course = request.query_params.get('course')

        question_qs = _question_course_scoped(Question.objects.all(), user)
        locked = _locked_subject_ids(user)
        if locked:
            question_qs = question_qs.exclude(subject_id__in=locked)
        if subject:
            question_qs = question_qs.filter(subject__slug=subject)
        if course:
            # Narrows within the already-eligible set above — never a
            # substitute for it (see QuestionViewSet.get_queryset for the
            # same principle applied to the main listing/search endpoint).
            # courses__id=course alone would match nothing for a question
            # relying on its Subject's scope (courses__isnull=True case),
            # same reasoning as _question_course_scoped above.
            from django.db.models import Q as Q_course
            question_qs = question_qs.filter(
                Q_course(courses__id=course) | Q_course(courses__isnull=True, subject__courses__id=course)
            )
        total_questions = question_qs.distinct().count()

        attempt_qs = QuestionAttempt.objects.filter(user=user, attempts_count__gt=0)
        if subject:
            attempt_qs = attempt_qs.filter(question__subject__slug=subject)
        if course:
            attempt_qs = attempt_qs.filter(
                Q_course(question__courses__id=course)
                | Q_course(question__courses__isnull=True, question__subject__courses__id=course)
            )

        attempted = attempt_qs.count()
        correct = attempt_qs.filter(last_result=True).count()
        incorrect = attempt_qs.filter(last_result=False).count()
        accuracy = round(correct / attempted * 100, 2) if attempted else 0.0
        topics_practiced = attempt_qs.exclude(question__topic__isnull=True).values('question__topic').distinct().count()

        # QBank-only study time (source='qbank') — deliberately excludes Test
        # Mode time, since this dashboard is QBank-scoped throughout; test
        # time already has its own home in kpi_overview()'s total_study_seconds.
        event_qs = QuestionEvent.objects.filter(user=user, source='qbank')
        if subject:
            event_qs = event_qs.filter(question__subject__slug=subject)
        if course:
            event_qs = event_qs.filter(
                Q_course(question__courses__id=course)
                | Q_course(question__courses__isnull=True, question__subject__courses__id=course)
            )
        study_seconds = event_qs.aggregate(total=Sum('time_taken_seconds'))['total'] or 0

        return Response({
            'total_questions': total_questions,
            'attempted': attempted,
            'new': max(total_questions - attempted, 0),
            'correct': correct,
            'incorrect': incorrect,
            'accuracy': accuracy,
            'bookmarked': attempt_qs.filter(is_bookmarked=True).count(),
            'mastered': attempt_qs.filter(mastery_status='mastered').count(),
            'weak': attempt_qs.filter(mastery_status='weak').count(),
            'need_practice': attempt_qs.filter(mastery_status__in=['need_practice', 'learning']).count(),
            'need_revision': attempt_qs.filter(revision_due_at__lte=timezone.now()).count(),
            'topics_practiced': topics_practiced,
            'study_seconds': study_seconds,
        })

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def mistakes(self, request):
        """Mistake Bank: subject-wise counts of currently-wrong questions,
        plus a filtered/ordered list. scope=frequent orders by how many
        times this question has been gotten wrong; scope=recent uses the
        QuestionEvent log (QuestionAttempt.answered_at is set only once, on
        first attempt, so it can't answer "most recently wrong")."""
        user = request.user
        locked = _locked_subject_ids(user)

        base = QuestionAttempt.objects.filter(user=user, last_result=False)
        if locked:
            base = base.exclude(question__subject_id__in=locked)

        by_subject = list(
            base.values('question__subject_id', 'question__subject__name')
            .annotate(count=Count('id')).order_by('-count')
        )

        qs = base.select_related('question', 'question__subject', 'question__chapter')
        subject = request.query_params.get('subject')
        chapter = request.query_params.get('chapter')
        if subject:
            qs = qs.filter(question__subject__slug=subject)
        if chapter:
            qs = qs.filter(question__chapter_id=chapter)

        scope = request.query_params.get('scope', 'all')
        if scope == 'frequent':
            attempts = list(qs.order_by('-incorrect_count')[:100])
        elif scope == 'recent':
            recent_qids = list(
                QuestionEvent.objects.filter(user=user, is_correct=False)
                .order_by('-created_at').values_list('question_id', flat=True)[:300]
            )
            seen = set()
            ordered_ids = [qid for qid in recent_qids if not (qid in seen or seen.add(qid))]
            by_qid = {a.question_id: a for a in qs.filter(question_id__in=ordered_ids)}
            attempts = [by_qid[qid] for qid in ordered_ids if qid in by_qid][:100]
        else:
            attempts = list(qs.order_by('-incorrect_count')[:100])

        questions = [a.question for a in attempts]
        for q in questions:
            q.is_bookmarked_by_user = next((a.is_bookmarked for a in attempts if a.question_id == q.id), False)

        return Response({
            'by_subject': [
                {'subject_id': row['question__subject_id'], 'subject_name': row['question__subject__name'], 'count': row['count']}
                for row in by_subject
            ],
            'results': QuestionSerializer(questions, many=True, context={'request': request}).data,
        })

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def recommended(self, request):
        """QBank-specific sibling of tests_app.performance.recommendations()
        — same rule-based philosophy and underlying aggregation (weakest
        subjects, weak topics), but pointed at a QBank practice session
        instead of a Test/Video, since that's what this dashboard should
        drive the student into. Falls back to a plain "start practicing"
        nudge when there isn't enough data yet — never fabricated examples."""
        from tests_app.performance import subject_breakdown, topic_mastery

        user = request.user
        course = request.query_params.get('course')
        course_id = int(course) if course else None

        subjects = subject_breakdown(user, course_id)
        attempted = [s for s in subjects if s['attempted'] >= 3]
        weakest = sorted(attempted, key=lambda s: s['accuracy'])[:2]

        suggestions = []
        for s in weakest:
            topics = topic_mastery(user, s['subject_id'])
            weak_topics = sorted([t for t in topics if t['mastery'] == 'weak'], key=lambda t: t['accuracy'])
            if weak_topics:
                t = weak_topics[0]
                weak_count = QuestionAttempt.objects.filter(
                    user=user, mastery_status='weak', question__topic_id=t['topic_id'],
                ).count()
                suggestions.append({
                    'type': 'revise_topic', 'subject_id': s['subject_id'], 'subject_name': s['subject_name'],
                    'topic_id': t['topic_id'], 'topic_name': t['topic_name'], 'count': weak_count,
                    'accuracy': t['accuracy'],
                    'message': f"Revise {t['topic_name']} — {weak_count} weak question{'s' if weak_count != 1 else ''}",
                    'practice_params': {'subject': s['subject_id'], 'topic': t['topic_id'], 'status': 'weak'},
                })
            else:
                suggestions.append({
                    'type': 'improve_subject', 'subject_id': s['subject_id'], 'subject_name': s['subject_name'],
                    'accuracy': s['accuracy'],
                    'message': f"Practice {s['subject_name']} — accuracy {s['accuracy']}%",
                    'practice_params': {'subject': s['subject_id']},
                })

        mistake_count = QuestionAttempt.objects.filter(user=user, last_result=False).count()
        if mistake_count:
            suggestions.append({
                'type': 'retry_mistakes', 'count': mistake_count,
                'message': f"Retry your recent mistakes — {mistake_count} question{'s' if mistake_count != 1 else ''}",
                'practice_params': {'status': 'incorrect'},
            })

        new_subject_qs = _course_scoped(Subject.objects.all(), user, courses_lookup='courses')
        locked = _locked_subject_ids(user)
        if locked:
            new_subject_qs = new_subject_qs.exclude(id__in=locked)
        attempted_subject_ids = set(QuestionAttempt.objects.filter(user=user).values_list('question__subject_id', flat=True))
        never_touched = new_subject_qs.exclude(id__in=attempted_subject_ids).first()
        if never_touched:
            new_count = never_touched.questions.count()
            if new_count:
                suggestions.append({
                    'type': 'new_subject', 'subject_id': never_touched.id, 'subject_name': never_touched.name, 'count': new_count,
                    'message': f"New {never_touched.name} Questions — {new_count} question{'s' if new_count != 1 else ''}",
                    'practice_params': {'subject': never_touched.id, 'status': 'new'},
                })

        if not suggestions:
            suggestions.append({
                'type': 'start_new', 'message': 'Start with New Questions',
                'practice_params': {'status': 'new'},
            })

        # Normalized accuracy_pct/question_count/estimated_minutes on the TOP
        # suggestion only — this is what the "Your Next Practice" hero card
        # reads (accuracy ring + "~N min" + "N Questions"). Only computed for
        # suggestions[0] since it's the only one ever rendered as the hero;
        # the raw per-type suggestion dicts above are unchanged for every
        # other existing caller of this endpoint.
        top = suggestions[0]
        top['question_count'] = top.get('count')
        top['accuracy_pct'] = top.get('accuracy')
        if top['question_count'] is None and top['type'] == 'improve_subject':
            top['question_count'] = QuestionAttempt.objects.filter(
                user=user, mastery_status__in=['weak', 'need_practice'], question__subject_id=top.get('subject_id'),
            ).count()
        top['estimated_minutes'] = max(5, round(top['question_count'] * 1.25)) if top['question_count'] else None

        return Response({'suggestions': suggestions[:4], 'note': 'Rule-based suggestions from your own performance data.'})

    @action(detail=False, methods=['post'], url_path='practice-session', permission_classes=[IsAuthenticated])
    def practice_session(self, request):
        """Practice Session Builder's backend — given exam/subject/chapter/
        topic(s)/difficulty/status/count, returns a bounded question list for
        the existing QuestionSolver to consume. No second quiz engine."""
        from django.db.models import Q as Q_

        user = request.user
        data = request.data

        qs = _question_course_scoped(
            Question.objects.all().select_related('subject', 'chapter').prefetch_related('options'),
            user,
        )
        locked = _locked_subject_ids(user)
        if locked:
            qs = qs.exclude(subject_id__in=locked)
        if data.get('course'):
            # Narrows within the already-eligible set above — a client
            # sending a course id the student isn't enrolled in can no
            # longer widen the base queryset past it (this action used to
            # build Question.objects.all() directly with no eligibility
            # filter at all — the actual leak behind "Physics/Chemistry
            # still appear when practicing"). Must use the same
            # Question.courses-blank-falls-back-to-Subject.courses OR
            # pattern _question_course_scoped() already uses — a bare
            # `courses__id=` matches almost nothing in real production
            # data (Question.courses is unpopulated on every question),
            # which silently zeroed out every Smart Practice tile's
            # results whenever the request carried the student's real
            # active course (i.e. every real browser request).
            course_id = data['course']
            qs = qs.filter(Q_(courses__id=course_id) | Q_(courses__isnull=True, subject__courses__id=course_id))
        if data.get('subject'):
            subject_val = data['subject']
            qs = qs.filter(subject_id=subject_val) if str(subject_val).isdigit() else qs.filter(subject__slug=subject_val)
        if data.get('chapter'):
            qs = qs.filter(chapter_id=data['chapter'])
        topics = data.get('topics') or ([data['topic']] if data.get('topic') else [])
        if topics:
            qs = qs.filter(topic_id__in=topics)
        difficulty = data.get('difficulty')
        if difficulty and difficulty != 'any':
            qs = qs.filter(Q_(instructor_difficulty=difficulty) | Q_(actual_difficulty=difficulty))

        statuses = data.get('status') or []
        if isinstance(statuses, str):
            statuses = [statuses]
        statuses = [s for s in statuses if s and s != 'any']
        if statuses:
            qs = qs.filter(id__in=_status_question_ids(user, statuses, qs))

        try:
            count = min(int(data.get('count') or 20), 100)
        except (TypeError, ValueError):
            count = 20

        qs = qs.distinct().order_by('?')[:count]
        questions = list(qs)
        bookmarked_ids = set(
            QuestionAttempt.objects.filter(user=user, question__in=questions, is_bookmarked=True)
            .values_list('question_id', flat=True)
        )
        for q in questions:
            q.is_bookmarked_by_user = q.id in bookmarked_ids

        return Response(QuestionSerializer(questions, many=True, context={'request': request}).data)


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


class ReferenceBookViewSet(viewsets.ModelViewSet):
    """Small admin-managed lookup table backing Question.reference_book —
    read-only (list/retrieve) for anyone, since it's just book names/authors
    with no course-scoping concern; writes are staff-only."""
    queryset = ReferenceBook.objects.all()
    serializer_class = ReferenceBookSerializer
    permission_classes = [IsStaffOrReadOnly]


class QuestionReportViewSet(viewsets.ModelViewSet):
    """Admin-only review queue for student-submitted QuestionReports
    (Admin/src/app/question-reports/). Never exposed to students — creation
    happens exclusively through QuestionViewSet.report(), not here."""
    queryset = QuestionReport.objects.select_related('question', 'reviewed_by').all()
    serializer_class = QuestionReportAdminSerializer
    permission_classes = [IsAdminUser]
    http_method_names = ['get', 'patch', 'head', 'options']

    def get_queryset(self):
        qs = super().get_queryset()
        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)
        return qs

    def perform_update(self, serializer):
        new_status = self.request.data.get('status')
        extra = {}
        if new_status in ('reviewed', 'dismissed'):
            extra = {'reviewed_by': self.request.user, 'reviewed_at': timezone.now()}
        serializer.save(**extra)
