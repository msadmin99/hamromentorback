# Daily Test Android/Redmi White-Screen Investigation — CLOSED

## Status

Closed with production workaround/configuration. Not root-cause-fixed.

## Confirmed facts

- 1 MCQ per page works correctly on the affected Redmi devices.
- More than 1 MCQ per page (specifically observed at 10/page) could produce
  a blank/white question area or navigation to Submit on affected Redmi
  devices when the student scrolled.
- Desktop Safari (Mac) works correctly regardless of questions-per-page.
- An automated Playwright mobile-Chromium diagnostic harness — a real
  Chromium engine with mobile viewport/touch emulation, driving the actual
  deployed app code end-to-end against a local stack seeded with Daily
  Tests at 1/2/3/5/10/20 questions per page — ran 304 scenario executions
  (every page-size × 8 scroll/answer action sequences, with the two
  implicated sizes run 10 times each) and reproduced **zero** blank-screen
  or unexpected-submit events. This rules out a pure JS/DOM/scroll-state
  bug in the app's own code as the sole explanation; it does not rule out
  a real Android OS/IME-level mechanism, since Chromium's mobile emulation
  cannot simulate a real on-screen keyboard/IME/autofill viewport-resize
  event.
- A separate, related fix — `.hm-app-shell`'s CSS height changed from
  `100dvh` to `100svh` (`Frontend/src/app/globals.css`, commit `444ae34`)
  — was investigated, implemented, tested, and deployed to production.
  This closed a *confirmed, reproduced-from-source* mechanism (an Android
  keyboard/IME/autofill surface shrinking the dynamic viewport, collapsing
  the exam shell's flex-1 question region to zero height under
  `overflow: hidden`), triggered by the accessibility remediation's
  native, focusable radio inputs. It remained deployed after this
  incident and was **not reverted** — but it did not eliminate every
  report of the underlying Redmi issue.

## Root cause

**Root cause remains unconfirmed.** Evidence suggests a device/browser-
specific interaction involving multi-question rendering and mobile
scrolling, but no telemetry from the actual affected device (real Android
remote-debugging session, console/network/DOM capture at the moment of
failure) has been obtained. The `100dvh`→`100svh` mechanism above is a
confirmed, real, separately-fixed issue — it is not established that it is
the same mechanism behind the specific multi-question-per-page reports
that prompted this closure.

## Production decision

All production student-facing Tests (`exam_type` in `daily`, `mock`,
`grand`, `pyq` — the exam types that share the same attempt player,
`Frontend/src/app/tests/attempt/[attemptId]/page.js`) are configured with
`questions_per_page = 1`. QBank is unaffected and out of scope — it does
not use this attempt player at all.

This was a pure production **data** change on the existing
`tests_app.Test.questions_per_page` field — no schema change, no
migration, no application code change. 58 rows were updated (57 Daily
Tests previously at 10 or 20 per page, 1 PYQ test — id 24, "IOM 2011" —
previously at 20 per page). Mock, Grand, and every other PYQ test were
already at 1 per page. Historical (submitted) attempts are unaffected —
this field is read live at serialization time for in-progress attempts'
pagination display only; it does not touch `AttemptQuestion` rows,
`Answer` rows, scores, or any already-finalized result.

## Risk

The multi-question-per-page Redmi issue remains an unresolved
compatibility limitation, but it is removed from the normal student-facing
path by configuration. If a future product requirement calls for more
than one question per page again, this configuration change would need to
be revisited alongside real root-cause confirmation.

## Reopen condition

Reopen this investigation only if:

- a production requirement exists for more than 1 MCQ per page, or
- the white-screen/unexpected-submit problem occurs even with 1 MCQ per
  page, or
- a real Android/Redmi remote-debugging session provides reproducible
  telemetry (console, network, DOM/CSS snapshots, `visualViewport` state)
  captured at the moment of failure.

Do not reopen based solely on speculation.
