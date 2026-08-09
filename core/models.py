from django.db import models

from academics.models import Question


class Banner(models.Model):
    title = models.CharField(max_length=255)
    subtitle = models.CharField(max_length=255, blank=True)
    tag = models.CharField(max_length=50, blank=True, help_text='e.g. NEW PATTERN')
    image = models.ImageField(upload_to='banners/', null=True, blank=True)
    link_url = models.URLField(blank=True)
    background_color = models.CharField(max_length=20, default='#1447E6')
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return self.title


class MCQOfTheDay(models.Model):
    """`date` is intentionally NOT unique — admin can add one entry per course
    for the same date (e.g. an Anatomy question for CEE-PG and a Physics
    question for CEE-UG on the same day), scoped via the question's own
    `Question.courses` M2M. A question with no courses tagged applies to
    everyone. HomeDashboardView picks whichever entry matches the viewing
    student's active course."""
    date = models.DateField()
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    label = models.CharField(max_length=100, default='MCQ of the Day')

    def __str__(self):
        return f'{self.date} - {self.question}'


class Announcement(models.Model):
    message = models.CharField(max_length=255)
    coupon_code = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.message


class SiteSettings(models.Model):
    """Singleton (pk always 1) holding the editable text for the public
    homepage's header, hero, stats and footer sections."""

    # Header / nav
    app_badge_text = models.CharField(max_length=100, default='📱 App coming soon')
    nav_cta_text = models.CharField(max_length=50, default='Login/Signup')

    # Hero
    hero_headline = models.CharField(max_length=255, default='The Trusted Path to CEE-PG & NMCLE Success')
    hero_subtitle = models.TextField(
        default='QBank, mock tests and video lectures for CEE-UG, CEE-PG and NMCLE aspirants — '
                'built for how the real exam works.'
    )
    hero_cta_primary_text = models.CharField(max_length=50, default='Get Started')
    hero_cta_primary_link = models.CharField(max_length=255, default='/register')
    hero_cta_secondary_text = models.CharField(max_length=50, default='View Courses ›')
    hero_cta_secondary_link = models.CharField(max_length=255, default='#courses')
    hero_badge_icon = models.CharField(max_length=10, default='🔥')
    hero_badge_title = models.CharField(max_length=100, default='MCQ of the Day')
    hero_badge_tag = models.CharField(max_length=30, default='LIVE')
    hero_badge_subtitle = models.CharField(max_length=150, default='A fresh high-yield MCQ, every day')
    hero_badge_cta_text = models.CharField(max_length=50, default='Check it out ›')
    hero_badge_link = models.CharField(max_length=255, default='/login')

    # Stats / test-series section
    stats_icon = models.CharField(max_length=10, default='📝')
    stats_headline = models.CharField(
        max_length=255, default="A growing Test Series built for Nepal's medical aspirants",
    )
    stats_headline_highlight = models.CharField(
        max_length=100, default='Test Series', blank=True,
        help_text='Substring of the headline above to highlight in brand blue.',
    )
    stats_body = models.TextField(
        default='Grand, Mini and Subject tests with negative marking, shuffled questions and instant '
                'subject-wise scoring — practice under exam conditions before the real thing, and see '
                'exactly where you stand.'
    )
    stats_cta_text = models.CharField(max_length=50, default='Get started ›')
    stats_cta_link = models.CharField(max_length=255, default='/register')
    stats_image = models.ImageField(
        upload_to='site/', null=True, blank=True,
        help_text='Optional override for the phone-preview image (defaults to the built-in app screenshot).',
    )

    # Features section
    features_heading = models.CharField(max_length=255, default='What makes Dr. Gutka complete?')
    features_heading_highlight = models.CharField(max_length=100, default='Dr. Gutka', blank=True)

    # Courses section
    courses_eyebrow = models.CharField(max_length=50, default='Pick your track')
    courses_heading = models.CharField(max_length=100, default='Choose your course')
    courses_subtitle = models.TextField(
        default='Every track comes with its own mock tests, question bank and video lectures. '
                'Pick yours and start practicing in minutes.'
    )

    # Footer
    footer_copyright = models.CharField(max_length=255, default='© 2026 Dr. Gutka. All rights reserved.')

    class Meta:
        verbose_name = 'Site settings'
        verbose_name_plural = 'Site settings'

    def __str__(self):
        return 'Site settings'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class HomeFeature(models.Model):
    """One tile in the numbered 'What makes Dr. Gutka complete?' grid."""
    number = models.CharField(max_length=4, default='01')
    title = models.CharField(max_length=150)
    body = models.TextField()
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f'{self.number} {self.title}'


class SiteLink(models.Model):
    """A single link shown in the navbar or the footer."""
    SECTION_CHOICES = [('nav', 'Navbar'), ('footer', 'Footer')]

    section = models.CharField(max_length=10, choices=SECTION_CHOICES)
    label = models.CharField(max_length=100)
    url = models.CharField(max_length=255)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['section', 'order']

    def __str__(self):
        return f'[{self.section}] {self.label}'
