from django.conf import settings
from django.db import models
from django.utils.text import slugify


class TeacherProfile(models.Model):
    """A marketplace instructor application/profile. Deliberately separate from
    the existing `admin_role='teacher'` concept (accounts/models.py) — that one
    is an internal staff account with scoped access to the shared institutional
    question bank; this is an independent instructor who self-registers to sell
    their own courses. Kept as two distinct concepts rather than overloading
    admin_role, matching StudentProfile's existing shape/convention."""
    STATUS_CHOICES = [
        ('pending', 'Pending review'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('suspended', 'Suspended'),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='teacher_profile')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    bio = models.TextField(blank=True)
    qualification = models.CharField(max_length=255, blank=True, help_text='e.g. "MBBS | Medical Educator"')
    specialization = models.CharField(max_length=255, blank=True)
    photo = models.ImageField(upload_to='teacher_photos/', null=True, blank=True)

    submitted_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Which admin approved/rejected this application.',
    )
    rejection_reason = models.CharField(max_length=500, blank=True)

    def __str__(self):
        return f'TeacherProfile<{self.user.email}> [{self.status}]'

    @property
    def course_count(self):
        return self.user.taught_courses.filter(status='approved').count()

    @property
    def student_count(self):
        return CourseEnrollment.objects.filter(course__teacher=self.user, is_active=True).values('user').distinct().count()


class CourseCategory(models.Model):
    """Admin-manageable free-form category list, same shape as videos_app.VideoCategory."""
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'name']
        verbose_name_plural = 'Course categories'

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            suffix = 1
            while CourseCategory.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                suffix += 1
                slug = f'{base_slug}-{suffix}'
            self.slug = slug
        super().save(*args, **kwargs)


class TeacherCourse(models.Model):
    """A teacher-owned, sellable structured course — distinct from courses.Course
    (which is a subscription-gated 'program track' like CEE-PG, unrelated concept
    already wired through Subject/Video/Test/Enrollment/billing). Named
    TeacherCourse specifically so the two are never ambiguous in an import."""
    LEVEL_CHOICES = [('beginner', 'Beginner'), ('intermediate', 'Intermediate'), ('advanced', 'Advanced')]
    STATUS_CHOICES = [
        ('draft', 'Draft'), ('pending_review', 'Pending review'), ('approved', 'Approved'),
        ('rejected', 'Rejected'), ('archived', 'Archived'),
    ]
    ACCESS_DURATION_CHOICES = [
        ('lifetime', 'Lifetime'), ('30_days', '30 days'), ('90_days', '90 days'),
        ('180_days', '180 days'), ('1_year', '1 year'), ('custom', 'Custom'),
    ]

    teacher = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='taught_courses')
    category = models.ForeignKey(CourseCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name='courses')

    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=280, unique=True, blank=True)
    short_description = models.CharField(max_length=300, blank=True)
    description = models.TextField(blank=True)
    thumbnail = models.ImageField(upload_to='course_thumbnails/', null=True, blank=True)
    level = models.CharField(max_length=15, choices=LEVEL_CHOICES, blank=True)

    price = models.DecimalField(max_digits=9, decimal_places=2, default=0)
    is_free = models.BooleanField(default=False)

    access_duration_type = models.CharField(max_length=10, choices=ACCESS_DURATION_CHOICES, default='lifetime')
    access_duration_days = models.PositiveIntegerField(
        null=True, blank=True, help_text='Only used when access_duration_type="custom".',
    )

    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='draft')
    rejection_reason = models.CharField(max_length=500, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Which admin approved/rejected this course.',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.title)
            slug = base_slug
            suffix = 1
            while TeacherCourse.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                suffix += 1
                slug = f'{base_slug}-{suffix}'
            self.slug = slug
        super().save(*args, **kwargs)

    @property
    def section_count(self):
        return self.sections.count()

    @property
    def lesson_count(self):
        return CourseLesson.objects.filter(section__course=self).count()


class CourseSection(models.Model):
    course = models.ForeignKey(TeacherCourse, on_delete=models.CASCADE, related_name='sections')
    title = models.CharField(max_length=255)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f'{self.course.title} / {self.title}'


class CourseLesson(models.Model):
    """One curriculum item inside a section. `lesson_type` determines which of
    video/test/pdf_file/notes_content is populated. video/test reuse the
    existing videos_app.Video / tests_app.Test systems rather than duplicating
    them — a course's 'Video Lesson' or 'MCQ Quiz Exam' IS a Video/Test row."""
    LESSON_TYPE_CHOICES = [
        ('video', 'Video Lesson'), ('pdf', 'PDF Lesson'), ('quiz', 'MCQ Quiz Exam'),
        ('live_class', 'Live Zoom Class'), ('notes', 'Notes'),
    ]

    section = models.ForeignKey(CourseSection, on_delete=models.CASCADE, related_name='lessons')
    lesson_type = models.CharField(max_length=15, choices=LESSON_TYPE_CHOICES)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=0)

    video = models.ForeignKey('videos_app.Video', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    test = models.ForeignKey('tests_app.Test', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    pdf_file = models.FileField(upload_to='course_lesson_pdfs/', null=True, blank=True)
    notes_content = models.TextField(blank=True, help_text='Rich HTML notes content, used when lesson_type="notes".')

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f'{self.section.course.title} / {self.title}'


class CourseEnrollment(models.Model):
    """A student's access grant to a TeacherCourse. Minimal for now — created by
    an admin grant today; a future payment phase creates these the same way
    billing.Purchase.approve() already creates courses.Enrollment rows, not via
    a gateway callback (this platform's payment flow is manual-approval, not a
    live webhook integration — see billing.Purchase)."""
    SOURCE_CHOICES = [('purchase', 'Purchase'), ('admin_grant', 'Admin grant')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='course_enrollments')
    course = models.ForeignKey(TeacherCourse, on_delete=models.CASCADE, related_name='enrollments')
    source = models.CharField(max_length=15, choices=SOURCE_CHOICES, default='admin_grant')
    enrolled_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True, help_text='Blank = lifetime access.')
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('user', 'course')

    def __str__(self):
        return f'{self.user.email} -> {self.course.title}'
