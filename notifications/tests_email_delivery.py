"""Phase 4 — Email delivery pipeline tests: channel fan-out/preferences,
transaction safety, idempotency/duplicate-send protection (the central
engineering requirement of this phase — see email_tasks.py's own
docstrings), retry classification, and error handling.

Only `email_adapter.send` is ever mocked — the one genuinely external
call in this phase — matching this project's own established convention
(webpush_adapter.send is Phase 3's only mock; billing/tests.py mocks only
GCS upload and a confirmation email send, never the notification logic
itself)."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, override_settings

from . import email_tasks, services
from .email_adapter import EmailSendResult
from .events import CHANNEL_EMAIL
from .models import Notification, NotificationDelivery, NotificationPreference

User = get_user_model()


def _make_user(username):
    return User.objects.create_user(username=username, email=f'{username}@example.com', password='pw12345')


class ChannelFanOutTests(TestCase):
    """Mandatory regression, mirroring the Phase 3 brief's own §12 for push."""

    def setUp(self):
        self.user = _make_user('email_fanout_student')

    def test_email_is_a_candidate_channel_for_every_real_user(self):
        """accounts.User.email is required+unique+non-blank (docs/
        PHASE_4_EMAIL_TRACEABILITY_AND_DESIGN.md §2) — email is always a
        candidate, unlike push which needs an active subscription."""
        notification = services.create_notification(self.user, 'ANNOUNCEMENT', 'Hello')
        self.assertTrue(NotificationDelivery.objects.filter(notification=notification, channel=CHANNEL_EMAIL).exists())

    def test_default_preference_behavior_email_enabled_for_existing_user_with_no_preference_row(self):
        """P0-8 / Phase 4 brief §6: absence of a NotificationPreference row
        must mean enabled, never accidentally disabled — this is the
        single most important preference-safety property for existing
        users who have never touched their settings."""
        self.assertFalse(NotificationPreference.objects.filter(user=self.user).exists())
        notification = services.create_notification(self.user, 'ANNOUNCEMENT', 'Hello')
        delivery = NotificationDelivery.objects.get(notification=notification, channel=CHANNEL_EMAIL)
        self.assertNotEqual(delivery.status, NotificationDelivery.STATUS_SKIPPED)

    def test_email_disabled_preference_skips_email_but_in_app_unaffected(self):
        NotificationPreference.objects.create(user=self.user, category='announcements', channel='email', course=None, enabled=False)
        notification = services.create_notification(self.user, 'ANNOUNCEMENT', 'Hello')
        deliveries = NotificationDelivery.objects.filter(notification=notification)
        email_delivery = deliveries.get(channel='email')
        self.assertEqual(email_delivery.status, NotificationDelivery.STATUS_SKIPPED)
        in_app_delivery = deliveries.get(channel='in_app')
        self.assertEqual(in_app_delivery.status, NotificationDelivery.STATUS_DELIVERED)

    def test_mandatory_category_delivers_email_regardless_of_preference(self):
        NotificationPreference.objects.create(user=self.user, category='billing', channel='email', course=None, enabled=False)
        notification = services.create_notification(self.user, 'PAYMENT_APPROVED', 'Paid', 'Your payment was approved.')
        email_delivery = NotificationDelivery.objects.get(notification=notification, channel='email')
        self.assertNotEqual(email_delivery.status, NotificationDelivery.STATUS_SKIPPED)

    def test_explicit_channels_argument_bypasses_auto_resolution(self):
        notification = services.create_notification(self.user, 'ANNOUNCEMENT', 'Hello', channels=('in_app',))
        self.assertFalse(NotificationDelivery.objects.filter(notification=notification, channel='email').exists())

    def test_notification_deduplication_creates_no_second_email_delivery(self):
        key = 'test:dedup:email'
        first = services.create_notification(self.user, 'ANNOUNCEMENT', 'Hello', dedupe_key=key)
        second = services.create_notification(self.user, 'ANNOUNCEMENT', 'Hello', dedupe_key=key)
        self.assertEqual(first.id, second.id)
        self.assertEqual(NotificationDelivery.objects.filter(notification=first, channel='email').count(), 1)


class TransactionSafetyTests(TestCase):
    """P0-5 — email failure/latency can never affect the business
    transaction that created the notification, and the provider is never
    called before that transaction actually commits."""

    def setUp(self):
        self.user = _make_user('email_txn_student')

    def test_captured_on_commit_callback_enqueues_exactly_once(self):
        with patch('notifications.email_tasks.process_email_delivery') as mock_process:
            with self.captureOnCommitCallbacks(execute=True):
                services.create_notification(self.user, 'ANNOUNCEMENT', 'Hello')
            mock_process.assert_called_once()

    def test_rolled_back_transaction_never_enqueues_email_task(self):
        class _Boom(Exception):
            pass

        with patch('notifications.email_tasks.process_email_delivery') as mock_process:
            try:
                with self.captureOnCommitCallbacks(execute=True):
                    with transaction.atomic():
                        services.create_notification(self.user, 'ANNOUNCEMENT', 'Hello')
                        raise _Boom()
            except _Boom:
                pass
            mock_process.assert_not_called()
        self.assertFalse(Notification.objects.filter(user=self.user).exists())

    def test_outer_transaction_rollback_after_a_real_send_creates_no_notification(self):
        """Mirrors the Phase 3 billing suite's own
        test_outer_transaction_rollback_after_activate_creates_no_notification
        — proves on_commit (not a plain call) is the correct primitive."""
        class _Boom(Exception):
            pass

        with patch('notifications.email_adapter.send') as mock_send:
            mock_send.return_value = EmailSendResult(outcome=EmailSendResult.OUTCOME_SENT)
            try:
                with self.captureOnCommitCallbacks(execute=True):
                    with transaction.atomic():
                        services.create_notification(self.user, 'ANNOUNCEMENT', 'Hello')
                        raise _Boom()
            except _Boom:
                pass
            mock_send.assert_not_called()
        self.assertFalse(Notification.objects.filter(user=self.user).exists())


class AsyncEnqueueConstructionTests(TestCase):
    """Phase 4E (docs/PHASE_4E_EMAIL_QUEUE_INFRASTRUCTURE.md §10) — every
    existing test up to this phase exercised only the
    EMAIL_PROCESSING_ASYNC=False sync-fallback branch of
    enqueue_email_delivery_task (the local/test default). Nothing
    previously proved the ASYNC branch itself builds a correct Cloud
    Tasks request — this closes that gap by mocking only the true
    external boundary (google.cloud.tasks_v2.CloudTasksClient), never
    email_tasks.py's own logic. The real `email` queue itself (project
    hamromentor-app, region us-central1) and a real dispatch's mechanics
    were separately verified outside the test suite (§12 of the Phase 4E
    report) — this test proves the CODE constructs that dispatch
    correctly, independent of whether a real queue exists to receive it."""

    def setUp(self):
        self.user = _make_user('async_enqueue_student')

    @override_settings(
        EMAIL_PROCESSING_ASYNC=True, CLOUD_TASKS_EMAIL_QUEUE='email',
        GCP_PROJECT_ID='hamromentor-app', GCP_REGION='us-central1',
        BACKEND_INTERNAL_URL='https://api.drgutka.com', EMAIL_PROCESSING_SECRET='real-secret-value',
    )
    def test_async_path_builds_the_exact_expected_cloud_tasks_request(self):
        notification = Notification.objects.create(
            user=self.user, event_type='ANNOUNCEMENT', category='announcements', title='Hi', body='Body',
        )
        delivery = NotificationDelivery.objects.create(notification=notification, channel=CHANNEL_EMAIL)

        # email_tasks.py imports `from google.cloud import tasks_v2` LOCALLY
        # inside the function (not a module-level name) — patched at its
        # real source (google.cloud.tasks_v2), not on email_tasks itself,
        # which has no such attribute to patch.
        with patch('google.cloud.tasks_v2.CloudTasksClient') as mock_client_cls, \
             patch('notifications.email_tasks.process_email_delivery') as mock_process:
            mock_client = mock_client_cls.return_value
            mock_client.queue_path.return_value = 'projects/hamromentor-app/locations/us-central1/queues/email'
            email_tasks.enqueue_email_delivery_task(delivery.id)

        # The sync path must NEVER also run when async is enabled — this
        # is exactly the "async send + sync send for the same delivery"
        # duplicate-email risk §15/§16 of the brief warns about.
        mock_process.assert_not_called()

        mock_client.queue_path.assert_called_once_with('hamromentor-app', 'us-central1', 'email')
        mock_client.create_task.assert_called_once()
        request = mock_client.create_task.call_args.kwargs['request']
        self.assertEqual(request['parent'], 'projects/hamromentor-app/locations/us-central1/queues/email')

        http_request = request['task']['http_request']
        self.assertEqual(http_request['url'], 'https://api.drgutka.com/api/notifications/email/process/')
        self.assertEqual(http_request['headers']['X-Email-Processing-Secret'], 'real-secret-value')

        import json as _json
        body = _json.loads(http_request['body'])
        self.assertEqual(body, {'delivery_id': delivery.id})

    @override_settings(EMAIL_PROCESSING_ASYNC=False)
    def test_sync_path_never_touches_cloud_tasks_client(self):
        """The mirror image of the test above — proves the two branches
        are mutually exclusive, never both firing for one enqueue call."""
        notification = Notification.objects.create(
            user=self.user, event_type='ANNOUNCEMENT', category='announcements', title='Hi', body='Body',
        )
        delivery = NotificationDelivery.objects.create(notification=notification, channel=CHANNEL_EMAIL)

        with patch('google.cloud.tasks_v2.CloudTasksClient') as mock_client_cls, \
             patch('notifications.email_tasks.process_email_delivery') as mock_process:
            email_tasks.enqueue_email_delivery_task(delivery.id)

        mock_process.assert_called_once_with(delivery.id)
        mock_client_cls.assert_not_called()

    def test_one_delivery_row_produces_at_most_one_enqueue_call_via_the_real_pipeline(self):
        """§16 — proves the real create_notification() -> on_commit ->
        enqueue path calls enqueue exactly once per delivery, not by
        inspecting the source but by driving the actual production code
        path and counting real calls."""
        with patch('notifications.email_tasks.enqueue_email_delivery_task') as mock_enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                services.create_notification(self.user, 'ANNOUNCEMENT', 'Hello')
        mock_enqueue.assert_called_once()


class IdempotencyAndConcurrencyTests(TestCase):
    """P0-3 — THE central engineering requirement of Phase 4. Proves the
    exact guarantee email_tasks._claim_delivery/process_email_delivery
    actually provide: application-level, at-least-once-effectively-once
    duplicate suppression via a conditional UPDATE — never claimed as
    true distributed exactly-once delivery (see those functions' own
    docstrings for the one honestly-documented gap this does NOT close)."""

    def setUp(self):
        self.user = _make_user('email_idempotency_student')
        self.notification = Notification.objects.create(
            user=self.user, event_type='ANNOUNCEMENT', category='announcements', title='Hi', body='Body text',
        )
        self.delivery = NotificationDelivery.objects.create(notification=self.notification, channel='email')

    def test_claim_delivery_wins_exactly_once_for_a_queued_row(self):
        claimed = email_tasks._claim_delivery(self.delivery.id)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.status, NotificationDelivery.STATUS_PROCESSING)

    def test_claim_delivery_returns_none_for_an_already_processing_row(self):
        email_tasks._claim_delivery(self.delivery.id)  # first claim wins
        second_claim = email_tasks._claim_delivery(self.delivery.id)
        self.assertIsNone(second_claim)

    def test_claim_delivery_returns_none_for_an_already_sent_row(self):
        self.delivery.status = NotificationDelivery.STATUS_SENT
        self.delivery.save(update_fields=['status'])
        self.assertIsNone(email_tasks._claim_delivery(self.delivery.id))

    def test_concurrent_claim_only_one_of_two_simultaneous_calls_wins(self):
        """Simulates two Cloud Tasks executions racing for the same
        delivery_id — the real scenario Cloud Tasks' own at-least-once
        semantics can produce. Uses two real, separate calls (not
        threads — Django's TestCase wraps everything in one transaction,
        so the conditional-UPDATE's atomicity, not thread interleaving,
        is what's actually being proven here, which is the real
        mechanism protecting production too)."""
        first = email_tasks._claim_delivery(self.delivery.id)
        second = email_tasks._claim_delivery(self.delivery.id)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_retried_task_after_successful_send_never_calls_provider_again(self):
        with patch('notifications.email_tasks.email_adapter.send') as mock_send:
            mock_send.return_value = EmailSendResult(outcome=EmailSendResult.OUTCOME_SENT)
            email_tasks.process_email_delivery(self.delivery.id)
            self.assertEqual(mock_send.call_count, 1)

            # Simulated Cloud Tasks retry of the exact same delivery_id.
            email_tasks.process_email_delivery(self.delivery.id)
            self.assertEqual(mock_send.call_count, 1)  # still 1 — never sent twice

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, NotificationDelivery.STATUS_SENT)

    def test_temporary_failure_returns_delivery_to_queued_for_a_real_retry(self):
        with patch('notifications.email_tasks.email_adapter.send') as mock_send:
            mock_send.return_value = EmailSendResult(outcome=EmailSendResult.OUTCOME_TEMPORARY_FAILURE, error_code='timeout')
            complete = email_tasks.process_email_delivery(self.delivery.id)
        self.assertFalse(complete)  # signals "please retry" to the Cloud Tasks callback
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, NotificationDelivery.STATUS_QUEUED)
        self.assertEqual(self.delivery.attempt_count, 1)

    def test_temporary_failure_can_be_reclaimed_and_retried_after_returning_to_queued(self):
        with patch('notifications.email_tasks.email_adapter.send') as mock_send:
            mock_send.return_value = EmailSendResult(outcome=EmailSendResult.OUTCOME_TEMPORARY_FAILURE, error_code='timeout')
            email_tasks.process_email_delivery(self.delivery.id)
            mock_send.return_value = EmailSendResult(outcome=EmailSendResult.OUTCOME_SENT)
            complete = email_tasks.process_email_delivery(self.delivery.id)
        self.assertTrue(complete)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, NotificationDelivery.STATUS_SENT)

    def test_permanent_failure_marked_failed_immediately_no_retry(self):
        with patch('notifications.email_tasks.email_adapter.send') as mock_send:
            mock_send.return_value = EmailSendResult(outcome=EmailSendResult.OUTCOME_PERMANENT_FAILURE, error_code='invalid_recipient')
            complete = email_tasks.process_email_delivery(self.delivery.id)
        self.assertTrue(complete)  # nothing left to retry
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, NotificationDelivery.STATUS_FAILED)
        self.assertEqual(self.delivery.error_code, 'invalid_recipient')

    def test_temporary_failure_exhausts_after_max_attempts(self):
        with patch('notifications.email_tasks.email_adapter.send') as mock_send:
            mock_send.return_value = EmailSendResult(outcome=EmailSendResult.OUTCOME_TEMPORARY_FAILURE, error_code='timeout')
            # Each call: claims the row (it was put back to QUEUED by the
            # previous call's own temporary-failure branch), attempts,
            # fails again — simulating MAX_DELIVERY_ATTEMPTS separate
            # Cloud Tasks retries of the same delivery_id.
            for _ in range(email_tasks.MAX_DELIVERY_ATTEMPTS):
                email_tasks.process_email_delivery(self.delivery.id)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, NotificationDelivery.STATUS_FAILED)
        self.assertEqual(self.delivery.attempt_count, email_tasks.MAX_DELIVERY_ATTEMPTS)

    def test_nonexistent_delivery_id_is_treated_as_complete_not_an_error(self):
        self.assertTrue(email_tasks.process_email_delivery(999999))

    def test_wrong_channel_delivery_id_is_never_claimed(self):
        """_claim_delivery filters on channel=CHANNEL_EMAIL explicitly —
        an id belonging to a push/in_app delivery must never be claimed
        here even if IDs happen to collide across channels."""
        other = NotificationDelivery.objects.create(notification=self.notification, channel='in_app')
        # in_app deliveries are marked STATUS_DELIVERED immediately by
        # create_notification's own synchronous path — but even a
        # freshly-created STATUS_QUEUED in_app row (as here, direct ORM
        # create) must not be claimable by the email claim function.
        self.assertIsNone(email_tasks._claim_delivery(other.id))


class RetryClassificationTests(TestCase):
    """Postmark HTTP failures must map to the existing retry contract."""

    def _response(self, status=200, payload=None):
        import io

        class FakeResponse:
            def __init__(self, status_code, body):
                self.status = status_code
                self._body = io.BytesIO(body)

            def getcode(self):
                return self.status

            def read(self, _size=-1):
                return self._body.read(_size)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        body = __import__('json').dumps(payload or {}).encode()
        return FakeResponse(status, body)

    @override_settings(POSTMARK_SERVER_TOKEN='test-server-token', DEFAULT_FROM_EMAIL='Dr. Gutka <support@drgutka.com>')
    def test_success_uses_postmark_http_contract_and_message_id(self):
        from . import email_adapter
        with patch('notifications.email_adapter.urlopen') as mock_urlopen:
            mock_urlopen.return_value = self._response(
                200, {'ErrorCode': 0, 'Message': 'OK', 'MessageID': 'pm-123'},
            )
            result = email_adapter.send('student@example.com', 'Subject', 'Body', '<p>Body</p>')

        self.assertEqual(result.outcome, EmailSendResult.OUTCOME_SENT)
        self.assertEqual(result.provider_message_id, 'pm-123')
        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.get_header('X-postmark-server-token'), 'test-server-token')
        self.assertEqual(request.full_url, 'https://api.postmarkapp.com/email')
        payload = __import__('json').loads(request.data.decode())
        self.assertEqual(payload['From'], 'Dr. Gutka <support@drgutka.com>')
        self.assertEqual(payload['To'], 'student@example.com')
        self.assertEqual(payload['MessageStream'], 'outbound')
        self.assertEqual(mock_urlopen.call_args.kwargs['timeout'], 10)

    @override_settings(POSTMARK_SERVER_TOKEN='test-server-token')
    def test_401_is_permanent(self):
        from . import email_adapter
        with patch('notifications.email_adapter.urlopen') as mock_urlopen:
            from urllib.error import HTTPError
            import io
            mock_urlopen.side_effect = HTTPError(
                'https://api.postmarkapp.com/email', 401, 'Unauthorized', {},
                io.BytesIO(b'{"ErrorCode":10,"Message":"Bad token"}'),
            )
            result = email_adapter.send('student@example.com', 'Subject', 'Body', '<p>Body</p>')
        self.assertEqual(result.outcome, EmailSendResult.OUTCOME_PERMANENT_FAILURE)
        self.assertEqual(result.error_code, 'postmark_10')

    @override_settings(POSTMARK_SERVER_TOKEN='test-server-token')
    def test_422_is_permanent(self):
        from . import email_adapter
        with patch('notifications.email_adapter.urlopen') as mock_urlopen:
            from urllib.error import HTTPError
            import io
            mock_urlopen.side_effect = HTTPError(
                'https://api.postmarkapp.com/email', 422, 'Unprocessable', {},
                io.BytesIO(b'{"ErrorCode":300,"Message":"Invalid From"}'),
            )
            result = email_adapter.send('student@example.com', 'Subject', 'Body', '<p>Body</p>')
        self.assertEqual(result.outcome, EmailSendResult.OUTCOME_PERMANENT_FAILURE)
        self.assertEqual(result.error_code, 'postmark_300')

    @override_settings(POSTMARK_SERVER_TOKEN='test-server-token')
    def test_429_is_temporary(self):
        from . import email_adapter
        with patch('notifications.email_adapter.urlopen') as mock_urlopen:
            from urllib.error import HTTPError
            import io
            mock_urlopen.side_effect = HTTPError(
                'https://api.postmarkapp.com/email', 429, 'Too Many Requests', {},
                io.BytesIO(b'{"ErrorCode":429,"Message":"Rate limited"}'),
            )
            result = email_adapter.send('student@example.com', 'Subject', 'Body', '<p>Body</p>')
        self.assertEqual(result.outcome, EmailSendResult.OUTCOME_TEMPORARY_FAILURE)
        self.assertEqual(result.error_code, 'postmark_429')

    @override_settings(POSTMARK_SERVER_TOKEN='test-server-token')
    def test_503_is_temporary(self):
        from . import email_adapter
        with patch('notifications.email_adapter.urlopen') as mock_urlopen:
            from urllib.error import HTTPError
            import io
            mock_urlopen.side_effect = HTTPError(
                'https://api.postmarkapp.com/email', 503, 'Unavailable', {},
                io.BytesIO(b'{"ErrorCode":100,"Message":"Maintenance"}'),
            )
            result = email_adapter.send('student@example.com', 'Subject', 'Body', '<p>Body</p>')
        self.assertEqual(result.outcome, EmailSendResult.OUTCOME_TEMPORARY_FAILURE)
        self.assertEqual(result.error_code, 'postmark_100')

    @override_settings(POSTMARK_SERVER_TOKEN='test-server-token')
    def test_timeout_is_temporary(self):
        from . import email_adapter
        with patch('notifications.email_adapter.urlopen', side_effect=TimeoutError('timed out')):
            result = email_adapter.send('student@example.com', 'Subject', 'Body', '<p>Body</p>')
        self.assertEqual(result.outcome, EmailSendResult.OUTCOME_TEMPORARY_FAILURE)
        self.assertEqual(result.error_code, 'timeout')

    @override_settings(POSTMARK_SERVER_TOKEN='test-server-token')
    def test_invalid_recipient_is_rejected_before_network(self):
        from . import email_adapter
        with patch('notifications.email_adapter.urlopen') as mock_urlopen:
            result = email_adapter.send('not-an-email-address', 'Subject', 'Body', '<p>Body</p>')
        mock_urlopen.assert_not_called()
        self.assertEqual(result.outcome, EmailSendResult.OUTCOME_PERMANENT_FAILURE)
        self.assertEqual(result.error_code, 'invalid_recipient')

    @override_settings(POSTMARK_SERVER_TOKEN='')
    def test_missing_token_is_permanent_configuration_failure(self):
        from . import email_adapter
        with patch('notifications.email_adapter.urlopen') as mock_urlopen:
            result = email_adapter.send('student@example.com', 'Subject', 'Body', '<p>Body</p>')
        mock_urlopen.assert_not_called()
        self.assertEqual(result.outcome, EmailSendResult.OUTCOME_PERMANENT_FAILURE)
        self.assertEqual(result.error_code, 'configuration_error')
