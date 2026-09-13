from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Prefetch, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.functional import cached_property
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
from .random_sample import random_sample
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


class _BoundedListPagination(PageNumberPagination):
    """Shared safety cap for GET /subjects/, /chapters/, /topics/ — none of
    these have real pagination UI on any known caller (all read the
    response as a bare array), and catalog-wide counts of subjects/
    chapters/topics stay small even at the 100,000+ MCQ target, but an
    explicit DB-level LIMIT is still cheap insurance against an
    unfiltered request materializing every row."""
    page_size = 500
    max_page_size = 500

    def get_paginated_response(self, data):
        return Response(data)


class SubjectViewSet(viewsets.ModelViewSet):
    queryset = Subject.objects.all().prefetch_related('courses')
    permission_classes = [IsStaffOrReadOnlyExcludingTeacherWrites]
    lookup_field = 'slug'
    pagination_class = _BoundedListPagination

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
        qs = qs.distinct().annotate(
            # module_count/question_count/video_count were previously
            # obj.chapters.count()/obj.questions.count()/obj.videos.count()
            # SerializerMethodFields — one query each per subject per
            # field, per request (scalability audit). Combined in one
            # annotate() call with distinct=True on each, following the
            # same fan-out-safe pattern already proven for Test's
            # question_count/total_marks annotations.
            annotated_module_count=Count('chapters', distinct=True),
            annotated_question_count=Count('questions', distinct=True),
            annotated_video_count=Count('videos', distinct=True),
        ).order_by('order', 'name', 'id')
        # solved_modules/attempted_count are per-student, not per-catalog,
        # so batching them here (subject count stays small even at the
        # 100,000+ MCQ target) trades one extra grouped query for
        # eliminating what used to be two more queries PER SUBJECT ROW.
        user = self.request.user
        self._attempted_count_by_subject = {}
        self._solved_modules_by_subject = {}
        if user.is_authenticated:
            subject_ids = list(qs.values_list('id', flat=True))
            if subject_ids:
                rows = QuestionAttempt.objects.filter(
                    user=user, question__subject_id__in=subject_ids,
                ).values('question__subject_id', 'question__chapter_id')
                solved_chapters = {}
                for row in rows:
                    sid = row['question__subject_id']
                    self._attempted_count_by_subject[sid] = self._attempted_count_by_subject.get(sid, 0) + 1
                    solved_chapters.setdefault(sid, set()).add(row['question__chapter_id'])
                self._solved_modules_by_subject = {sid: len(chs) for sid, chs in solved_chapters.items()}
        return qs

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['attempted_count_by_subject'] = getattr(self, '_attempted_count_by_subject', None)
        context['solved_modules_by_subject'] = getattr(self, '_solved_modules_by_subject', None)
        return context


class ChapterViewSet(viewsets.ModelViewSet):
    queryset = Chapter.objects.all()
    serializer_class = ChapterSerializer
    permission_classes = [IsStaffOrReadOnlyExcludingTeacherWrites]
    pagination_class = _BoundedListPagination

    def get_queryset(self):
        qs = super().get_queryset()
        qs = _course_scoped(qs, self.request.user, courses_lookup='subject__courses')
        subject_slug = self.request.query_params.get('subject')
        if subject_slug:
            qs = qs.filter(subject__slug=subject_slug)
        # ChapterSerializer nests TopicSerializer(many=True) for `topics` —
        # previously unprefetched (N+1 on its own) and each nested Topic's
        # question_count/video_count were themselves per-topic queries (a
        # second, multiplicative N+1 layer). The Prefetch's own queryset
        # carries the same annotations TopicSerializer reads.
        topic_qs = Topic.objects.annotate(
            annotated_question_count=Count('questions', distinct=True),
            annotated_video_count=Count('videos', distinct=True),
        ).order_by('order', 'name', 'id')
        qs = qs.distinct().annotate(
            annotated_mcq_count=Count('questions', distinct=True),
            annotated_video_count=Count('videos', distinct=True),
        ).prefetch_related(Prefetch('topics', queryset=topic_qs)).order_by('order', 'name', 'id')
        user = self.request.user
        self._solved_count_by_chapter = {}
        if user.is_authenticated:
            chapter_ids = list(qs.values_list('id', flat=True))
            if chapter_ids:
                rows = (
                    QuestionAttempt.objects.filter(user=user, question__chapter_id__in=chapter_ids)
                    .values('question__chapter_id').annotate(n=Count('id'))
                )
                self._solved_count_by_chapter = {row['question__chapter_id']: row['n'] for row in rows}
        return qs

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['solved_count_by_chapter'] = getattr(self, '_solved_count_by_chapter', None)
        return context


class TopicViewSet(viewsets.ModelViewSet):
    queryset = Topic.objects.all()
    serializer_class = TopicSerializer
    permission_classes = [IsStaffOrReadOnlyExcludingTeacherWrites]
    pagination_class = _BoundedListPagination

    def get_queryset(self):
        qs = super().get_queryset()
        qs = _course_scoped(qs, self.request.user, courses_lookup='chapter__subject__courses')
        chapter_id = self.request.query_params.get('chapter')
        if chapter_id:
            qs = qs.filter(chapter_id=chapter_id)
        return qs.distinct().annotate(
            annotated_question_count=Count('questions', distinct=True),
            annotated_video_count=Count('videos', distinct=True),
        ).order_by('order', 'name', 'id')


# QBank 2.0 Phase 3 — documented, plain constants (not a new
# QuestionBankConfig field: these are simple, self-explanatory product
# thresholds, not a tunable mastery/difficulty scale like the existing
# config fields, so the smallest-architecture choice is a constant, not a
# new admin-editable row + migration + Admin UI surface).
#
# A question counts as a "Repeated Mistake" once it's been answered
# incorrectly at least this many times (QuestionAttempt.incorrect_count).
REPEATED_MISTAKE_MIN_COUNT = 2
# A "Recent Mistake" is a QuestionEvent(is_correct=False) within this many
# days — independent of the question's current (possibly since-corrected)
# mastery_status.
RECENT_MISTAKE_WINDOW_DAYS = 7


def _status_question_ids(user, statuses, base_qs):
    """Resolves the spec's independent status flags (New/Mastered/Weak/
    Need Practice/Incorrect/Bookmarked/Need Revision/Overdue/Due
    Today/Repeated Mistake/Recent Mistake) to a set of matching question
    ids, OR'd together — 'New + Incorrect' means either, not both.
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
    # QBank 2.0 Phase 3A: Due Today / Overdue split need_revision above
    # into two mutually-exclusive, calendar-date-based buckets — matching
    # the Revision Center's own "Due Today" vs "Overdue" summary cards.
    if 'overdue' in statuses:
        start_of_today = tz.localtime(tz.now()).replace(hour=0, minute=0, second=0, microsecond=0)
        ids |= set(attempt_qs.filter(revision_due_at__lt=start_of_today).values_list('question_id', flat=True))
    if 'due_today' in statuses:
        start_of_today = tz.localtime(tz.now()).replace(hour=0, minute=0, second=0, microsecond=0)
        start_of_tomorrow = start_of_today + tz.timedelta(days=1)
        ids |= set(
            attempt_qs.filter(revision_due_at__gte=start_of_today, revision_due_at__lt=start_of_tomorrow)
            .values_list('question_id', flat=True)
        )
    if 'repeated_mistake' in statuses:
        ids |= set(
            attempt_qs.filter(incorrect_count__gte=REPEATED_MISTAKE_MIN_COUNT).values_list('question_id', flat=True)
        )
    if 'recent_mistake' in statuses:
        cutoff = tz.now() - tz.timedelta(days=RECENT_MISTAKE_WINDOW_DAYS)
        ids |= set(
            QuestionEvent.objects.filter(user=user, is_correct=False, created_at__gte=cutoff)
            .values_list('question_id', flat=True)
        )
    return ids


def _revision_reason(question, now):
    """One short, transparent, plain-language sentence for why a question
    was selected by Smart Revision — same priority order the score below
    weighs, so the stated reason always matches what actually drove the
    ranking. Never a complicated score shown to the student."""
    mastery = getattr(question, 'mastery_status_for_user', None) or 'new'
    due_at = getattr(question, 'revision_due_at_for_user', None)
    incorrect = getattr(question, 'incorrect_count_for_user', None) or 0
    confidence = getattr(question, 'confidence_for_user', None)

    if due_at and due_at < now:
        days = max((now - due_at).days, 1)
        return f"Overdue by {days} day{'s' if days != 1 else ''}."
    if confidence == 'confident' and mastery in ('weak', 'need_practice', 'learning'):
        return 'Answered incorrectly despite high confidence.'
    if incorrect >= REPEATED_MISTAKE_MIN_COUNT:
        return f"You've gotten this wrong {incorrect} times."
    if mastery == 'weak':
        return 'One of your weaker questions.'
    if due_at and due_at <= now:
        return 'Due for review today.'
    return 'Recommended for revision.'


def _smart_revision_score(question, now):
    """Transparent, documented priority — factors and weights are
    deliberately simple (not a hidden ML score): overdue days (capped),
    mastery state, repeated-incorrect count (capped), and a confidence-
    trap boost. Every input is a real, already-computed QuestionAttempt
    field (see the annotations practice_session() adds above) — no new
    signal is invented. Internal only; students see _revision_reason()'s
    plain sentence instead of this number."""
    mastery = getattr(question, 'mastery_status_for_user', None) or 'new'
    due_at = getattr(question, 'revision_due_at_for_user', None)
    incorrect = getattr(question, 'incorrect_count_for_user', None) or 0
    confidence = getattr(question, 'confidence_for_user', None)

    score = 0
    if due_at and due_at < now:
        overdue_days = (now - due_at).days
        score += min(overdue_days, 14) * 10
    elif due_at and due_at <= now:
        score += 5  # due today, not yet overdue

    mastery_weight = {'weak': 50, 'need_practice': 30, 'learning': 15, 'mastered': 0}
    score += mastery_weight.get(mastery, 20)

    score += min(incorrect, 5) * 8

    if confidence == 'confident' and mastery in ('weak', 'need_practice', 'learning'):
        score += 15

    return score


def _rank_for_smart_revision(qs, count):
    """Smart Revision's selection: every candidate in `qs` (already
    course/eligibility-scoped and restricted to actually-attempted
    questions by the caller) is scored by _smart_revision_score and the
    top `count` are returned, each carrying a matching revision_reason_
    for_user. Evaluated in Python over this one student's own attempted-
    question pool (bounded — hundreds, not the platform's full catalog),
    not a platform-wide scan."""
    from django.utils import timezone as tz

    now = tz.now()
    candidates = list(qs)
    candidates.sort(key=lambda q: _smart_revision_score(q, now), reverse=True)
    selected = candidates[:count]
    for q in selected:
        q.revision_reason_for_user = _revision_reason(q, now)
    return selected


# Scalability audit Phase B: Q(text__icontains=search) forced a full table
# scan of academics_question on every Question Bank search (confirmed via
# EXPLAIN: type=ALL, ~29,500 rows examined at 30K questions) — the search
# spans `text` (the question body, by far the largest/most numerous field
# being scanned) plus public_id/subject/chapter/topic/tags, but only
# `text` is what actually drives the scan cost at scale (the others are
# short fields or joins to small catalog tables, ~20 subjects/chapters/
# topics — never the bottleneck).
#
# MySQL FULLTEXT (BOOLEAN MODE, trailing wildcard for prefix matching) is
# a real index lookup instead of a scan, but it is *word*-tokenized: it
# cannot match a term that only appears mid-word inside another word
# (searching "diac" would never match "cardiac" — LIKE '%diac%' would).
# There's no reliable way to know in advance whether a given search term
# is a genuine word/prefix (safe for FULLTEXT) or a word fragment (needs
# the old scan) — so instead of guessing from the term itself, this tries
# FULLTEXT first and checks the *result*: if it found at least one match,
# that's used (the fast path, and the overwhelmingly common case — most
# real searches for a whole word/prefix that exists in the question
# bank). If FULLTEXT found nothing, that's exactly the "maybe this term
# only exists as a mid-word substring, or is shorter than FULLTEXT's
# minimum indexed token length" case — falls back to the *exact* original
# full-scan query, so no result the old search would have found is ever
# lost, just occasionally not sped up. `tags` is excluded from FULLTEXT
# entirely (it's a JSONField storing a keyword list, not a text column —
# MySQL FULLTEXT indexes can't include JSON columns) and stays on
# icontains in both branches, completely unaffected either way.
#
# MySQL-only: SQLite (local dev/tests) has no FULLTEXT/MATCH...AGAINST
# support at all, so this always takes the original full-scan path there
# — same query, same results, just without the index. See this migration:
# academics/migrations/0024_question_text_fulltext_index.py.
def _apply_question_search(qs, search):
    from django.db.models import Q

    def _original_full_scan():
        return qs.filter(
            Q(public_id__icontains=search) | Q(text__icontains=search)
            | Q(subject__name__icontains=search) | Q(chapter__name__icontains=search)
            | Q(topic__name__icontains=search) | Q(tags__icontains=search)
        )

    from django.db import connection

    if connection.vendor != 'mysql':
        return _original_full_scan()

    import re

    # Strip MySQL boolean-mode operators (+ - < > ( ) ~ * " @) so a search
    # term containing them can't change query semantics (e.g. a leading
    # "-word" means "exclude" in boolean mode) or cause a syntax error —
    # tokenized the same way FULLTEXT itself would split on them anyway.
    safe_term = re.sub(r'[+\-<>()~*"@]+', ' ', search).strip()
    boolean_term = ' '.join(f'{word}*' for word in safe_term.split() if word)
    if not boolean_term:
        return _original_full_scan()

    # Materialized eagerly (a plain Python list of ids), not left as a
    # lazy queryset nested via id__in=<queryset> — Django re-aliases the
    # table when a queryset is embedded as a subquery (`academics_question`
    # becomes `U0` in the generated SQL), which breaks this raw MATCH()
    # clause's hardcoded table reference and MySQL rejects it outright
    # (errno 1210, "Incorrect arguments to MATCH") since MATCH() can't
    # correlate to an outer query's table. Running it as its own
    # standalone top-level query first sidesteps that entirely — confirmed
    # via a direct repro against staging before landing this fix.
    fulltext_ids = list(
        Question.objects.extra(
            where=['MATCH(academics_question.text) AGAINST (%s IN BOOLEAN MODE)'],
            params=[boolean_term],
        ).values_list('id', flat=True)
    )
    if not fulltext_ids:
        return _original_full_scan()

    return qs.filter(
        Q(id__in=fulltext_ids) | Q(public_id__icontains=search)
        | Q(subject__name__icontains=search) | Q(chapter__name__icontains=search)
        | Q(topic__name__icontains=search) | Q(tags__icontains=search)
    )


class _BrowsePagination(PageNumberPagination):
    page_size = 20
    max_page_size = 50
    page_size_query_param = 'page_size'


class _CheapDistinctCountPaginator(Paginator):
    """Scalability audit: DRF's paginator needs self.count (via num_pages)
    to validate the requested page even though _QuestionListPagination's
    response never surfaces it — and the normal Paginator.count just calls
    queryset.count(). For QuestionViewSet's queryset that means a wide,
    unrestricted DISTINCT: no .values()/.only() is applied, so MySQL has
    to materialize every matching row's full ~40-column set (including the
    large `text` field and the 4 correlated-subquery annotation columns)
    into a derived table, deduplicate on ALL of it, and only then count —
    confirmed by direct measurement at 2.7-4.7s per request at 100K
    questions, dominating this endpoint's latency far more than the actual
    500-row fetch or serialization (both of which stayed under 100ms).

    Every row is already unique by `pk` (Question's AutoField), so
    counting `.values('pk').distinct()` instead returns the exact same
    number — it does not change which questions match, their order, or
    anything about the actual page that gets fetched/serialized/returned
    (unaffected: filtering, search, course-scoping, permissions, response
    shape) — but it lets MySQL deduplicate on one indexed integer column
    instead of a full wide row, an index-covered operation. Confirmed via
    direct measurement: ~20-30ms at 100K questions, matching the cost of a
    plain COUNT(*) with no DISTINCT at all."""
    @cached_property
    def count(self):
        return self.object_list.values('pk').distinct().count()


class _QuestionListPagination(PageNumberPagination):
    """Caps GET /questions/ at a real DB-level LIMIT without changing its
    response shape — its 4 confirmed callers (QuestionSolver's chapter-
    solve session, the QBank bookmarks page, the Admin question-management
    table, Admin's QuestionPicker) all read the response as a bare array,
    exactly the reason `browse` above exists as a separate opt-in action
    rather than pagination going on globally. The two Admin surfaces
    (management table, QuestionPicker) are the genuine "browse the whole
    catalog" cases and are the best long-term fit for `browse`'s paginated
    envelope, but migrating them requires real Admin UI changes (pagination
    controls) that are out of scope for this pass per "do not change
    existing UI/UX unless required for scalability" — this 500-row cap
    already satisfies the actual requirement (no endpoint returns an
    unbounded dataset) for them today. The two student-facing callers (one
    chapter's questions, one student's own bookmarks) are inherently
    bounded by what they already filter to. This cap is the safety net for
    the remaining unbounded case — no filters at all applied."""
    page_size = 500
    max_page_size = 500
    django_paginator_class = _CheapDistinctCountPaginator

    def get_paginated_response(self, data):
        return Response(data)


class QuestionViewSet(viewsets.ModelViewSet):
    queryset = Question.objects.all().select_related('subject', 'chapter').prefetch_related('options')
    permission_classes = [IsStaffOrReadOnly]
    pagination_class = _QuestionListPagination

    def get_serializer_class(self):
        if self.request.user.is_authenticated and self.request.user.is_staff:
            return QuestionAdminSerializer
        return QuestionSerializer

    def get_queryset(self):
        # topic + each option's image_asset were previously unfetched
        # (only subject/chapter/options itself were) — one extra query per
        # row per relation, per field, on every question in the page
        # (scalability audit: up to ~122 queries for one 20-row public
        # page, ~182 for the admin one). image_asset is select_related
        # unconditionally because QuestionSerializer.get_image_data() reads
        # it too (public path, not admin-only, unlike the block below).
        # explanation_image_asset/reference_book/created_by ARE ADMIN-
        # serializer-only fields (QuestionSerializer never reads them) —
        # only select_related them for a staff request, per "only load
        # relationships required by the serializer/action".
        is_admin_view = self.request.user.is_authenticated and self.request.user.is_staff
        option_qs = Option.objects.select_related('image_asset')
        qs = Question.objects.select_related('subject', 'chapter', 'topic', 'image_asset').prefetch_related(
            Prefetch('options', queryset=option_qs),
        )
        if is_admin_view:
            qs = qs.select_related('explanation_image_asset', 'reference_book', 'created_by')
            # QuestionAdminSerializer also exposes the raw `courses` M2M
            # (QuestionSerializer does not) — without this, each row triggers
            # its own `courses_course` query when the field is serialized.
            qs = qs.prefetch_related('courses')
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
            # Scalability audit Fix 2: was qs.filter(courses__id=course) —
            # a JOIN to the same Question.courses M2M table as
            # question_course_scoped() below. This one value can only
            # ever match one M2M row per question (no fan-out on its own),
            # but converting it to EXISTS keeps every course-related
            # filter on this queryset off the JOIN path consistently, per
            # the approved fix scope.
            qs = qs.filter(Exists(Question.courses.through.objects.filter(question_id=OuterRef('pk'), course_id=course)))
        if teacher:
            qs = qs.filter(created_by_id=teacher)
        if search:
            qs = _apply_question_search(qs, search)
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
                # QBank 2.0 Phase 3: same Subquery mechanism, four more
                # already-existing QuestionAttempt fields the Revision
                # Center / Mistake Bank 2.0 need to read back.
                incorrect_count_for_user=Subquery(attempt_for_user.values('incorrect_count')[:1]),
                attempts_count_for_user=Subquery(attempt_for_user.values('attempts_count')[:1]),
                confidence_for_user=Subquery(attempt_for_user.values('confidence')[:1]),
                answered_at_for_user=Subquery(attempt_for_user.values('answered_at')[:1]),
            )
        # Question has no Meta.ordering — was never deterministic before
        # this (DRF's paginator would otherwise warn "may yield
        # inconsistent results", same as tests_app.TestViewSet had to be
        # fixed for). -id (newest first) matches the direction every
        # existing consumer already implicitly assumed with no order at all.
        #
        # Scalability audit Fix 2: .distinct() removed here. It used to be
        # required because course_course_scoped()'s and the ?course=
        # filter's M2M JOINs could fan out (one row per matching related
        # row); both are now EXISTS-based subqueries, which return one
        # boolean per outer row and never fan out. Every other filter
        # applied above is verified fan-out-safe on its own: search only
        # joins Question's single-valued subject/chapter/topic FKs (at
        # most one match per question); the bookmarked filter joins
        # QuestionAttempt, which has a unique_together(user, question)
        # constraint (at most one match per question per user); the status
        # filter narrows by a pre-deduplicated Python set of ids. With no
        # remaining fan-out source, .distinct() was a pure no-op paid on
        # every request — and, with no restricted .values(), an expensive
        # one: it forced MySQL to materialize every matching row's full
        # column set into a derived table just to deduplicate rows that
        # were never duplicated in the first place (confirmed via EXPLAIN
        # and direct timing — see the Fix 2 audit report).
        return qs.order_by('-id')

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
        # Phase 3 Free Starter: deliberately NOT self.get_object() — that
        # applies get_queryset()'s LISTING-scoped locked_subject_ids
        # exclusion, which is a coarse, subject-wide lock meant for catalog
        # browsing. This action already runs its own precise, per-question
        # entitlement check below (can_view_qbank, gated on option_id) —
        # applying the coarser listing lock on top would 404 a request this
        # action's own gate is fully equipped to answer properly (e.g. an
        # informative 402 instead of an opaque "not found"), and would
        # incorrectly block re-answering an already-legitimately-attempted
        # question once the student's free-starter quota is later
        # exhausted. Matches the existing get_object_or_404(Option, ...)
        # pattern a few lines below — same style, same reasoning.
        question = get_object_or_404(Question, pk=pk)
        serializer = AnswerSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        option_id = serializer.validated_data.get('option_id')
        time_taken_seconds = serializer.validated_data.get('time_taken_seconds')
        confidence = serializer.validated_data.get('confidence') or None

        selected_option = None
        is_correct = False
        attempt = None
        if option_id:
            # Phase 3 Free Starter (docs/FREE_STARTER_USAGE_RULES.md §2):
            # only gate/consume on a student's genuinely FIRST-EVER attempt
            # at THIS question — an already-attempted question (any prior
            # entitlement source) stays answerable even if the student's
            # entitlement has since lapsed, matching the same "don't re-gate
            # something already unlocked" rule applied to Mock/Daily/Grand
            # below. Free subjects are never gated at all (see
            # entitlements.services.can_view_qbank's own free-subject
            # short-circuit).
            # Staff excluded entirely (Step 31) — preserves the exact
            # pre-Phase-3 behavior of unrestricted staff QBank access (there
            # was no gate here for anyone before this phase; adding one for
            # staff now would both regress admin/QA workflows and risk
            # accidentally consuming a free-starter row for a non-student
            # account).
            already_attempted = QuestionAttempt.objects.filter(user=request.user, question=question).exists()
            if not already_attempted and not question.subject.is_free and not request.user.is_staff:
                from entitlements.provisioning import ensure_and_consume_free_starter
                from entitlements.services import SOURCE_FREE_STARTER, can_view_qbank

                decision = can_view_qbank(request.user, question.subject)
                if not decision.allowed:
                    return Response(
                        {
                            'detail': 'Free practice limit reached.', 'code': 'purchase_required',
                            'access_denied': {
                                'reason': 'free_limit_reached', 'source': 'free_starter', 'upgrade_available': True,
                            },
                        },
                        status=status.HTTP_402_PAYMENT_REQUIRED,
                    )
                if decision.source_type == SOURCE_FREE_STARTER:
                    ensure_and_consume_free_starter(request.user, 'qbank')

            selected_option = get_object_or_404(Option, pk=option_id, question=question)
            is_correct = selected_option.is_correct
            # bookmark is intentionally untouched here — it has its own dedicated
            # action below; folding it into this call previously reset a prior
            # bookmark to False on every plain answer (bool("False") is True, and
            # the serializer's bookmark default is always present in
            # validated_data even when the client never sent the key).
            attempt = record_question_result(
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

        # QBank 2.0 Phase 2D: recent_events backs "Your Question History" —
        # the append-only QuestionEvent log already records every past
        # attempt at this question by this user; this is the first time
        # it's read back rather than only written. Scoped to request.user
        # only, so it can never expose another student's activity. Kept to
        # the last 10 (oldest-noise trimmed) and only computed on an actual
        # answer, matching every other field in this response.
        recent_events = None
        if option_id:
            recent_events = [
                {'is_correct': e.is_correct, 'date': e.created_at.isoformat()}
                for e in QuestionEvent.objects.filter(user=request.user, question=question).order_by('-created_at')[:10]
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
            # QBank 2.0 Phase 2D — surfaces what record_question_result()
            # (above) already computed and saved onto QuestionAttempt;
            # previously discarded. No new mastery/revision algorithm.
            'mastery_status': attempt.mastery_status if attempt else None,
            'attempts_count': attempt.attempts_count if attempt else None,
            'correct_count': attempt.correct_count if attempt else None,
            'incorrect_count': attempt.incorrect_count if attempt else None,
            'revision_due_at': attempt.revision_due_at.isoformat() if attempt and attempt.revision_due_at else None,
            'recent_events': recent_events,
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

        # QBank 2.0 Phase 3A/3H: Revision Center summary numbers — every
        # one of these reuses attempt_qs/event_qs above (already course/
        # subject-scoped), just with the specific bucketing the Revision
        # Center needs. No new query pattern, no new mastery/revision
        # algorithm — see _status_question_ids() for the identical Due
        # Today/Overdue/Repeated/Recent definitions used by the list
        # endpoints, kept consistent with these counts on purpose.
        start_of_today = timezone.localtime(timezone.now()).replace(hour=0, minute=0, second=0, microsecond=0)
        start_of_tomorrow = start_of_today + timezone.timedelta(days=1)
        due_today = attempt_qs.filter(
            revision_due_at__gte=start_of_today, revision_due_at__lt=start_of_tomorrow,
        ).count()
        overdue = attempt_qs.filter(revision_due_at__lt=start_of_today).count()
        repeated_mistakes = attempt_qs.filter(incorrect_count__gte=REPEATED_MISTAKE_MIN_COUNT).count()
        recent_cutoff = timezone.now() - timezone.timedelta(days=RECENT_MISTAKE_WINDOW_DAYS)
        recent_mistakes = (
            event_qs.filter(is_correct=False, created_at__gte=recent_cutoff)
            .values('question_id').distinct().count()
        )

        # "Revision accuracy": accuracy across questions currently relevant
        # to revision (weak/learning/need_practice, or due/overdue) — a
        # real, computable metric from existing fields. Deliberately NOT
        # "accuracy of Smart Revision sessions specifically": QuestionEvent
        # doesn't distinguish a Smart Revision answer from any other QBank
        # answer (both are source='qbank'), and adding that distinction
        # would mean new event-level tracking — out of scope for Phase 3
        # per "do not create duplicate tracking".
        from django.db.models import Q as Q_revision
        revision_relevant = attempt_qs.filter(
            Q_revision(mastery_status__in=['weak', 'need_practice', 'learning'])
            | Q_revision(revision_due_at__lte=timezone.now())
        ).distinct()
        revision_relevant_count = revision_relevant.count()
        revision_accuracy = (
            round(revision_relevant.filter(last_result=True).count() / revision_relevant_count * 100, 2)
            if revision_relevant_count else None
        )

        # Last 7 days of QBank activity (source='qbank', same event_qs
        # scoping as study_seconds above) — Phase 3I "Revision Activity".
        # Real QuestionEvent rows only; a day with zero events is simply
        # absent from the list rather than a fabricated zero entry, so the
        # frontend can render "no activity" instead of misleadingly
        # implying every day was tracked.
        from django.db.models.functions import TruncDate
        daily_activity = list(
            event_qs.filter(created_at__gte=recent_cutoff)
            .annotate(day=TruncDate('created_at'))
            .values('day').annotate(count=Count('id')).order_by('-day')
        )

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
            'due_today': due_today,
            'overdue': overdue,
            'repeated_mistakes': repeated_mistakes,
            'recent_mistakes': recent_mistakes,
            'revision_accuracy': revision_accuracy,
            'daily_activity': [{'date': row['day'].isoformat(), 'count': row['count']} for row in daily_activity],
            'topics_practiced': topics_practiced,
            'study_seconds': study_seconds,
        })

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def progress(self, request):
        """QBank 2.0 Phase 4 — Progress & Learning Intelligence.

        Deliberately QBank-practice-only (QuestionAttempt/QuestionEvent),
        using QuestionAttempt.mastery_status throughout — NOT
        tests_app.performance's combined Test+QBank subject_breakdown/
        topic_mastery (a different scope, a different accuracy-bucket
        mastery scale, already serving the existing /performance page).
        This is a new, complementary lens ("how am I doing in QBank
        practice specifically"), not a duplicate of that page — nothing
        here is recomputed from data that page already owns, and nothing
        here creates a third mastery definition (the documented
        QuestionBankConfig-vs-performance.py discrepancy is left exactly
        as it is)."""
        from django.db.models import Q
        from django.db.models.functions import TruncDate

        user = request.user
        course = request.query_params.get('course')
        locked = _locked_subject_ids(user)

        attempt_qs = QuestionAttempt.objects.filter(user=user, attempts_count__gt=0)
        if locked:
            attempt_qs = attempt_qs.exclude(question__subject_id__in=locked)
        if course:
            attempt_qs = attempt_qs.filter(
                Q(question__courses__id=course) | Q(question__courses__isnull=True, question__subject__courses__id=course)
            )

        # By-subject: attempted/accuracy/mastered/weak/revision-due — all
        # QBank-only, one GROUP BY query (not one query per subject).
        by_subject_rows = (
            attempt_qs.values('question__subject_id', 'question__subject__name', 'question__subject__slug')
            .annotate(
                attempted=Count('id'),
                correct=Count('id', filter=Q(last_result=True)),
                mastered=Count('id', filter=Q(mastery_status='mastered')),
                weak=Count('id', filter=Q(mastery_status='weak')),
                revision_due=Count('id', filter=Q(revision_due_at__lte=timezone.now())),
            )
        )

        # total_questions per subject — same course/lock-scoped Question
        # queryset dashboard() already builds, just grouped by subject
        # instead of summed to one total.
        question_qs = _question_course_scoped(Question.objects.all(), user)
        if locked:
            question_qs = question_qs.exclude(subject_id__in=locked)
        if course:
            question_qs = question_qs.filter(
                Q(courses__id=course) | Q(courses__isnull=True, subject__courses__id=course)
            )
        total_by_subject = dict(
            question_qs.values('subject_id').annotate(total=Count('id', distinct=True)).values_list('subject_id', 'total')
        )

        by_subject = []
        for row in by_subject_rows:
            sid = row['question__subject_id']
            attempted = row['attempted']
            by_subject.append({
                'subject_id': sid,
                'subject_name': row['question__subject__name'],
                'subject_slug': row['question__subject__slug'],
                'attempted': attempted,
                'accuracy': round(row['correct'] / attempted * 100, 2) if attempted else 0.0,
                'mastered': row['mastered'],
                'weak': row['weak'],
                'revision_due': row['revision_due'],
                'total_questions': total_by_subject.get(sid, 0),
            })
        by_subject.sort(key=lambda r: r['accuracy'])

        # Mastery distribution — the 5th bucket (new/unattempted) has no
        # mastery_status to show in a bar built from attempt_qs, so this
        # is deliberately the 4-way split *within* attempted questions
        # only, not dashboard()'s "new = total - attempted" framing.
        mastery_distribution = {
            'learning': attempt_qs.filter(mastery_status='learning').count(),
            'need_practice': attempt_qs.filter(mastery_status='need_practice').count(),
            'weak': attempt_qs.filter(mastery_status='weak').count(),
            'mastered': attempt_qs.filter(mastery_status='mastered').count(),
        }

        # Weakest/strongest topics — gated by the same existing minimum-
        # sample threshold /questions/{id}/answer/ already uses before
        # showing any peer-comparison signal (QuestionBankConfig.
        # min_attempts_for_option_stats) — reused, not a new invented
        # threshold, per the explicit Phase 4 instruction. `attempted`
        # here counts DISTINCT questions tried in the topic (Count('id'),
        # matching by_subject's own definition above), not raw attempt-
        # events on however few questions — re-answering one question
        # five times isn't five independent data points about the topic.
        min_attempts = QuestionBankConfig.load().min_attempts_for_option_stats
        topic_rows = (
            attempt_qs.exclude(question__topic__isnull=True)
            .values('question__topic_id', 'question__topic__name', 'question__subject__name')
            .annotate(attempted=Count('id'), correct=Count('id', filter=Q(last_result=True)))
            .filter(attempted__gte=min_attempts)
        )
        topics = [
            {
                'topic_id': r['question__topic_id'],
                'topic_name': r['question__topic__name'],
                'subject_name': r['question__subject__name'],
                'attempted': r['attempted'],
                'accuracy': round(r['correct'] / r['attempted'] * 100, 2),
            }
            for r in topic_rows
        ]
        weakest_topics = sorted(topics, key=lambda t: t['accuracy'])[:5]
        strongest_topics = sorted(topics, key=lambda t: -t['accuracy'])[:5]

        # Accuracy trend — QuestionEvent (source='qbank'), day-bucketed,
        # last 90 days. Real timestamped events only; a day with zero
        # activity is simply absent from the list, never a fabricated 0%.
        cutoff = timezone.now() - timezone.timedelta(days=90)
        event_qs = QuestionEvent.objects.filter(user=user, source='qbank', created_at__gte=cutoff)
        if locked:
            event_qs = event_qs.exclude(question__subject_id__in=locked)
        if course:
            event_qs = event_qs.filter(
                Q(question__courses__id=course) | Q(question__courses__isnull=True, question__subject__courses__id=course)
            )
        trend_rows = (
            event_qs.annotate(day=TruncDate('created_at'))
            .values('day').annotate(attempted=Count('id'), correct=Count('id', filter=Q(is_correct=True)))
            .order_by('day')
        )
        accuracy_trend = [
            {
                'date': r['day'].isoformat(),
                'attempted': r['attempted'],
                'accuracy': round(r['correct'] / r['attempted'] * 100, 2) if r['attempted'] else 0.0,
            }
            for r in trend_rows
        ]

        return Response({
            'by_subject': by_subject,
            'mastery_distribution': mastery_distribution,
            'weakest_topics': weakest_topics,
            'strongest_topics': strongest_topics,
            'accuracy_trend': accuracy_trend,
        })

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def mistakes(self, request):
        """Mistake Bank: subject-wise counts of currently-wrong questions,
        plus a filtered/ordered list. scope=frequent orders by how many
        times this question has been gotten wrong; scope=recent uses the
        QuestionEvent log (QuestionAttempt.answered_at is set only once, on
        first attempt, so it can't answer "most recently wrong").

        QBank 2.0 Phase 3E: previously had no course scoping at all (the
        documented inconsistency vs. Bookmarks, which already filters by
        ?course=). Two fixes, both mirroring patterns already used
        elsewhere in this file: an optional ?course= narrows to one active
        course (same Q_course pattern as dashboard()/practice_session()),
        and — because this action returns full question CONTENT for
        display/practice, unlike dashboard()'s counts-only response — a
        question_course_scoped() filter is now applied unconditionally, so
        a question a student can no longer access (lapsed course/subject
        access) never appears here merely because a historical
        QuestionAttempt row exists for it."""
        from django.db.models import Q as Q_mistakes

        user = request.user
        locked = _locked_subject_ids(user)
        course = request.query_params.get('course')

        base = QuestionAttempt.objects.filter(user=user, last_result=False)
        base = base.filter(question__in=_question_course_scoped(Question.objects.all(), user))
        if locked:
            base = base.exclude(question__subject_id__in=locked)
        if course:
            base = base.filter(
                Q_mistakes(question__courses__id=course)
                | Q_mistakes(question__courses__isnull=True, question__subject__courses__id=course)
            )

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
        # QBank 2.0 Phase 3D: attach the same *_for_user fields
        # get_queryset()/practice_session() annotate via Subquery — here
        # the QuestionAttempt row is already loaded (no extra query at
        # all), so this is a plain attribute assignment, not a second
        # mastery/history mechanism. Fixes the pre-existing bug where this
        # page's mastery badge never rendered (mastery_status_for_user was
        # never set, so QuestionSerializer always fell back to 'new').
        by_qid_attempt = {a.question_id: a for a in attempts}
        for q in questions:
            attempt = by_qid_attempt.get(q.id)
            q.is_bookmarked_by_user = attempt.is_bookmarked if attempt else False
            q.mastery_status_for_user = attempt.mastery_status if attempt else None
            q.incorrect_count_for_user = attempt.incorrect_count if attempt else None
            q.attempts_count_for_user = attempt.attempts_count if attempt else None
            q.confidence_for_user = attempt.confidence if attempt else None
            q.answered_at_for_user = attempt.answered_at if attempt else None
            q.revision_due_at_for_user = attempt.revision_due_at if attempt else None

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
        from django.db.models import Subquery

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

        # QBank 2.0 Phase 2D bug fix: get_queryset() (the /questions/{id}/
        # and /questions/?chapter= entry points) annotates every question
        # with this user's own mastery_status/last_result/revision_due_at
        # so QuestionSerializer can report the real values instead of its
        # 'new'/False fallback — this action never did, so a Practice
        # Session (Smart Practice, Quick Practice — the majority of real
        # QuestionSolver traffic) silently reported every question as
        # freshly 'new' and never due for revision, regardless of the
        # student's actual history. Same exact annotation pattern, applied
        # here for the first time; is_bookmarked_by_user is left alone
        # below, already handled separately and correctly.
        attempt_for_user = QuestionAttempt.objects.filter(user=user, question=OuterRef('pk'))
        qs = qs.annotate(
            mastery_status_for_user=Subquery(attempt_for_user.values('mastery_status')[:1]),
            last_result_for_user=Subquery(attempt_for_user.values('last_result')[:1]),
            revision_due_at_for_user=Subquery(attempt_for_user.values('revision_due_at')[:1]),
            # QBank 2.0 Phase 3: same fields added to get_queryset()'s own
            # annotation block — see that comment. Needed here so
            # smart_revision's ranking below (and the Revision Center's
            # transparent "why this question" text) can read them.
            incorrect_count_for_user=Subquery(attempt_for_user.values('incorrect_count')[:1]),
            confidence_for_user=Subquery(attempt_for_user.values('confidence')[:1]),
        )

        # QBank 2.0 Phase 3B — Smart Revision: opt-in only (every existing
        # caller — Quick Practice, Smart Practice tiles, the manual
        # builder — omits this flag and gets byte-for-byte the same
        # random_sample() behavior as before). A revision session only
        # makes sense over questions this student has actually attempted
        # (a never-seen question has no mastery/revision signal to act
        # on), then ranked by a transparent, documented priority — never
        # a second mastery/spaced-repetition algorithm, just an ordering
        # over the same QuestionAttempt fields already computed elsewhere.
        if data.get('smart_revision'):
            qs = qs.filter(mastery_status_for_user__isnull=False)
            questions = _rank_for_smart_revision(qs.distinct(), count)
        else:
            # random_sample() replaces `.order_by('?')[:count]` — see
            # academics/random_sample.py for why ORDER BY RAND() doesn't scale.
            questions = random_sample(qs.distinct(), count)

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
