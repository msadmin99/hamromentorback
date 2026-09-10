from django.conf import settings
from django.db import models

from academics.models import Question, Subject


class ExamTemplate(models.Model):
    """The stable identity of a recurring exam (e.g. 'CEE Mock Test #01' /
    EXAM-000125) that persists across reschedules. Each Test row linked via
    Test.exam_template is one "version" of this template (question set +
    settings); each ExamSession is one scheduled occurrence of a version."""
    exam_code = models.CharField(max_length=20, unique=True, blank=True)
    title = models.CharField(max_length=255)
    exam_type = models.CharField(max_length=20, choices=[
        ('qbank', 'Question Bank'), ('daily', 'Daily Test'), ('mock', 'Mock Test'),
        ('grand', 'Grand Test'), ('pyq', 'Past Year Questions'),
    ])
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.title} ({self.exam_code})'

    def save(self, *args, **kwargs):
        if not self.exam_code:
            last = ExamTemplate.objects.count() + 1
            code = f'EXAM-{last:06d}'
            while ExamTemplate.objects.filter(exam_code=code).exists():
                last += 1
                code = f'EXAM-{last:06d}'
            self.exam_code = code
        super().save(*args, **kwargs)


class Test(models.Model):
    EXAM_TYPE_CHOICES = [
        ('qbank', 'Question Bank'),
        ('daily', 'Daily Test'),
        ('mock', 'Mock Test'),
        ('grand', 'Grand Test'),
        ('pyq', 'Past Year Questions'),
    ]
    DIFFICULTY_CHOICES = [('easy', 'Easy'), ('medium', 'Medium'), ('hard', 'Hard')]
    SOLUTIONS_VISIBILITY_CHOICES = [
        ('auto', 'Automatically, once the exam window ends'),
        ('manual', 'Only when I click "Release solutions"'),
    ]

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, help_text='Short blurb shown on the exam card, e.g. "Sharpen your skills with exam-focused practice."')
    difficulty = models.CharField(max_length=10, choices=DIFFICULTY_CHOICES, blank=True)
    exam_type = models.CharField(max_length=20, choices=EXAM_TYPE_CHOICES, default='mock')
    subject = models.ForeignKey(Subject, on_delete=models.SET_NULL, null=True, blank=True, related_name='tests')
    courses = models.ManyToManyField(
        'courses.Course', blank=True, related_name='mapped_tests',
        help_text='Which subcourse(s) this exam is assigned to. Blank = visible to no one until assigned (see '
                   'needs_course_review for the one-time legacy exception), NOT "visible to everyone" — that '
                   'opt-out-by-omission default was the root cause of exams leaking across unrelated courses.',
    )
    assigned_students = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name='assigned_tests',
        help_text='Individual students explicitly granted access regardless of course/batch assignment.',
    )
    assigned_batches = models.ManyToManyField(
        'courses.Batch', blank=True, related_name='assigned_tests',
        help_text='Specific course cohort(s) (e.g. "2082 Batch") granted access, independent of the broader courses assignment.',
    )

    questions = models.ManyToManyField(Question, through='TestQuestion', related_name='tests')

    duration_minutes = models.PositiveIntegerField(default=60)
    questions_per_page = models.PositiveIntegerField(
        default=1,
        help_text='How many questions show together on one exam page. 1 = one question per screen (classic).',
    )
    negative_marking = models.BooleanField(default=True)
    shuffle_questions = models.BooleanField(default=True)
    shuffle_options = models.BooleanField(default=True)
    max_attempts = models.PositiveIntegerField(default=1)
    solutions_visibility = models.CharField(max_length=10, choices=SOLUTIONS_VISIBILITY_CHOICES, default='auto')
    review_duration_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Grand Test 3.0 / GT3-4: how many days after the exam window closes the DETAILED per-question '
                   'review (question/answer/solution list) stays available. Null (the default) = permanent — the '
                   'exact, unchanged behavior every existing Test already has, since this field starts null for '
                   'every row. The attempt/result OVERVIEW (score, rank, percentile, accuracy) never expires '
                   'regardless of this setting — only the detailed question-by-question breakdown does. Lives here '
                   '(not on ExamSession) to match solutions_visibility/solutions_released_at, which already live at '
                   'this same Test level — a genuinely per-session override was not something any current Grand '
                   'Test usage pattern required.',
    )

    is_pro = models.BooleanField(default=False)
    is_new = models.BooleanField(default=False)
    price = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    access_password = models.CharField(max_length=50, blank=True)
    free_preview_questions = models.PositiveIntegerField(
        default=0,
        help_text="For PRO Daily/Live tests only: how many questions a student without a subscription can preview. "
                   "They can view but not submit those questions until they subscribe.",
    )

    academic_year = models.CharField(max_length=20, blank=True, help_text='e.g. 2025-26')
    university = models.CharField(
        max_length=100, blank=True,
        help_text='Conducting institution for Past Year Questions, e.g. IOM, MOE, BPKIHS, KU — the top-level '
                   'grouping on the student Past Year Questions page (Year is the level below it).',
    )
    scheduled_start = models.DateTimeField(null=True, blank=True)
    scheduled_end = models.DateTimeField(null=True, blank=True)

    is_draft = models.BooleanField(
        default=True,
        help_text='On (the default for every new exam) = only visible to staff — an admin/teacher must explicitly '
                   'assign courses/students/batches and publish before students can see it. Off = visible to '
                   'eligible students per the courses/assigned_students/assigned_batches assignment below.',
    )
    needs_course_review = models.BooleanField(
        default=False,
        help_text='Set only by the one-time migration that introduced default-deny access control, for legacy '
                   'exams that were published with no course assignment and could not be safely auto-mapped to '
                   'exactly one course. While True, this exam keeps its pre-migration "visible to everyone" '
                   'behavior so no existing access is silently revoked — an admin should assign real courses and '
                   'clear this flag. Never set for exams created after this feature shipped.',
    )

    # Phase 7 — only meaningful when solutions_visibility='manual': set once
    # an admin explicitly releases solutions for this Test's session-less
    # (anytime) attempts. See tests_app.lifecycle.can_view_solutions_for
    # /entitlements.services.can_view_solutions for the read side, and
    # TestViewSet.release_solutions for the write side. NULL = not yet
    # released. Irrelevant when solutions_visibility='auto' (see that
    # field's own help_text — 'auto' is computed from timing, not this).
    # ExamSession has its own, independent pair of these same two fields
    # for session-scoped attempts (a re-released Daily Test's Session #2
    # must never inherit Session #1's release).
    solutions_released_at = models.DateTimeField(null=True, blank=True)
    solutions_released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    # --- Reschedule / Exam Versioning ---
    # A Test row IS an "Exam Version" (it already holds the question set, duration,
    # marks, negative marking). exam_template groups multiple versions of the same
    # recurring exam together; null on every Test that predates this feature or
    # has never been rescheduled — fully backward compatible.
    exam_template = models.ForeignKey(
        'ExamTemplate', on_delete=models.SET_NULL, null=True, blank=True, related_name='versions',
        help_text='Set the first time this exam is rescheduled — groups this Test (and any later versions) under '
                   'one stable exam identity so it can have multiple scheduled Exam Sessions.',
    )
    version_number = models.PositiveIntegerField(default=1)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Which staff account created this exam — used to scope Teacher-role visibility to their own content.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-scheduled_start', '-created_at']
        indexes = [
            # Scalability audit Phase 1.5: TestViewSet.get_queryset() filters
            # by exam_type + is_draft together on essentially every list
            # request (public listing always filters is_draft=False; Admin
            # Exam Management filters both independently) — see
            # tests_app/views.py get_queryset() and tests_app/access.py
            # visible_test_queryset().
            models.Index(fields=['exam_type', 'is_draft'], name='test_examtype_isdraft_idx'),
        ]

    def __str__(self):
        return self.title

    @property
    def total_marks(self):
        return sum(float(q.marks) for q in self.questions.all())

    @property
    def question_count(self):
        return self.questions.count()


class ExamTypePolicy(models.Model):
    """Phase 5 — one canonical, admin-overridable default-config template per
    exam category (Practice/QBank, Mock, Daily, Grand, Past Year Questions).

    This is a TEMPLATE consumed once, at Test-creation time, to fill in
    unspecified fields on a new Test — never a live reference a Test keeps
    pointing back to. Changing a row here never mutates any Test already
    created; only Tests created *after* the change use the new values (see
    tests_app/policy.py: get_exam_type_defaults(), the only reader of this
    model, and TestAdminSerializer.create(), the only place its output is
    consumed). No Test field is a ForeignKey to this model, by design.

    Admin-configurable via the Django admin only, matching the same
    precedent used by entitlements.FreeStarterPolicy in Phase 2 — a new
    custom Admin-panel UI screen was judged unnecessary scope for Phase 5.
    """
    EXAM_TYPE_CHOICES = Test.EXAM_TYPE_CHOICES
    SOLUTIONS_VISIBILITY_CHOICES = Test.SOLUTIONS_VISIBILITY_CHOICES

    exam_type = models.CharField(max_length=20, choices=EXAM_TYPE_CHOICES, unique=True, primary_key=True)

    default_duration_minutes = models.PositiveIntegerField(default=60)
    default_questions_per_page = models.PositiveIntegerField(default=1)
    default_negative_marking = models.BooleanField(default=True)
    default_shuffle_questions = models.BooleanField(default=True)
    default_shuffle_options = models.BooleanField(default=True)
    default_max_attempts = models.PositiveIntegerField(default=1)
    default_solutions_visibility = models.CharField(max_length=10, choices=SOLUTIONS_VISIBILITY_CHOICES, default='auto')
    default_is_draft = models.BooleanField(
        default=True,
        help_text='On (recommended) = new exams of this category start as drafts, invisible to students until an '
                   'admin explicitly publishes them — matches Test.is_draft\'s own safe-by-default documentation.',
    )
    default_is_pro = models.BooleanField(default=False)
    default_free_preview_questions = models.PositiveIntegerField(default=0)
    default_price = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Exam type policy'
        verbose_name_plural = 'Exam type policies'
        ordering = ['exam_type']

    def __str__(self):
        return f'{self.get_exam_type_display()} policy'


class TestQuestion(models.Model):
    test = models.ForeignKey(Test, on_delete=models.CASCADE)
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order']
        unique_together = ('test', 'question')


class ExamSession(models.Model):
    """One scheduled occurrence of an Exam Template's version — this is what
    'Reschedule / Schedule Again' creates. Never mutates exam_version once it
    has any attempt; changing questions/settings instead creates a new Test
    version (see tests_app.exam_versioning.clone_test_as_new_version)."""
    STATUS_CHOICES = [
        ('draft', 'Draft'), ('scheduled', 'Scheduled'), ('registration_open', 'Registration Open'),
        ('live', 'Live'), ('completed', 'Completed'), ('cancelled', 'Cancelled'),
    ]
    ACCESS_CHOICES = [
        ('all', 'All eligible students'), ('course', 'Specific course subscribers'),
        ('membership', 'Specific membership'), ('batch', 'Specific batch/group'),
        ('private', 'Private/password protected'),
    ]
    RECURRENCE_CHOICES = [
        ('none', 'One time'), ('weekly', 'Weekly'), ('monthly', 'Monthly'), ('custom', 'Custom'),
    ]

    exam_template = models.ForeignKey(ExamTemplate, on_delete=models.CASCADE, related_name='sessions')
    exam_version = models.ForeignKey(
        Test, on_delete=models.PROTECT, related_name='sessions',
        help_text='The Test row (question set + settings) this session uses. Never changes after creation.',
    )
    session_name = models.CharField(max_length=255)
    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()
    registration_deadline = models.DateTimeField(null=True, blank=True)
    timezone = models.CharField(max_length=50, default='Asia/Kathmandu')
    access_type = models.CharField(max_length=15, choices=ACCESS_CHOICES, default='all')
    access_courses = models.ManyToManyField(
        'courses.Course', blank=True, related_name='+',
        help_text="Used when access_type='course' — which course(s)' subscribers may take this session.",
    )
    password = models.CharField(max_length=50, blank=True, help_text="Used when access_type='private'.")
    max_attempts = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')

    # Future-ready for recurring scheduling (see spec #15) — schema only,
    # no recurrence-generation logic implemented yet.
    recurrence = models.CharField(max_length=10, choices=RECURRENCE_CHOICES, default='none')
    recurrence_parent = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='occurrences',
    )

    # Phase 7 — session-scoped counterpart of Test.solutions_released_at/by
    # (see that field's comment). Independent per session on purpose: a
    # Daily Test re-release (Phase 6) creates a brand new ExamSession, and
    # releasing Session #1's solutions must never leak into a still-live
    # Session #2 for the same Test.
    solutions_released_at = models.DateTimeField(null=True, blank=True)
    solutions_released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-start_datetime']

    def __str__(self):
        return f'{self.exam_template.title} — {self.session_name}'

    @property
    def participant_count(self):
        return self.attempts.values('user').distinct().count()

    def refresh_status(self):
        """Auto-transitions based on current time. Never moves a session out
        of 'cancelled' or 'draft' — those are explicit admin actions only.
        Phase 6: the transition rule itself now lives in
        tests_app.lifecycle.compute_effective_session_status() (a pure,
        read-only function reused by ExamSessionSerializer.get_effective_
        status() for list/retrieve reads, which never write) — this method
        is just that rule plus the actual DB write, so a session's status
        can never drift between what a read shows and what a write
        commits."""
        from .lifecycle import compute_effective_session_status
        new_status = compute_effective_session_status(self)
        if new_status != self.status:
            self.status = new_status
            self.save(update_fields=['status'])
        return self.status


class TestAttempt(models.Model):
    STATUS_CHOICES = [
        ('in_progress', 'In progress'),
        ('submitted', 'Submitted'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='test_attempts')
    test = models.ForeignKey(Test, on_delete=models.CASCADE, related_name='attempts')
    session = models.ForeignKey(
        ExamSession, on_delete=models.SET_NULL, null=True, blank=True, related_name='attempts',
        help_text='Null for every attempt made before this feature existed, or made directly against a Test that '
                   'has never been scheduled through a session — those keep ranking against test-wide attempts '
                   'exactly as before.',
    )
    attempt_number = models.PositiveIntegerField(default=1)
    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(null=True, blank=True)
    score = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    rank = models.PositiveIntegerField(null=True, blank=True)
    percentile = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    accuracy = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='in_progress')
    auto_submitted = models.BooleanField(
        default=False,
        help_text="Phase 6: True when this attempt was finalized by the server because its effective deadline "
                   "(MIN(start_time + test.duration_minutes, session.end_datetime)) passed, rather than by the "
                   "student's own Submit action. Deliberately NOT a separate status value — an auto-submitted "
                   "attempt is 'submitted' in every way that matters (scored, ranked, reviewable) — this is purely "
                   "an informational marker so the UI can show 'time ran out' instead of 'you submitted this' "
                   "(see tests_app/lifecycle.py's module docstring for the full reasoning).",
    )

    stats_applied_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Deadlock-fix audit: set when this attempt\'s cross-student Question/Option aggregate stat '
                   'deltas (total_attempts, correct_attempts, pick_count, pick_percentage — NOT scoring/ranking/'
                   'QuestionAttempt, which stay synchronous) have been applied by the async stats worker (see '
                   'tests_app/stats_tasks.py). Written only inside the same transaction as the apply itself (a '
                   'locked check-then-apply-then-mark), so it can only ever be non-NULL after the deltas actually '
                   'landed — never as a claim taken before the work, which a crash mid-task could otherwise leave '
                   'stuck "done" with the stats never applied. NULL is always safe to retry.',
    )

    class Meta:
        ordering = ['-start_time']
        indexes = [
            # Scalability audit Phase 1.5 — three real, confirmed query
            # shapes, not speculative:
            # (test, status, score): SubmitTestView's ranking pool
            # (tests_app/views.py) and the test-wide average-score
            # aggregation (tests_app/performance.py comparative()).
            models.Index(fields=['test', 'status', 'score'], name='ta_test_status_score_idx'),
            # (user, test, status): "this user's submitted attempts on this
            # specific test" — tests_app/performance.py comparative().
            models.Index(fields=['user', 'test', 'status'], name='ta_user_test_status_idx'),
            # (user, status): the base queryset behind the whole, uncached
            # performance dashboard — tests_app/performance.py
            # _attempts_qs()/_activity_streak()/activity_calendar(), each
            # hit on every dashboard load. A narrower prefix than the index
            # above, needed because these don't also filter by test.
            models.Index(fields=['user', 'status'], name='ta_user_status_idx'),
        ]

    def __str__(self):
        return f'{self.user} - {self.test} (#{self.attempt_number})'


class Answer(models.Model):
    attempt = models.ForeignKey(TestAttempt, on_delete=models.CASCADE, related_name='answers')
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    selected_option = models.ForeignKey('academics.Option', on_delete=models.SET_NULL, null=True, blank=True)
    is_correct = models.BooleanField(default=False)
    is_marked_for_review = models.BooleanField(default=False)
    time_taken_seconds = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        unique_together = ('attempt', 'question')


class AttemptQuestion(models.Model):
    """Phase 9 — the frozen, ordered list of questions belonging to one
    TestAttempt, persisted exactly once at attempt-creation time
    (tests_app.lifecycle.freeze_attempt_questions(), called from
    _start_attempt() immediately after the TestAttempt row is created),
    never modified afterward.

    Fixes the root cause of the Daily Test "white screen / questions
    disappearing / refresh shows Submit" production incident:
    TestAttemptSerializer.get_questions() used to re-derive the question
    set/order from Test.questions on every single GET — including a
    fresh random.shuffle() whenever Test.shuffle_questions=True (the
    model's own default) and a fresh preview-eligibility slice for a
    student without full access — so the specific questions an
    in-progress attempt showed could silently change between page loads,
    down to an empty set in the worst case. Every GET/answer/submit for
    an attempt now reads this table instead of recomputing anything.

    For a preview-only attempt (billing.access.is_preview_only() was True
    at start time), only the free_preview_questions questions actually
    selected are persisted here at all — exactly matching what the
    student's UI displays, so "a question shown as preview" and "a
    question the answer endpoint recognizes as preview" can never
    diverge again. `is_preview` records this for every row of that
    attempt purely for audit/debugging traceability — it is not consulted
    by any access decision; entitlement itself is decided once via
    lifecycle.attempt_is_preview_only() and never re-derived per question.

    `question` is SET_NULL, not CASCADE — matching AttemptQuestionSnapshot's
    own precedent below — so if a live Question is later deleted (already
    blocked by QuestionViewSet.destroy() while the question has any
    attempt history at all, but this is the defense-in-depth backstop for
    every other deletion path: the Django admin site, a management
    command, a bulk queryset .delete()), this row survives and the
    attempt stays resumable for every OTHER question in its frozen list;
    only the one row loses its live question content (get_questions()
    skips a null-question row when building the display list — the
    attempt's remaining questions and their order are otherwise
    untouched).

    Deliberately does NOT duplicate question text/options here (unlike
    AttemptQuestionSnapshot, which does — but only once, at finalize
    time, for the immutable post-submission review). During the
    in-progress phase, content is still read live via the `question` FK:
    freezing content this early would duplicate the Phase 8 snapshot
    mechanism for no benefit before the attempt is even finalized. Only
    WHICH questions and WHAT ORDER are frozen here — that was the one
    thing actually unstable.

    Grand Test 3.0 / GT3-1 addendum: `marks_snapshot`/`negative_marks_snapshot`
    freeze Question.marks/negative_marks at this SAME moment (attempt
    creation), closing a real, distinct gap the Phase 8/9 work above never
    covered: _score_and_rank() (tests_app/lifecycle.py) used to read
    Question.marks/negative_marks LIVE at finalize time, not at attempt-
    start time. For a short quiz this rarely matters, but for a Grand
    Test — a single scheduled window many students sit simultaneously,
    some finishing in 20 minutes, others using the full 3 hours — an
    admin editing a question's marks mid-window would score an earlier
    finisher and a later finisher under two different point values for
    the identical question, even though neither attempt's own history
    was ever retroactively altered after ITS OWN finalization. Both
    fields are nullable so this migration never has to backfill existing
    rows: _score_and_rank() falls back to live Question.marks/
    negative_marks whenever the snapshot is null (a pre-GT3-1 attempt, or
    the pre-Phase-9 legacy case of zero AttemptQuestion rows at all) —
    the exact same graceful-fallback shape already established by
    attempt_is_preview_only()/_create_question_snapshots() above."""
    attempt = models.ForeignKey(TestAttempt, on_delete=models.CASCADE, related_name='attempt_questions')
    question = models.ForeignKey(Question, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    order = models.PositiveIntegerField(default=0)
    is_preview = models.BooleanField(
        default=False,
        help_text='Whether this row exists because the attempt was frozen in preview-only mode at start time. '
                   'Informational/audit only — no access decision reads this field directly.',
    )
    marks_snapshot = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='Question.marks at attempt-creation time. Null for a pre-GT3-1 attempt — '
                   '_score_and_rank() then falls back to live Question.marks, exactly as it always has.',
    )
    negative_marks_snapshot = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='Question.negative_marks at attempt-creation time — see marks_snapshot.',
    )

    class Meta:
        ordering = ['order']
        # Not ('attempt', 'question') — matching AttemptQuestionSnapshot's
        # own reasoning below: `question` can legitimately be null, and a
        # unique constraint with a nullable column doesn't reliably
        # enforce uniqueness across multiple NULLs. `order` is always
        # populated (assigned from enumerate() at creation) and is
        # naturally unique per attempt by construction.
        unique_together = ('attempt', 'order')

    def __str__(self):
        return f'AttemptQuestion(attempt={self.attempt_id}, order={self.order})'


class AttemptQuestionSnapshot(models.Model):
    """Phase 8 — an immutable, point-in-time capture of exactly what one
    question (text/options/explanation/correctness) looked like when a
    specific TestAttempt was finalized, plus which option (by original,
    non-FK identity) the student selected.

    Created exactly once, inside finalize_attempt()'s own transaction
    (tests_app/lifecycle.py) — never updated afterward; no code anywhere
    calls .save()/.update() on an existing row. Exists specifically so a
    later edit to the live Question/Option (text, correct answer,
    explanation) — or even the live row's deletion — can never change what
    an already-finalized attempt's review page shows or silently null out
    which option the student picked (Answer.selected_option/QuestionAttempt.
    selected_option are both SET_NULL and do get nulled by an ordinary
    options edit — see QuestionAdminSerializer.update()). See
    docs/QUESTION_VERSIONING_DESIGN.md for the full audit and the
    versioning-vs-snapshot analysis behind this design.

    `question` is SET_NULL (not CASCADE) specifically so this row survives
    even if the live Question is later deleted — though that path is
    already blocked while historical attempts exist (see
    QuestionViewSet.destroy()), SET_NULL is the correct defensive choice
    regardless, since every field actually needed for display is already
    duplicated here, not re-derived from the live FK.

    Deliberately NOT created for attempts finalized before this model
    existed — backfilling from CURRENT (already-possibly-drifted) content
    would not recover the true original content and risks looking more
    authoritative than it is. Pre-Phase-8 attempts have no rows here; the
    result serializer falls back to the exact pre-Phase-8 live-read
    behavior for them (see docs/QUESTION_VERSIONING_DESIGN.md §7)."""
    attempt = models.ForeignKey(TestAttempt, on_delete=models.CASCADE, related_name='question_snapshots')
    question = models.ForeignKey(
        'academics.Question', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='The live Question this was captured from, if it still exists — for admin/debugging traceability '
                   'only. Every field actually used for display below is a duplicated, frozen copy, not derived '
                   'from this FK, so it stays correct even if this goes null.',
    )
    order = models.PositiveIntegerField(default=0, help_text="This question's position in the test at finalization time.")

    text = models.TextField()
    image_data = models.JSONField(null=True, blank=True, help_text='Resolved {url, variants, width, height} — see media_library.serializers.resolve_image_data.')
    latex = models.TextField(blank=True)

    explanation = models.TextField(blank=True)
    explanation_image_data = models.JSONField(null=True, blank=True)
    explanation_latex = models.TextField(blank=True)
    explanation_video_url = models.URLField(blank=True)
    key_takeaway = models.TextField(blank=True)
    references = models.JSONField(default=list, blank=True)
    reference_book_name = models.CharField(max_length=255, blank=True)
    reference_edition = models.CharField(max_length=50, blank=True)
    reference_chapter = models.CharField(max_length=255, blank=True)
    reference_page = models.CharField(max_length=50, blank=True)
    reference_url = models.URLField(blank=True)

    subject_name = models.CharField(max_length=255, blank=True)
    marks = models.DecimalField(max_digits=5, decimal_places=2, default=1)
    negative_marks = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    # [{id, text, image_data, latex, is_correct, explanation, order}, ...] —
    # a JSON list, not a child table: options are only ever read as a unit
    # alongside their question, never queried independently, so a
    # table+FK graph would add real complexity for no benefit here (see
    # docs/QUESTION_VERSIONING_DESIGN.md §5).
    options_snapshot = models.JSONField(default=list)
    # Plain integer, deliberately NOT a ForeignKey — the one field that
    # actually solves the SET_NULL problem above: nothing can null this by
    # deleting/recreating the live Option table. Matched against
    # options_snapshot[i]['id'] at read time.
    selected_option_original_id = models.PositiveIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order']
        # Not ('attempt', 'question') — `question` can legitimately be
        # null (§ above), and a unique constraint with a nullable column
        # doesn't reliably enforce uniqueness across multiple NULLs. `order`
        # is always populated (assigned from enumerate() at creation) and
        # is naturally unique per attempt by construction.
        unique_together = ('attempt', 'order')

    def __str__(self):
        return f'Snapshot: {self.text[:40]} (attempt #{self.attempt_id})'


class GrandTestMotivationBand(models.Model):
    """Grand Test 3.0 / GT3-6 — admin-configurable score-band motivational
    copy (§16/§54 of the GT3-6 spec: 'Implement configurable score-band
    messages... allow Admin to configure score-band motivational
    messages, score thresholds'). Deliberately a flat, CRUD-able table
    (mirroring ExamTypePolicy's per-row-not-singleton pattern) rather than
    a rules-engine UI — exactly the 'do NOT build a complex rule-
    management UI' scope this phase asks for.

    `recommended_practice_hint` is short, generic GUIDANCE TEXT only
    ("Advanced practice / difficult questions / timed Mock Test") — never
    a specific exam name or link. The actual, concrete, real-exam
    recommendation (verified to exist and be accessible) comes from
    smart_practice.grand_test_bridge, a completely separate concern —
    this band never claims to know what specific practice is available
    for a given student, only what KIND of practice generally suits this
    score range."""
    min_percent = models.DecimalField(max_digits=5, decimal_places=2)
    max_percent = models.DecimalField(max_digits=5, decimal_places=2)
    title = models.CharField(max_length=100, help_text='e.g. "Excellent Progress"')
    message = models.TextField()
    recommended_practice_hint = models.CharField(
        max_length=200, blank=True,
        help_text='Short, generic guidance, e.g. "Weak-topic practice + timed Mock Test" — never a specific exam name.',
    )
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-min_percent']

    def __str__(self):
        return f'{self.title} ({self.min_percent}-{self.max_percent}%)'


class SavedExamView(models.Model):
    """A staff member's saved Exam Management filter combination (Program,
    Exam Type, Status, search text, etc.) — restored with one click instead
    of re-selecting the same filters every visit. `filters` is opaque JSON
    the frontend owns the shape of; the backend only stores/scopes it."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='saved_exam_views')
    name = models.CharField(max_length=100)
    filters = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        unique_together = ('user', 'name')

    def __str__(self):
        return f'{self.name} ({self.user})'
