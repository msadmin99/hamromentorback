"""Grand Test 3.0 / GT3-7 — Concurrency Protection.

Two real, reproduced races closed by this phase (both required a real
TransactionTestCase — the default APITestCase wraps a whole test in one
outer transaction that never commits, which hides exactly what these
tests exist to catch; see SubmitTestDoubleSubmissionRaceTests in
tests_app/tests.py for the same, already-established pattern this file
mirrors):

1. Start concurrency (§42): two simultaneous start requests from the SAME
   user must never create two 'official' TestAttempt rows.
2. Ranking concurrency (§46): two different students finalizing at
   genuinely the same instant must still get ranks that reflect their
   relative scores, not both computed against an incomplete pool.
"""
import threading
import time

from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient, APITransactionTestCase

from academics.models import Option, Question, Subject
from tests_app.models import Answer, Test, TestAttempt, TestQuestion

User = get_user_model()


def _sqlite_retry(fn, results, lock, attempts=20):
    """Shared retry wrapper — SQLite (this suite's test DB) has no
    per-row locking and can surface contention as 'database is locked'
    instead of the real target database's (MySQL/InnoDB) blocking
    select_for_update() wait. Re-applies PRAGMA busy_timeout per attempt
    since connection.close() below tears down the connection it was set
    on — identical reasoning to SubmitTestDoubleSubmissionRaceTests."""
    for attempt_no in range(attempts):
        try:
            if connection.vendor == 'sqlite':
                with connection.cursor() as cur:
                    cur.execute('PRAGMA busy_timeout = 30000')
            result = fn()
            with lock:
                results.append(result)
            return
        except Exception as exc:  # noqa: BLE001 — SQLite lock-contention retry
            if 'locked' in str(exc).lower() and attempt_no < attempts - 1:
                time.sleep(0.05)
                continue
            raise
        finally:
            connection.close()


class StartAttemptConcurrencyTests(APITransactionTestCase):
    """GT3-7 §42 — one user, multiple simultaneous start requests, must
    never produce multiple TestAttempt rows."""

    def test_concurrent_start_creates_only_one_attempt(self):
        student = User.objects.create_user(username='race_start', email='race_start@example.com', password='pw12345')
        subject = Subject.objects.create(name='Race Start Subject')
        question = Question.objects.create(subject=subject, text='Q?', marks=1, negative_marks=0)
        Option.objects.create(question=question, text='A', order=0, is_correct=True)
        test = Test.objects.create(title='Race Start Exam', exam_type='mock', is_draft=False, max_attempts=1)
        test.assigned_students.set([student])
        TestQuestion.objects.create(test=test, question=question)

        status_codes = []
        lock = threading.Lock()

        def start_once():
            # _start_attempt itself catches OperationalError (SQLite lock
            # contention) and returns a 409 Response rather than raising —
            # the exact same defensive pattern as TestViewSet.reschedule's
            # own lock-contention handling (see that view's own comment).
            # That means _sqlite_retry's exception-based retry never fires
            # here (no exception ever reaches it, just a 409 response), so
            # this function retries on the 409 status itself instead —
            # under real production row-level locking (MySQL/InnoDB) a
            # "loser" thread simply blocks and then succeeds, so this
            # retry loop mirrors what that blocking wait effectively does.
            client = APIClient()
            client.force_authenticate(user=student)
            for attempt_no in range(20):
                resp = client.post(f'/api/tests/{test.id}/start/')
                if resp.status_code != 409 or attempt_no == 19:
                    return resp.status_code
                time.sleep(0.05)
            return resp.status_code

        threads = [
            threading.Thread(target=_sqlite_retry, args=(start_once, status_codes, lock)) for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Loosely checked (SQLite's table-level contention can still
        # surface as a 409 for a "loser" thread that exhausts its own
        # retry budget under heavy parallel test-suite load) — the actual
        # invariant is the DB state below: never more than one
        # TestAttempt, regardless of engine or how many 409s occurred.
        self.assertTrue(set(status_codes).issubset({200, 201, 409}), status_codes)
        self.assertLessEqual(TestAttempt.objects.filter(user=student, test=test).count(), 1)
        if 409 not in status_codes:
            # No contention was even observed this run (all 5 threads
            # serialized cleanly through the lock) — then the invariant
            # can be checked at full strength.
            self.assertEqual(TestAttempt.objects.filter(user=student, test=test).count(), 1)


class RankingConcurrencyTests(APITransactionTestCase):
    """GT3-7 §46 — two different students, different scores, finalizing
    at genuinely the same instant must still get ranks reflecting their
    relative scores (never both rank=1).

    IMPORTANT, HONEST LIMITATION: the fix (tests_app/lifecycle.py's
    _score_and_rank now takes Test.objects.select_for_update() before
    reading the ranking pool) depends on a real blocking row lock — the
    second transaction's SELECT must wait for the first transaction's
    COMMIT. SQLite (this suite's test database) does not implement
    row-level locking at all: select_for_update() against it emits a
    plain SELECT with no blocking semantics, so two threads can both read
    the ranking pool before either commits regardless of this fix,
    exactly as they could before it. This is a genuine gap in what this
    test environment can prove — recorded here rather than hidden behind
    a test that silently exercises a different (looser) invariant than it
    claims to. What IS empirically verified below: the fix introduces no
    crash, deadlock, or scoring corruption under concurrent finalization,
    and each attempt's own rank/percentile is still computed via the
    exact same, unmodified formula. The 'different scores -> correctly
    ordered ranks under true simultaneous finalization' guarantee itself
    is verified by code inspection only (the lock is real and correct
    against production MySQL/InnoDB, which does block a second
    transaction's SELECT ... FOR UPDATE until the first commits) and is
    explicitly NOT exercised under test — see the GT3-7 final report's
    Concurrency Audit section."""

    def test_concurrent_finalization_produces_consistent_ranks(self):
        subject = Subject.objects.create(name='Race Rank Subject')
        q1 = Question.objects.create(subject=subject, text='Q1', marks=1, negative_marks=0)
        q1_correct = Option.objects.create(question=q1, text='Right', order=0, is_correct=True)
        q1_wrong = Option.objects.create(question=q1, text='Wrong', order=1, is_correct=False)

        test = Test.objects.create(title='Race Rank Exam', exam_type='mock', is_draft=False, negative_marking=False)
        TestQuestion.objects.create(test=test, question=q1)

        high_scorer = User.objects.create_user(username='race_high', email='race_high@example.com', password='pw12345')
        low_scorer = User.objects.create_user(username='race_low', email='race_low@example.com', password='pw12345')

        high_attempt = TestAttempt.objects.create(user=high_scorer, test=test, status='in_progress')
        Answer.objects.create(attempt=high_attempt, question=q1, selected_option=q1_correct, is_correct=True)

        low_attempt = TestAttempt.objects.create(user=low_scorer, test=test, status='in_progress')
        Answer.objects.create(attempt=low_attempt, question=q1, selected_option=q1_wrong, is_correct=False)

        results = []
        lock = threading.Lock()

        def submit(user, attempt_id):
            def _do():
                client = APIClient()
                client.force_authenticate(user=user)
                resp = client.post(f'/api/attempts/{attempt_id}/submit/')
                return resp.status_code
            return _do

        threads = [
            threading.Thread(target=_sqlite_retry, args=(submit(high_scorer, high_attempt.id), results, lock)),
            threading.Thread(target=_sqlite_retry, args=(submit(low_scorer, low_attempt.id), results, lock)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        high_attempt.refresh_from_db()
        low_attempt.refresh_from_db()
        # What SQLite CAN prove: concurrent finalization completes
        # cleanly, both attempts score correctly (no corruption/crash/
        # deadlock), and each gets a real rank — see this class's own
        # docstring for why the stronger 'high scorer's rank is strictly
        # better' invariant cannot be validated against this test
        # database and is instead verified by code inspection only.
        self.assertEqual(high_attempt.status, 'submitted')
        self.assertEqual(low_attempt.status, 'submitted')
        self.assertGreater(float(high_attempt.score), float(low_attempt.score))
        self.assertIsNotNone(high_attempt.rank)
        self.assertIsNotNone(low_attempt.rank)
