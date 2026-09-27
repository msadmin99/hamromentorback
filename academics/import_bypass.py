"""Bulk-import "Skip this error" (a.k.a. IMPORT-ANYWAY) policy.

Vocabulary — kept deliberately distinct, because "skip" already means two
different things in this app:

  * SKIP (error)      = the admin acknowledges a validation error and chooses to
                        import the question ANYWAY. The row keeps its
                        `status='error'` and its error list (an audit trail of
                        what was ignored); `ImportRow.error_skipped=True` is
                        the persisted decision. The stored field name predates
                        this meaning and is kept only to avoid a schema
                        migration — in code, prefer the helpers below.
  * DELETE            = the row is removed from the batch and is never imported.
  * dedup "skip"      = a duplicate row is not re-created; the existing
                        question is attached instead. Unrelated to this module.

A bypass is NEVER trusted from the client. Whether an error may be ignored is
decided here, on the server, from the row's own `raw_data` (re-validated every
time), at three points: when the admin sets the flag, when a row is edited, and
again at the moment questions are created. See
import_validation.BYPASSABLE_ISSUE_CODES for which errors qualify and why.
"""
from typing import NamedTuple, Optional

from .import_validation import collect_validation_issues


class BypassAssessment(NamedTuple):
    eligible: bool
    # Messages of errors that make the row impossible/unsafe to import.
    blocking_messages: list
    # Messages of errors the admin would be choosing to ignore.
    ignorable_messages: list


def assess_bypass(raw_data):
    """Re-validates `raw_data` and reports whether every current error on it
    may be ignored. A row with no current errors at all is trivially eligible
    (e.g. its stored `errors` are stale from an earlier failed attempt)."""
    issues, _warnings = collect_validation_issues(raw_data or {})
    blocking = [i.message for i in issues if not i.bypassable]
    ignorable = [i.message for i in issues if i.bypassable]
    return BypassAssessment(eligible=not blocking, blocking_messages=blocking, ignorable_messages=ignorable)


def is_error_bypassed(row):
    """True if this row is an Error the admin chose to import anyway."""
    return row.status == 'error' and bool(row.error_skipped)


def cannot_bypass_reason(assessment):
    """One human-readable sentence explaining why Skip is not allowed."""
    return (
        "This error can't be skipped because the question can't be imported safely: "
        + ' '.join(assessment.blocking_messages)
        + ' Fix the question, or delete it.'
    )


class ImportProblem(NamedTuple):
    reason: str
    # True when the row was marked "Skip" for an error that may NOT be
    # ignored — the caller withdraws that stale decision.
    clear_skip: bool


def importability_problem(row) -> Optional[ImportProblem]:
    """IMPORT-TIME SAFETY NET. Called for every row about to become a new
    Question, in both import paths (Add to Exam / Create Test and the
    question-bank run_import), regardless of the row's `status`. Nothing
    upstream may be trusted to have kept a row importable: a duplicate row
    can be edited into an unimportable state, and a Skip flag can be set by
    a buggy or malicious client.

    Judged fresh from the row's own data:
      * a structural error (blank stem, < 2 options, no correct option)
        always blocks — Skip can never override it;
      * an error that Skip MAY override (a duplicated option) blocks only if
        the admin has not skipped it;
      * a row with no current errors is always fine.
    Returns None when the row may be imported."""
    issues, _warnings = collect_validation_issues(row.raw_data or {})
    blocking = [i.message for i in issues if not i.bypassable]
    ignorable = [i.message for i in issues if i.bypassable]
    if blocking:
        if row.error_skipped:
            return ImportProblem(cannot_bypass_reason(BypassAssessment(False, blocking, ignorable)), clear_skip=True)
        return ImportProblem(
            "This question can't be imported because of a structural error: " + ' '.join(blocking)
            + ' Fix the question, or delete it.',
            clear_skip=False,
        )
    if ignorable and not row.error_skipped:
        return ImportProblem(
            'This question still has an unresolved validation error (' + ' '.join(ignorable)
            + ') — skip the error to import it anyway, or fix the question.',
            clear_skip=False,
        )
    return None


def attaches_existing_only(row):
    """A duplicate row decided "skip" does not create a question at all (the
    existing one is attached instead), so its own data is never persisted and
    the safety net does not apply to it."""
    return row.status == 'duplicate' and row.dedup_action == 'skip'
