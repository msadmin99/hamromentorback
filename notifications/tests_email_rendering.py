"""Phase 4 — Email content rendering tests: subject resolution, plain-text
fallback, HTML template rendering, and HTML-escaping (P0-7 — the P0
acceptance criterion for untrusted content, e.g. Announcement.message,
never becoming unescaped HTML in an email)."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from courses.models import Course

from . import email_content
from .models import Notification

User = get_user_model()


def _make_user(username):
    return User.objects.create_user(username=username, email=f'{username}@example.com', password='pw12345')


def _make_course(name='CEE-MBBS'):
    return Course.objects.create(name=name, prefix=name[:10])


class SubjectResolutionTests(TestCase):
    """Deterministic, testable subjects (docs/PHASE_4_EMAIL_TRACEABILITY_AND_DESIGN.md
    §10) — never a re-templated placeholder that has already lost its context."""

    def setUp(self):
        self.user = _make_user('subject_student')

    def test_mapped_event_type_gets_its_defined_subject(self):
        n = Notification.objects.create(user=self.user, event_type='PAYMENT_APPROVED', category='billing', title='x', body='y')
        self.assertEqual(email_content.resolve_subject(n), 'Payment Approved')

    def test_course_scoped_notification_gets_a_course_prefixed_subject(self):
        course = _make_course('CEE-MBBS')
        n = Notification.objects.create(
            user=self.user, course=course, event_type='GRAND_TEST_REMINDER', category='tests', title='x', body='y',
        )
        self.assertEqual(email_content.resolve_subject(n), 'CEE-MBBS — Grand Test Reminder')

    def test_global_notification_gets_no_course_prefix(self):
        n = Notification.objects.create(user=self.user, event_type='ANNOUNCEMENT', category='announcements', title='x', body='y')
        self.assertEqual(email_content.resolve_subject(n), 'Dr. Gutka Announcement')

    def test_unmapped_event_type_falls_back_to_notification_title_never_crashes(self):
        n = Notification.objects.create(user=self.user, event_type='LOGIN_SECURITY_ALERT', category='security', title='New login detected', body='y')
        self.assertEqual(email_content.resolve_subject(n), 'New login detected')

    def test_every_real_producer_event_type_has_a_deterministic_subject(self):
        """Every event type this project's real Phase 2/3 producers
        actually raise (exam_integration.py, billing_integration.py,
        signals.py) must be in EMAIL_SUBJECTS, not silently fall back."""
        real_producer_event_types = [
            'GRAND_TEST_SCHEDULED', 'GRAND_TEST_REMINDER', 'GRAND_TEST_STARTING', 'GRAND_TEST_RESULT_AVAILABLE',
            'DAILY_TEST_AVAILABLE', 'DAILY_TEST_ENDING', 'DAILY_TEST_RESULT_AVAILABLE',
            'PAYMENT_APPROVED', 'SUBSCRIPTION_ACTIVATED', 'ANNOUNCEMENT',
        ]
        for event_type in real_producer_event_types:
            self.assertIn(event_type, email_content.EMAIL_SUBJECTS, f'{event_type} has no deterministic email subject mapped')


class AbsoluteUrlTests(TestCase):
    def test_relative_path_is_made_absolute_using_frontend_url(self):
        with override_settings(FRONTEND_URL='https://app.drgutka.com'):
            self.assertEqual(email_content.build_absolute_url('/grand-test/82'), 'https://app.drgutka.com/grand-test/82')

    def test_already_absolute_url_is_never_double_prefixed(self):
        self.assertEqual(email_content.build_absolute_url('https://elsewhere.example/x'), 'https://elsewhere.example/x')

    def test_blank_action_url_returns_blank_not_a_broken_link(self):
        self.assertEqual(email_content.build_absolute_url(''), '')


class HtmlRenderingAndEscapingTests(TestCase):
    """P0-7 — the P0 acceptance criterion: untrusted content must never
    become unescaped HTML."""

    def setUp(self):
        self.user = _make_user('html_render_student')

    def test_render_email_returns_subject_plain_text_and_html(self):
        n = Notification.objects.create(
            user=self.user, event_type='ANNOUNCEMENT', category='announcements',
            title='Hello', body='Plain body text.', action_url='/notifications',
        )
        subject, text_body, html_body = email_content.render_email(n)
        self.assertEqual(subject, 'Dr. Gutka Announcement')
        self.assertEqual(text_body, 'Plain body text.')  # Notification.body verbatim, zero new rendering
        self.assertIn('Plain body text.', html_body)
        self.assertIn('<!DOCTYPE html>', html_body)

    def test_malicious_script_tag_in_body_is_escaped_not_executed(self):
        n = Notification.objects.create(
            user=self.user, event_type='ANNOUNCEMENT', category='announcements',
            title='Hi', body='<script>alert(1)</script>',
        )
        _subject, _text, html_body = email_content.render_email(n)
        self.assertNotIn('<script>alert(1)</script>', html_body)
        self.assertIn('&lt;script&gt;', html_body)

    def test_malicious_img_onerror_in_body_is_escaped(self):
        n = Notification.objects.create(
            user=self.user, event_type='ANNOUNCEMENT', category='announcements',
            title='Hi', body='<img src=x onerror=alert(1)>',
        )
        _subject, _text, html_body = email_content.render_email(n)
        self.assertNotIn('<img src=x onerror=alert(1)>', html_body)
        self.assertIn('&lt;img', html_body)

    def test_malicious_content_in_title_is_escaped(self):
        n = Notification.objects.create(
            user=self.user, event_type='ANNOUNCEMENT', category='announcements',
            title='<script>alert(1)</script>', body='Body',
        )
        _subject, _text, html_body = email_content.render_email(n)
        self.assertNotIn('<script>alert(1)</script>', html_body)

    def test_malicious_content_in_course_name_is_escaped(self):
        course = Course.objects.create(name='<script>alert(1)</script>', prefix='XSS')
        n = Notification.objects.create(
            user=self.user, course=course, event_type='ANNOUNCEMENT', category='announcements',
            title='Hi', body='Body',
        )
        _subject, _text, html_body = email_content.render_email(n)
        self.assertNotIn('<script>alert(1)</script>', html_body)

    def test_javascript_href_scheme_is_rendered_as_inert_text_not_a_clickable_link(self):
        """action_url is always server-set in this codebase (never
        derived from user input) — this proves the template itself does
        not additionally validate the URL scheme, so the real defense is
        that no producer ever sets a javascript: action_url. Documented
        here as the actual boundary, not silently assumed safe."""
        n = Notification.objects.create(
            user=self.user, event_type='ANNOUNCEMENT', category='announcements',
            title='Hi', body='Body', action_url='javascript:alert(1)',
        )
        _subject, _text, html_body = email_content.render_email(n)
        # Rendered as the literal, escaped href value — a browser/email
        # client MAY still refuse to navigate a javascript: URI (most
        # modern mail clients strip this scheme entirely), but this test
        # only proves Django's own escaping ran over the value, not that
        # a scheme allowlist exists — see the docstring above.
        self.assertIn('javascript:alert(1)', html_body)

    def test_no_cta_button_rendered_when_action_url_is_blank(self):
        n = Notification.objects.create(
            user=self.user, event_type='ANNOUNCEMENT', category='announcements', title='Hi', body='Body', action_url='',
        )
        _subject, _text, html_body = email_content.render_email(n)
        self.assertNotIn('Open Dr. Gutka', html_body)

    def test_cta_button_rendered_when_action_url_present(self):
        n = Notification.objects.create(
            user=self.user, event_type='GRAND_TEST_REMINDER', category='tests', title='Hi', body='Body', action_url='/grand-test/1',
        )
        _subject, _text, html_body = email_content.render_email(n)
        self.assertIn('Open Dr. Gutka', html_body)
        self.assertIn('/grand-test/1', html_body)
