from django.db.models import Prefetch
from django.utils import timezone
from rest_framework import serializers

from academics.models import Option, Question
from academics.serializers import OptionAdminSerializer, OptionSerializer, QuestionResultSerializer

from .lifecycle import effective_attempt_end
from .models import AttemptQuestionSnapshot, Answer, ExamSession, ExamTemplate, SavedExamView, Test, TestAttempt, TestQuestion
from .policy import POLICY_CONTROLLED_FIELDS, get_exam_type_defaults


def _staff_name(user):
    if not user:
        return ''
    return user.first_name or user.email


class TestListSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source='subject.name', read_only=True)
    question_count = serializers.SerializerMethodField()
    total_marks = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    best_score = serializers.SerializerMethodField()
    courses_detail = serializers.SerializerMethodField()
    card_status = serializers.SerializerMethodField()
    access = serializers.SerializerMethodField()
    attempts_used = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    in_progress_answered_count = serializers.SerializerMethodField()
    latest_attempt_id = serializers.SerializerMethodField()

    exam_template_id = serializers.IntegerField(source='exam_template.id', read_only=True, default=None)
    exam_code = serializers.CharField(source='exam_template.exam_code', read_only=True, default=None)

    class Meta:
        model = Test
        fields = [
            'id', 'title', 'description', 'difficulty', 'exam_type', 'subject', 'subject_name', 'duration_minutes',
            'question_count', 'total_marks', 'is_pro', 'is_new', 'price', 'max_attempts',
            'academic_year', 'university', 'scheduled_start', 'scheduled_end', 'status', 'best_score',
            'courses_detail', 'card_status', 'attempts_used', 'created_by_name', 'created_at',
            'is_draft', 'exam_template_id', 'exam_code', 'version_number', 'in_progress_answered_count',
            'latest_attempt_id', 'solutions_visibility', 'solutions_released_at',
            'access',
        ]

    def get_created_by_name(self, obj):
        if not obj.created_by_id:
            return ''
        return obj.created_by.first_name or obj.created_by.email

    def get_question_count(self, obj):
        # annotated_question_count (TestViewSet.get_queryset) replaces a
        # live COUNT query per Test with one DB-side aggregate computed in
        # the same query as the row fetch. Can't just annotate a queryset
        # with the name `question_count` directly — Test.question_count is
        # already a model @property, and Django raises trying to set an
        # annotated value over an existing property descriptor — hence the
        # differently-named annotation plus this explicit field/fallback.
        annotated = getattr(obj, 'annotated_question_count', None)
        return annotated if annotated is not None else obj.question_count

    def get_total_marks(self, obj):
        annotated = getattr(obj, 'annotated_total_marks', None)
        if annotated is not None:
            return float(annotated)
        return float(obj.total_marks)

    def get_status(self, obj):
        now = timezone.now()
        if obj.scheduled_start and obj.scheduled_start > now:
            return 'upcoming'
        if obj.scheduled_end and obj.scheduled_end < now:
            return 'ended'
        return 'live'

    def _user_attempts(self, obj):
        # TestViewSet.get_queryset() prefetches this user's attempts across
        # every Test in the page in one query — reuse that in-memory list
        # instead of the 3 separate live per-row queries
        # (best-score/in-progress/attempts-used) this replaced. Falls back
        # to a real query so this serializer still works correctly (just
        # not as cheaply) if ever used without that prefetch.
        if hasattr(obj, '_prefetched_user_attempts'):
            return obj._prefetched_user_attempts
        user = self.context.get('request').user if self.context.get('request') else None
        if not user or not user.is_authenticated:
            return []
        return list(obj.attempts.filter(user=user))

    def _best_attempt(self, obj):
        # Cached per-instance — get_best_score() and get_latest_attempt_id()
        # both need this same row (DRF calls SerializerMethodFields
        # independently, so without this every card would run it twice).
        cache_attr = '_best_attempt_cache'
        if not hasattr(obj, cache_attr):
            # sorted() is stable, so ties keep _user_attempts' own order
            # (most-recent-first, the model's default ordering) — a
            # deterministic tiebreak, unlike the plain .order_by('-score')
            # this replaces, which had no secondary sort key at all.
            submitted = sorted(
                (a for a in self._user_attempts(obj) if a.status == 'submitted'),
                key=lambda a: a.score, reverse=True,
            )
            setattr(obj, cache_attr, submitted[0] if submitted else None)
        return getattr(obj, cache_attr)

    def get_best_score(self, obj):
        best = self._best_attempt(obj)
        return float(best.score) if best else None

    def get_latest_attempt_id(self, obj):
        # The best-scoring submitted attempt's id — "Review Test" links
        # straight to its /tests/result/{id} page instead of the generic
        # test-detail page, which has no per-question review of its own.
        best = self._best_attempt(obj)
        return best.id if best else None

    def get_courses_detail(self, obj):
        return [{'id': c.id, 'name': c.name} for c in obj.courses.all()]

    def get_attempts_used(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return 0
        return len(self._user_attempts(obj))

    def _in_progress_attempt(self, obj):
        # Cached on the instance per request — get_card_status() and
        # get_in_progress_answered_count() both need this and DRF calls
        # SerializerMethodFields independently, so without this every row
        # would run the same lookup twice.
        cache_attr = '_in_progress_attempt_cache'
        if not hasattr(obj, cache_attr):
            in_progress = sorted(
                (a for a in self._user_attempts(obj) if a.status == 'in_progress'),
                key=lambda a: a.start_time, reverse=True,
            )
            setattr(obj, cache_attr, in_progress[0] if in_progress else None)
        return getattr(obj, cache_attr)

    def get_in_progress_answered_count(self, obj):
        attempt = self._in_progress_attempt(obj)
        if not attempt:
            return None
        # answered_count is annotated on the prefetched queryset — falls
        # back to a live count for the no-prefetch case _user_attempts
        # itself already falls back to.
        annotated = getattr(attempt, 'answered_count', None)
        return annotated if annotated is not None else attempt.answers.count()

    def get_access(self, obj):
        """Phase 10 — the server-authoritative card contract (state,
        can_start/continue/review, reason_code, upgrade_available, source).
        See tests_app/card_access.py.

        The snapshot is built once per request and cached on the serializer
        context, so a 20-card page costs a fixed handful of entitlement
        queries rather than 20 × that. `_user_attempts` is already
        prefetched for the whole page by TestViewSet.get_queryset()."""
        from .card_access import StudentEntitlementSnapshot, resolve_card_access
        from .preview import resolve_preview_user

        request = self.context.get('request')
        user = request.user if request else None
        # Phase 10 plan bullet 3 — admin "preview as student": resolve the
        # card as that student, read-only (read_only=True suppresses the
        # lazy Free Starter provisioning a normal check would perform, so
        # previewing cannot write to their account). None for everyone else.
        preview_user = resolve_preview_user(request)
        if preview_user is not None:
            user = preview_user
        snapshot = self.context.get('_entitlement_snapshot')
        if snapshot is None or snapshot.user is not user:
            snapshot = StudentEntitlementSnapshot(user, read_only=preview_user is not None)
            # self.context is shared across every item in a many=True
            # render, which is exactly the scope this should live in.
            self.context['_entitlement_snapshot'] = snapshot
        return resolve_card_access(obj, snapshot, self._user_attempts(obj))

    def get_card_status(self, obj):
        """Available / Upcoming / Completed / Missed / In Progress — for the
        dashboard exam card. In Progress (a real TestAttempt exists but
        hasn't been submitted yet) previously fell through to 'available',
        which showed 'Start Test' instead of 'Continue Test' and lost the
        distinction the new status tabs need.

        Phase 10 note: kept for backward compatibility with existing
        clients, but `access.state` (above) is now the authoritative field
        — this one is derived from the legacy Test.scheduled_start/end
        fields rather than the ExamSession that actually schedules a Daily/
        Grand exam (Phase 6), and knows nothing about entitlement. New UI
        should read `access`."""
        now = timezone.now()
        has_attempted = self.get_best_score(obj) is not None
        if has_attempted:
            return 'completed'
        if self._in_progress_attempt(obj):
            return 'in_progress'
        if obj.scheduled_start and obj.scheduled_start > now:
            return 'upcoming'
        if obj.scheduled_end and obj.scheduled_end < now:
            return 'missed'
        return 'available'


class QuestionForAttemptSerializer(serializers.ModelSerializer):
    from academics.serializers import OptionSerializer as _OptionSerializer
    options = OptionSerializer(many=True, read_only=True)
    subject_name = serializers.CharField(source='subject.name', read_only=True)
    is_bookmarked = serializers.SerializerMethodField()

    class Meta:
        from academics.models import Question
        model = Question
        fields = ['id', 'public_id', 'text', 'image', 'latex', 'marks', 'negative_marks', 'subject_name', 'options', 'is_bookmarked']

    def get_is_bookmarked(self, obj):
        # Same Question/QuestionAttempt.is_bookmarked a QBank bookmark sets —
        # bulk-fetched once by TestAttemptSerializer.get_questions() into
        # context['bookmarked_question_ids'], not annotated here (this
        # queryset comes from obj.test.questions.all(), not
        # QuestionViewSet.get_queryset(), so no per-request annotation to
        # rely on) — avoids an N+1 bookmark lookup per question.
        return obj.id in self.context.get('bookmarked_question_ids', set())


class TestDetailSerializer(TestListSerializer):
    has_access = serializers.SerializerMethodField()
    requires_password = serializers.SerializerMethodField()
    # Phase 10: the authoritative preview flag, straight from
    # billing.access.is_preview_only — the student detail page used to
    # re-derive this client-side as
    # `is_pro && !has_access && free_preview_questions > 0 && type !== grand`,
    # a fourth place answering a question the backend already answers.
    preview_only = serializers.SerializerMethodField()
    # GT3-2: the derived (never stored) MISSED/UPCOMING/LIVE/etc. state —
    # only ever meaningful for a Grand Test; null for every other exam
    # type, so a Frontend consuming this never has to special-case
    # "field present but meaningless." See
    # tests_app.lifecycle.grand_test_participation_status's own docstring
    # for exactly what each value means and where it comes from (a real
    # ExamSession if one exists, else Test.scheduled_start/end).
    grand_test_status = serializers.SerializerMethodField()
    grand_test_schedule = serializers.SerializerMethodField()

    class Meta(TestListSerializer.Meta):
        fields = TestListSerializer.Meta.fields + [
            'shuffle_questions', 'shuffle_options', 'negative_marking', 'max_attempts',
            'free_preview_questions', 'has_access', 'requires_password', 'preview_only',
            'grand_test_status', 'grand_test_schedule',
        ]

    def get_grand_test_status(self, obj):
        if obj.exam_type != 'grand':
            return None
        request = self.context.get('request')
        user = request.user if request else None
        if not user or not user.is_authenticated:
            return None
        from .lifecycle import grand_test_participation_status

        return grand_test_participation_status(obj, user)

    def get_grand_test_schedule(self, obj):
        """The actually-authoritative start/end for this Grand Test's
        schedule — a real ExamSession's own dates when one exists (which
        may differ from Test.scheduled_start/end: that pair is only ever
        BACKFILLED once, at first reschedule, and a session can be edited
        independently afterward), else Test.scheduled_start/end directly.
        Null (both keys) for an unscheduled Grand Test or any other exam
        type — nothing here changes existing behavior for Daily/Mock/PYQ."""
        if obj.exam_type != 'grand':
            return None
        from .lifecycle import resolve_test_schedule_session

        session = resolve_test_schedule_session(obj)
        if session is not None:
            return {'start': session.start_datetime, 'end': session.end_datetime}
        if obj.scheduled_start and obj.scheduled_end:
            return {'start': obj.scheduled_start, 'end': obj.scheduled_end}
        return None

    def get_preview_only(self, obj):
        from billing.access import is_preview_only

        request = self.context.get('request')
        return is_preview_only(request.user if request else None, obj)

    def get_has_access(self, obj):
        """Kept for backward compatibility with existing clients. Note it
        answers a narrower question than `access` (above) — it knows
        nothing about Free Starter, assignment, attempt limits or session
        windows, so a student who can genuinely start a test can still see
        `has_access: false` here. New UI should read `access`."""
        from billing.access import get_grand_test_access, has_daily_test_access, has_mock_test_access

        if not obj.is_pro:
            return True
        request = self.context.get('request')
        user = request.user if request else None
        if obj.exam_type == 'grand':
            return bool(get_grand_test_access(user, obj))
        if obj.exam_type in ('mock', 'qbank'):
            return has_mock_test_access(user, obj)
        if obj.exam_type == 'daily':
            return has_daily_test_access(user, obj)
        return True

    def get_requires_password(self, obj):
        """GT3-3: an already-entitled Grand Test student no longer needs a
        password at all (see tests_app.views._start_attempt's matching
        change) — the Frontend reads this field directly to decide
        whether to show a password box before Start (Frontend/src/app/
        tests/[id]/page.js), so this is the one place that decision
        actually needs to change; the answer/start endpoints themselves
        already stopped requiring it. A not-yet-entitled student on a
        pro Grand Test is untouched — still True, since the free-starter
        path's own Test.access_password gate (a separate mechanism, not
        touched by GT3-3) may still apply to them."""
        if obj.exam_type == 'grand' and obj.is_pro:
            from billing.access import get_grand_test_access

            request = self.context.get('request')
            user = request.user if request else None
            if get_grand_test_access(user, obj):
                return False
            return True
        return bool(obj.access_password)


class TestAdminSerializer(serializers.ModelSerializer):
    question_ids = serializers.PrimaryKeyRelatedField(
        queryset=Question.objects.all(), many=True, write_only=True, required=False, source='selected_questions',
    )
    questions = serializers.SerializerMethodField()
    question_count = serializers.IntegerField(read_only=True)
    total_marks = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    exam_code = serializers.CharField(source='exam_template.exam_code', read_only=True, default=None)
    # Phase 7 — read-only, written only by TestViewSet.release_solutions.
    solutions_released_by_name = serializers.SerializerMethodField()
    # GT3-7 §7-8 — advisory only, never a write-blocking validation (per
    # this phase's own instruction: historical attempt snapshots — see
    # AttemptQuestion.marks_snapshot/AttemptQuestionSnapshot — are what
    # ALREADY structurally protects a finalized attempt's score/review
    # from a later edit here, regardless of this field). Lets the Admin
    # UI show the exact warning GT3-7 §8 specifies before an admin changes
    # a dangerous field on an exam that already has real attempts.
    attempt_count = serializers.SerializerMethodField()
    dangerous_edit_warning = serializers.SerializerMethodField()

    class Meta:
        model = Test
        fields = [
            'id', 'title', 'description', 'difficulty', 'exam_type', 'subject', 'courses', 'assigned_students',
            'assigned_batches', 'needs_course_review', 'duration_minutes', 'questions_per_page',
            'negative_marking', 'shuffle_questions', 'shuffle_options', 'max_attempts', 'solutions_visibility',
            'is_pro', 'is_new', 'price', 'access_password', 'free_preview_questions', 'academic_year', 'university',
            'scheduled_start', 'scheduled_end', 'is_draft', 'question_ids', 'questions', 'question_count', 'total_marks',
            'created_by_name', 'exam_template', 'exam_code', 'version_number',
            'solutions_released_at', 'solutions_released_by_name', 'review_duration_days',
            'attempt_count', 'dangerous_edit_warning',
        ]
        read_only_fields = ['exam_template', 'version_number', 'solutions_released_at']

    def get_solutions_released_by_name(self, obj):
        return _staff_name(obj.solutions_released_by)

    def get_attempt_count(self, obj):
        return obj.attempts.count()

    def get_dangerous_edit_warning(self, obj):
        if not obj.pk or not obj.attempts.exists():
            return None
        return (
            f'This exam already has {obj.attempts.count()} student attempt(s). Changing questions, marks, '
            'negative marks, or the schedule may affect examination integrity for students who already '
            'attempted it — their own scored results stay based on what the exam looked like when they took '
            'it (see the exam’s historical review), but new attempts will use whatever you save now.'
        )

    def get_total_marks(self, obj):
        return float(obj.total_marks)

    def get_questions(self, obj):
        from academics.serializers import QuestionSerializer
        return QuestionSerializer(obj.questions.all().order_by('testquestion__order'), many=True).data

    def get_created_by_name(self, obj):
        if not obj.created_by_id:
            return ''
        return obj.created_by.first_name or obj.created_by.email

    def create(self, validated_data):
        questions = validated_data.pop('selected_questions', [])
        courses = validated_data.pop('courses', [])
        assigned_students = validated_data.pop('assigned_students', [])
        assigned_batches = validated_data.pop('assigned_batches', [])
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            validated_data['created_by'] = request.user
        # Phase 5: backend-authoritative exam-type policy defaults. Both
        # known frontends (Create Exam Wizard, Import & Create Test) always
        # send every field explicitly today, so this is dormant for them —
        # an admin's explicit per-exam value always wins, exactly as before.
        # This exists so the backend, not just frontend fetch logic, is the
        # real source of truth for any caller that sends a partial payload.
        # update() never applies this — an existing Test is never touched
        # by a later policy change.
        exam_type = validated_data.get('exam_type', Test._meta.get_field('exam_type').get_default())
        defaults = get_exam_type_defaults(exam_type)
        for field in POLICY_CONTROLLED_FIELDS:
            if field not in validated_data:
                validated_data[field] = defaults[field]
        # GT3-4: a brand-new Grand Test defaults to a 30-day detailed-review
        # window when the admin doesn't set one explicitly — the approved
        # product default. Deliberately NOT added to POLICY_CONTROLLED_FIELDS/
        # ExamTypePolicy (that per-category-configurable machinery is a
        # bigger surface than "the minimum Admin setting necessary" this
        # phase asks for); the field is still fully admin-editable per-Test
        # via this same serializer. Applies only at creation, only for a
        # NEW Grand Test — never retroactively touches an existing row
        # (review_duration_days stays null/permanent for every Test that
        # existed before this phase, exactly like every other field this
        # create() method defaults only when the caller omits it).
        if exam_type == 'grand' and 'review_duration_days' not in validated_data:
            validated_data['review_duration_days'] = 30
        test = Test.objects.create(**validated_data)
        if courses:
            test.courses.set(courses)
        if assigned_students:
            test.assigned_students.set(assigned_students)
        if assigned_batches:
            test.assigned_batches.set(assigned_batches)
        for i, q in enumerate(questions):
            TestQuestion.objects.create(test=test, question=q, order=i)
        return test

    def update(self, instance, validated_data):
        questions = validated_data.pop('selected_questions', None)
        courses = validated_data.pop('courses', None)
        assigned_students = validated_data.pop('assigned_students', None)
        assigned_batches = validated_data.pop('assigned_batches', None)
        # needs_course_review is a one-time legacy-migration flag ("visible
        # to everyone until an admin assigns real courses" — see the model's
        # help_text) that the exam-management UI never surfaces, so it was
        # never getting cleared once an admin actually assigned courses
        # here — the real access-control bug this guards against. Auto-clear
        # it the moment this save gives the test a real assignment, unless
        # the caller explicitly set needs_course_review itself this request.
        if 'needs_course_review' not in validated_data and (courses or assigned_students or assigned_batches):
            instance.needs_course_review = False
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if courses is not None:
            instance.courses.set(courses)
        if assigned_students is not None:
            instance.assigned_students.set(assigned_students)
        if assigned_batches is not None:
            instance.assigned_batches.set(assigned_batches)
        if questions is not None:
            TestQuestion.objects.filter(test=instance).delete()
            for i, q in enumerate(questions):
                TestQuestion.objects.create(test=instance, question=q, order=i)
        return instance


class ExamSessionSerializer(serializers.ModelSerializer):
    exam_template_title = serializers.CharField(source='exam_template.title', read_only=True)
    exam_code = serializers.CharField(source='exam_template.exam_code', read_only=True)
    question_count = serializers.IntegerField(source='exam_version.question_count', read_only=True)
    total_marks = serializers.SerializerMethodField()
    duration_minutes = serializers.IntegerField(source='exam_version.duration_minutes', read_only=True)
    negative_marking = serializers.BooleanField(source='exam_version.negative_marking', read_only=True)
    participant_count = serializers.IntegerField(read_only=True)
    created_by_name = serializers.SerializerMethodField()
    password = serializers.CharField(required=False, allow_blank=True, write_only=True)
    # Phase 6 — additive: `status` is the last value a WRITE (the start
    # action, or an explicit admin edit) actually persisted, which can be
    # stale on a plain list/retrieve if nothing has written to this
    # session since its time window changed (audit finding: "session
    # state refresh was partly lazy"). `effective_status` is always
    # correct, computed live, and never writes — see
    # tests_app.lifecycle.compute_effective_session_status().
    effective_status = serializers.SerializerMethodField()
    # 'not_started' | 'in_progress' | 'submitted' | 'missed' | 'cancelled' | None (anonymous).
    # One extra query per row (this attempt lookup isn't annotated onto the
    # queryset) — accepted for now: session lists are small/curated
    # (typically one Daily Test's worth of occurrences, or an admin's
    # schedule view), not catalog-scale like the QBank listing Phase 3 had
    # to optimize. Flagged, not silently ignored, if this ever needs to
    # move to a query-level annotation.
    my_status = serializers.SerializerMethodField()
    # Phase 7 — read-only, written only by ExamSessionViewSet.release_solutions.
    solutions_released_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ExamSession
        fields = [
            'id', 'exam_template', 'exam_template_title', 'exam_code', 'exam_version', 'session_name',
            'start_datetime', 'end_datetime', 'registration_deadline', 'timezone', 'access_type',
            'access_courses', 'password', 'max_attempts', 'status', 'effective_status', 'my_status',
            'recurrence', 'question_count', 'total_marks', 'duration_minutes', 'negative_marking',
            'participant_count', 'created_by_name', 'created_at', 'updated_at',
            'solutions_released_at', 'solutions_released_by_name',
        ]
        read_only_fields = ['exam_template', 'exam_version', 'status', 'solutions_released_at']

    def get_total_marks(self, obj):
        return float(obj.exam_version.total_marks)

    def get_created_by_name(self, obj):
        return _staff_name(obj.created_by)

    def get_solutions_released_by_name(self, obj):
        return _staff_name(obj.solutions_released_by)

    def get_effective_status(self, obj):
        from .lifecycle import compute_effective_session_status
        return compute_effective_session_status(obj)

    def get_my_status(self, obj):
        from .lifecycle import compute_effective_session_status

        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return None
        attempt = obj.attempts.filter(user=user).order_by('-start_time').first()
        if attempt:
            return attempt.status  # 'in_progress' or 'submitted'
        effective = compute_effective_session_status(obj)
        if effective == 'completed':
            return 'missed'
        if effective == 'cancelled':
            return 'cancelled'
        return 'not_started'


class ExamTemplateSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()
    version_count = serializers.SerializerMethodField()
    latest_session = serializers.SerializerMethodField()
    total_participants = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    courses_detail = serializers.SerializerMethodField()

    class Meta:
        model = ExamTemplate
        fields = [
            'id', 'exam_code', 'title', 'exam_type', 'created_by_name', 'created_at',
            'version_count', 'latest_session', 'total_participants', 'status', 'courses_detail',
        ]

    def get_created_by_name(self, obj):
        return _staff_name(obj.created_by)

    def get_version_count(self, obj):
        return obj.versions.count()

    def _latest_version(self, obj):
        return obj.versions.order_by('-version_number', '-created_at').first()

    def get_latest_session(self, obj):
        latest = obj.sessions.order_by('-start_datetime').first()
        if not latest:
            return None
        return ExamSessionSerializer(latest, context=self.context).data

    def get_total_participants(self, obj):
        return TestAttempt.objects.filter(session__exam_template=obj).values('user').distinct().count()

    def get_status(self, obj):
        """Published / Draft / Scheduled for the exam-management table badge
        — mirrors the same latest-version-draft + upcoming-session logic
        tests_app.views._exam_stats() uses for counting, so the badge shown
        per row always agrees with the Exam Stats card's totals."""
        latest_version = self._latest_version(obj)
        if not latest_version or latest_version.is_draft:
            return 'draft'
        if obj.sessions.filter(status__in=['scheduled', 'registration_open']).exists():
            return 'scheduled'
        return 'published'

    def get_courses_detail(self, obj):
        latest_version = self._latest_version(obj)
        if not latest_version:
            return []
        return [{'id': c.id, 'name': c.name, 'program_group': c.program_group} for c in latest_version.courses.all()]


class SavedExamViewSerializer(serializers.ModelSerializer):
    class Meta:
        model = SavedExamView
        fields = ['id', 'name', 'filters', 'created_at']


class RescheduleSerializer(serializers.Serializer):
    """Input for TestViewSet.reschedule — validated shape only; the actual
    lazy-adoption/locking/versioning logic lives in exam_versioning.py."""
    session_name = serializers.CharField(required=False, allow_blank=True)
    start_datetime = serializers.DateTimeField()
    end_datetime = serializers.DateTimeField()
    registration_deadline = serializers.DateTimeField(required=False, allow_null=True)
    timezone = serializers.CharField(required=False, default='Asia/Kathmandu')
    access_type = serializers.ChoiceField(choices=ExamSession.ACCESS_CHOICES, required=False, default='all')
    access_course_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    password = serializers.CharField(required=False, allow_blank=True)
    max_attempts = serializers.IntegerField(required=False, default=1, min_value=1)
    new_version = serializers.BooleanField(required=False, default=False)
    new_version_question_ids = serializers.ListField(child=serializers.IntegerField(), required=False)


class StartTestSerializer(serializers.Serializer):
    access_password = serializers.CharField(required=False, allow_blank=True)


class TestAttemptSerializer(serializers.ModelSerializer):
    questions = serializers.SerializerMethodField()
    answers = serializers.SerializerMethodField()
    test_title = serializers.CharField(source='test.title', read_only=True)
    duration_minutes = serializers.IntegerField(source='test.duration_minutes', read_only=True)
    questions_per_page = serializers.IntegerField(source='test.questions_per_page', read_only=True)
    preview_only = serializers.SerializerMethodField()
    session_name = serializers.CharField(source='session.session_name', read_only=True, default=None)
    # Phase 6 — additive, display-only timing info. The frontend timer must
    # never be authoritative (every attempt-mutating endpoint independently
    # re-checks the deadline server-side regardless of what these say) —
    # these exist purely so the UI can render an accurate countdown/"time's
    # up" state instead of guessing from duration_minutes alone, which
    # ignores a shorter session window.
    effective_end_at = serializers.SerializerMethodField()
    server_time = serializers.SerializerMethodField()

    class Meta:
        model = TestAttempt
        fields = [
            'id', 'test', 'test_title', 'duration_minutes', 'questions_per_page', 'attempt_number',
            'start_time', 'status', 'auto_submitted', 'questions', 'answers', 'preview_only', 'session',
            'session_name', 'effective_end_at', 'server_time',
        ]

    def get_preview_only(self, obj):
        # Phase 9: the frozen, immutable-once-started verdict — see
        # tests_app.lifecycle.attempt_is_preview_only's own docstring for
        # why this must not re-derive from the student's current
        # entitlement state once the attempt is already in progress.
        from .lifecycle import attempt_is_preview_only

        return attempt_is_preview_only(obj)

    def get_effective_end_at(self, obj):
        return effective_attempt_end(obj) if obj.status == 'in_progress' else None

    def get_server_time(self, obj):
        from django.utils import timezone
        return timezone.now()

    def get_questions(self, obj):
        # Phase 9: the frozen, persisted question set/order for this
        # attempt — the fix for the proven incident where this method
        # re-shuffled Test.questions (and re-sliced a fresh preview
        # subset) on every single GET, so the same in-progress attempt
        # could show a different question set/order on every reload.
        frozen = list(
            obj.attempt_questions.select_related('question').prefetch_related('question__options').order_by('order')
        )
        if frozen:
            # A null question means the live Question was deleted after
            # this attempt froze its list (see AttemptQuestion's own
            # docstring) — skip it rather than error; the attempt's
            # remaining questions and their order are otherwise untouched.
            qs = [aq.question for aq in frozen if aq.question_id and aq.question is not None]
        else:
            # Legacy compatibility: an in-progress attempt started before
            # this feature shipped has no frozen AttemptQuestion rows at
            # all. Preserves the exact pre-fix behavior for that
            # naturally-shrinking population only (it never regains
            # frozen rows retroactively) — never applied to a SUBMITTED
            # attempt, which this method is never called for at all (see
            # AttemptDetailView.get(), which routes a reviewable attempt
            # to TestResultSerializer/AttemptQuestionSnapshot instead).
            qs = list(obj.test.questions.all().prefetch_related('options'))
            if obj.test.shuffle_questions:
                import random
                random.shuffle(qs)
            if self.get_preview_only(obj):
                qs = qs[:obj.test.free_preview_questions]

        request = self.context.get('request')
        user = request.user if request else None
        bookmarked_question_ids = set()
        if user and user.is_authenticated:
            from academics.models import QuestionAttempt
            bookmarked_question_ids = set(
                QuestionAttempt.objects.filter(
                    user=user, question_id__in=[q.id for q in qs], is_bookmarked=True,
                ).values_list('question_id', flat=True)
            )
        context = {**self.context, 'bookmarked_question_ids': bookmarked_question_ids}
        return QuestionForAttemptSerializer(qs, many=True, context=context).data

    def get_answers(self, obj):
        """Previously-saved answers/marks for this in-progress attempt — the
        frontend restores answered/marked-for-review state from this on
        load instead of starting blank on every page mount/refresh."""
        return {
            a.question_id: {'option_id': a.selected_option_id, 'is_marked_for_review': a.is_marked_for_review}
            for a in obj.answers.all()
        }


class SubmitAnswerSerializer(serializers.Serializer):
    question_id = serializers.IntegerField()
    option_id = serializers.IntegerField(required=False, allow_null=True)
    mark_for_review = serializers.BooleanField(required=False, default=False)
    time_taken_seconds = serializers.IntegerField(required=False, allow_null=True, min_value=0)


class TestAttemptSummarySerializer(serializers.ModelSerializer):
    """Lightweight attempt shape for list views (e.g. 'My Attempts' history) —
    skips the nested question/options/explanation payload TestResultSerializer builds."""
    test_title = serializers.CharField(source='test.title', read_only=True)
    exam_type = serializers.CharField(source='test.exam_type', read_only=True)
    total_marks = serializers.SerializerMethodField()
    time_taken_seconds = serializers.SerializerMethodField()
    session_name = serializers.CharField(source='session.session_name', read_only=True, default=None)

    class Meta:
        model = TestAttempt
        fields = [
            'id', 'test', 'test_title', 'exam_type', 'score', 'total_marks', 'rank',
            'percentile', 'accuracy', 'status', 'auto_submitted', 'start_time', 'end_time', 'time_taken_seconds',
            'session', 'session_name',
        ]

    def get_total_marks(self, obj):
        return float(obj.test.total_marks)

    def get_time_taken_seconds(self, obj):
        if obj.end_time and obj.start_time:
            return int((obj.end_time - obj.start_time).total_seconds())
        return None


class SessionAttemptSerializer(TestAttemptSummarySerializer):
    """TestAttemptSummarySerializer + who took it — used only by
    ExamSessionViewSet.attempts (admin Participants/Results view)."""
    user_name = serializers.SerializerMethodField()
    user_email = serializers.CharField(source='user.email', read_only=True)

    class Meta(TestAttemptSummarySerializer.Meta):
        fields = TestAttemptSummarySerializer.Meta.fields + ['user_name', 'user_email']

    def get_user_name(self, obj):
        return f'{obj.user.first_name} {obj.user.last_name}'.strip() or obj.user.email


class AttemptQuestionSnapshotResultSerializer(serializers.Serializer):
    """Phase 8 — renders an immutable AttemptQuestionSnapshot into the
    exact same output shape QuestionResultSerializer produces (same key
    names), so the frontend needs zero changes for fields it already
    reads — only the CONTENT source changed (frozen snapshot instead of a
    live Question/Option query), never the shape or the access-control
    architecture. See docs/QUESTION_VERSIONING_DESIGN.md.

    Same `context['show_solutions']` gating as QuestionResultSerializer,
    reusing that class's own field-name lists (`_SOLUTION_QUESTION_FIELDS`/
    `_SOLUTION_OPTION_FIELDS`) so the two can never independently drift on
    what counts as "solution content" to strip.

    A plain Serializer, not a ModelSerializer — snapshot field names
    (`options_snapshot`, `selected_option_original_id`) deliberately don't
    match the output 1:1, and building this by hand is clearer than
    fighting a ModelSerializer's field-name inference for that mapping.
    """
    text = serializers.CharField()
    latex = serializers.CharField()
    image_data = serializers.JSONField()
    explanation = serializers.CharField()
    explanation_latex = serializers.CharField()
    explanation_image_data = serializers.JSONField()
    explanation_video_url = serializers.CharField()
    references = serializers.JSONField()
    key_takeaway = serializers.CharField()
    reference_book_name = serializers.CharField()
    reference_edition = serializers.CharField()
    reference_chapter = serializers.CharField()
    reference_page = serializers.CharField()
    reference_url = serializers.CharField()
    subject_name = serializers.CharField()

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Legacy raw-URL image fields — image_data alone is sufficient
        # (RichContent.js prefers it and only falls back to `image` when
        # image_data is absent), so these are always null from a snapshot.
        data['image'] = None
        data['explanation_image'] = None
        data['id'] = instance.question_id  # may be null if the live Question was since deleted
        data['public_id'] = None

        answer = self.context.get('attempt_map', {}).get(instance.question_id)
        data['selected_option_id'] = instance.selected_option_original_id
        data['is_correct'] = answer.is_correct if answer else False

        # Peer "% of students got this right" stats are inherently live,
        # ever-changing aggregates (Question.total_attempts/correct_attempts),
        # never a point-in-time fact the way correctness/explanation are —
        # deliberately kept live (via the still-existing Question FK) rather
        # than frozen, matching docs/QUESTION_VERSIONING_DESIGN.md §5's
        # reasoning for what is and isn't snapshotted. Unavailable once the
        # live Question is gone (nothing left to read it from).
        min_attempts = self.context.get('min_attempts_for_option_stats')
        if min_attempts is None:
            from academics.models import QuestionBankConfig
            min_attempts = QuestionBankConfig.load().min_attempts_for_option_stats
        q = instance.question
        if q and q.total_attempts >= min_attempts:
            data['stats_available'] = True
            data['students_correct_percent'] = round(q.correct_attempts / q.total_attempts * 100)
            data['total_responses'] = q.total_attempts
        else:
            data['stats_available'] = False
            data['students_correct_percent'] = None
            data['total_responses'] = None

        data['options'] = [
            {
                'id': o.get('id'), 'text': o.get('text', ''), 'image': None,
                'image_data': o.get('image_data'), 'latex': o.get('latex', ''), 'order': o.get('order', 0),
                'is_correct': o.get('is_correct', False), 'explanation': o.get('explanation', ''),
                'pick_count': None, 'pick_percentage': None,
            }
            for o in (instance.options_snapshot or [])
        ]

        show_solutions = self.context.get('show_solutions', True)
        data['solutions_locked'] = not show_solutions
        if not show_solutions:
            for field in QuestionResultSerializer._SOLUTION_QUESTION_FIELDS:
                data.pop(field, None)
            for option in data['options']:
                for field in QuestionResultSerializer._SOLUTION_OPTION_FIELDS:
                    option.pop(field, None)
        return data


class MissedReviewQuestionSerializer(serializers.ModelSerializer):
    """Grand Test 3.0 / GT3-4 — the Missed-Exam Review question shape.

    Deliberately a SEPARATE serializer from QuestionResultSerializer, not
    that one called with an empty attempt_map — the GT3-4 spec's own §8
    rule is that a missed review must NOT contain a student answer, score,
    rank, percentile, or attempt time AT ALL, not merely show them as
    null/false. This serializer structurally cannot leak any of those:
    there is no selected_option_id/is_correct field defined on it in the
    first place, so there is nothing for a future edit to accidentally
    re-enable by, say, changing a context default. It exists specifically
    because the two shapes (appeared vs. missed) are genuinely different,
    not merely differently-populated — matching the GT3-4 spec's own
    'ExamQuestionSerializer vs ReviewQuestionSerializer' guidance.

    Uses OptionAdminSerializer (not the public, pre-submission
    OptionSerializer) deliberately — a missed review's whole purpose is
    showing the correct answer/explanation, which OptionSerializer
    explicitly strips."""
    options = OptionAdminSerializer(many=True, read_only=True)
    subject_name = serializers.CharField(source='subject.name', read_only=True, default='')
    chapter_name = serializers.CharField(source='chapter.name', read_only=True, default='')
    topic_name = serializers.CharField(source='topic.name', read_only=True, default='')
    image_data = serializers.SerializerMethodField()
    explanation_image_data = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = [
            'id', 'public_id', 'text', 'image', 'image_data', 'latex',
            'explanation', 'explanation_image', 'explanation_image_data', 'explanation_latex',
            'explanation_video_url', 'references', 'key_takeaway', 'options',
            'subject_name', 'chapter_name', 'topic_name',
        ]

    def get_image_data(self, obj):
        from media_library.serializers import resolve_image_data
        return resolve_image_data(obj.image_asset, obj.image)

    def get_explanation_image_data(self, obj):
        from media_library.serializers import resolve_image_data
        return resolve_image_data(obj.explanation_image_asset, obj.explanation_image)


class TestResultSerializer(serializers.ModelSerializer):
    questions = serializers.SerializerMethodField()
    test_title = serializers.CharField(source='test.title', read_only=True)
    total_marks = serializers.SerializerMethodField()
    session_name = serializers.CharField(source='session.session_name', read_only=True, default=None)
    # Phase 7 — server-authoritative capability state, additive. The
    # frontend should show/hide rank and the solutions section based on
    # these, not on its own guess — the per-question `solutions_locked`
    # flag inside each item of `questions` is the actual enforcement
    # (this is a convenience summary of the same decision).
    can_view_solutions = serializers.SerializerMethodField()
    can_view_rank = serializers.SerializerMethodField()
    # GT3-4 — additive. review_status/solutions_available_at/
    # review_expires_at are explicit state metadata for the Frontend
    # (never inferred from which fields happen to be missing — see the
    # GT3-4 spec's own "prefer explicit state metadata" instruction);
    # score/rank/percentile/accuracy above are NEVER gated by these —
    # the result OVERVIEW remains permanently available regardless of
    # review_status, only the detailed `questions` breakdown responds to
    # 'expired'. All three are null for anything that isn't a scheduled
    # Grand Test — no behavior/shape change for Daily/Mock/PYQ.
    review_status = serializers.SerializerMethodField()
    solutions_available_at = serializers.SerializerMethodField()
    review_expires_at = serializers.SerializerMethodField()
    # GT3-6 — additive, null for every non-Grand-Test result (no shape
    # change for Daily/Mock/PYQ). `motivation` is the score-band copy
    # from GrandTestMotivationBand; `grand_test_recommendations` is the
    # top-3 WHAT/WHY/HOW/FUTURE-BENEFIT list built via the smart_practice
    # bridge — see tests_app.grand_test_analytics for both.
    motivation = serializers.SerializerMethodField()
    grand_test_recommendations = serializers.SerializerMethodField()

    class Meta:
        model = TestAttempt
        fields = [
            'id', 'test', 'test_title', 'score', 'total_marks', 'rank',
            'percentile', 'accuracy', 'status', 'auto_submitted', 'start_time', 'end_time', 'questions',
            'session', 'session_name', 'can_view_solutions', 'can_view_rank',
            'review_status', 'solutions_available_at', 'review_expires_at',
            'motivation', 'grand_test_recommendations',
        ]

    def get_total_marks(self, obj):
        return float(obj.test.total_marks)

    def _show_solutions(self, obj):
        # Memoized per-instance — get_questions() below and this field both
        # need the same decision; DRF instantiates one serializer per
        # attempt here (never `many=True`), so this is safe and avoids
        # computing it twice for one response.
        if not hasattr(self, '_show_solutions_cache'):
            from entitlements.services import can_view_solutions as _can_view_solutions
            request = self.context.get('request')
            self._show_solutions_cache = _can_view_solutions(request.user if request else None, obj).allowed
        return self._show_solutions_cache

    def _show_detailed_review(self, obj):
        """GT3-4 — memoized alongside _show_solutions for the same reason:
        get_questions() and get_review_status() both need this exact
        decision, computed once."""
        if not hasattr(self, '_show_detailed_review_cache'):
            from entitlements.services import can_view_detailed_review as _can_view_detailed_review
            request = self.context.get('request')
            self._show_detailed_review_cache = _can_view_detailed_review(
                request.user if request else None, obj,
            ).allowed
        return self._show_detailed_review_cache

    def get_can_view_solutions(self, obj):
        return self._show_solutions(obj)

    def get_can_view_rank(self, obj):
        from entitlements.services import can_view_rank as _can_view_rank
        request = self.context.get('request')
        return _can_view_rank(request.user if request else None, obj).allowed

    def _review_window(self, obj):
        if obj.test.exam_type != 'grand':
            return None, None
        from .lifecycle import grand_test_review_window

        return grand_test_review_window(obj.test, session=obj.session if obj.session_id else None)

    def get_review_status(self, obj):
        # Null for anything that isn't a Grand Test — no review-lifecycle
        # concept exists for Daily/Mock/PYQ (their solutions release
        # immediately/manually exactly as before GT3-4; there is no
        # 'locked pending scheduled close' or 'expired' state to report).
        if obj.test.exam_type != 'grand':
            return None
        # Locked can only ever precede expired (review_expires_at is
        # always derived AFTER solutions_available_at — see
        # grand_test_review_window) — so these two checks are mutually
        # exclusive by construction, never overlapping.
        if not self._show_solutions(obj):
            return 'locked'
        if not self._show_detailed_review(obj):
            return 'expired'
        return 'available'

    def get_solutions_available_at(self, obj):
        available_at, _ = self._review_window(obj)
        return available_at

    def get_review_expires_at(self, obj):
        _, expires_at = self._review_window(obj)
        return expires_at

    def get_motivation(self, obj):
        # GT3-6 — appeared-Grand-Test-only. Not gated by review_status:
        # the score-band message is part of the permanent result
        # OVERVIEW (same permanence rule as score/rank/percentile/
        # accuracy), never expires alongside the detailed review.
        if obj.test.exam_type != 'grand' or obj.status != 'submitted':
            return None
        if not obj.test.total_marks:
            return None
        from .grand_test_analytics import motivation_for_score

        score_pct = float(obj.score) / float(obj.test.total_marks) * 100
        return motivation_for_score(score_pct)

    def get_grand_test_recommendations(self, obj):
        # GT3-6 — same scope as get_motivation above. Computed fresh per
        # request (a single attempt's question count is small — see
        # smart_practice/source_performance.py's own stated reasoning for
        # not persisting these aggregates).
        if obj.test.exam_type != 'grand' or obj.status != 'submitted':
            return []
        request = self.context.get('request')
        user = request.user if request else None
        if not user:
            return []
        from .grand_test_analytics import appeared_student_recommendations

        return appeared_student_recommendations(user, obj)

    def get_questions(self, obj):
        # Phase 7: whether solution content (is_correct/explanation/
        # correct-option/aggregate stats) is included in each question
        # below — see entitlements.services.can_view_solutions and
        # QuestionResultSerializer's own docstring.
        show_solutions = self._show_solutions(obj)

        # GT3-4: the detailed per-question list itself can expire
        # independently of the overview above it — returns an empty list
        # (not an error; the attempt/score/rank fields on this same
        # response stay fully populated) once review_expires_at has
        # passed. Every non-Grand-Test call site is unaffected
        # (_show_detailed_review always True for them, §_show_detailed_
        # review's own docstring).
        if not self._show_detailed_review(obj):
            return []

        filter_type = self.context.get('filter', 'all')
        if not show_solutions:
            # The wrong/correct filter is itself a soft solution leak (it
            # tells the student which questions they missed, which — for a
            # small option set — can narrow down the correct answer) —
            # disabled while solutions are locked, same as the per-question
            # is_correct field it depends on.
            filter_type = 'all'
        answers = {a.question_id: a for a in obj.answers.select_related('question', 'selected_option')}

        from academics.models import QuestionBankConfig
        min_attempts = QuestionBankConfig.load().min_attempts_for_option_stats

        # Phase 8: an attempt finalized after this phase shipped has one
        # AttemptQuestionSnapshot per question, captured immutably at
        # finalization — reading from these instead of the live Question/
        # Option table is what makes review content immune to a later edit
        # (or deletion) of that content. See docs/QUESTION_VERSIONING_
        # DESIGN.md. Pre-Phase-8 attempts have none (deliberately never
        # backfilled — see that doc's §7) and fall through unchanged to
        # this method's original live-read behavior below.
        snapshots = list(obj.question_snapshots.select_related('question').order_by('order'))
        if snapshots:
            attempt_map = {
                s.question_id: answers.get(s.question_id) or Answer(question_id=s.question_id, selected_option=None, is_correct=False)
                for s in snapshots
            }
            if filter_type == 'wrong':
                snapshots = [s for s in snapshots if not attempt_map[s.question_id].is_correct]
            elif filter_type == 'correct':
                snapshots = [s for s in snapshots if attempt_map[s.question_id].is_correct]
            context = {
                'attempt_map': attempt_map, 'show_solutions': show_solutions,
                'min_attempts_for_option_stats': min_attempts,
            }
            return AttemptQuestionSnapshotResultSerializer(snapshots, many=True, context=context).data

        # --- Pre-Phase-8 fallback: the exact, unchanged original behavior ---
        # Every question in the test is reviewable, not just the ones the student answered —
        # a skipped question is still "wrong", and should still show up with its solution.
        #
        # Scalability audit Phase 1: QuestionResultSerializer (below) reads
        # subject_name, reference_book_name, image_data, explanation_image_data,
        # and the full options list for every question it serializes. Without
        # these, each was a separate lazy query per question — select_related
        # for the single-object FKs, and a Prefetch (with its own
        # select_related for each option's image_asset, mirroring the same
        # pattern already used for Question Bank browsing) for the options
        # reverse-FK — collapsing what was ~4-5 queries/question down to a
        # fixed handful for the whole list. Same base queryset
        # (obj.test.questions), so question set and ordering are unchanged —
        # only the relations attached to each row differ.
        questions = list(
            obj.test.questions.select_related(
                'subject', 'reference_book', 'image_asset', 'explanation_image_asset',
            ).prefetch_related(
                Prefetch('options', queryset=Option.objects.select_related('image_asset')),
            )
        )
        attempt_map = {
            q.id: answers.get(q.id) or Answer(question_id=q.id, selected_option=None, is_correct=False)
            for q in questions
        }
        if filter_type == 'wrong':
            questions = [q for q in questions if not attempt_map[q.id].is_correct]
        elif filter_type == 'correct':
            questions = [q for q in questions if attempt_map[q.id].is_correct]

        context = {
            'attempt_map': attempt_map,
            'min_attempts_for_option_stats': min_attempts,
            'show_solutions': show_solutions,
        }
        return QuestionResultSerializer(questions, many=True, context=context).data
