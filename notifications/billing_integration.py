"""Billing domain-event integration (Phase 2) — PAYMENT_APPROVED /
SUBSCRIPTION_ACTIVATED, the two events the acceptance contract names.

billing/payment_service.py's own `activate()` already places its existing
`send_payment_notification(purchase.user, 'payment_approved', purchase)`
call OUTSIDE/AFTER the `with transaction.atomic():` block (confirmed by
reading the real function, Phase 2 precheck) — this module's entry point
is called from that exact same place, immediately alongside it. Not a
replacement: the existing email/SMS-style notification keeps running
unchanged; this is a second, additive in-app notification.

`transaction.on_commit` is used anyway (belt-and-suspenders): `activate()`
is itself sometimes called from within an outer atomic block by its own
callers (e.g. a bulk-approve admin action), and on_commit is always
correct regardless of nesting depth, while a plain call after `with
transaction.atomic():` is only correct when that's the outermost block.
"""
from django.db import transaction

from . import services, templates


def _course_and_item_for_purchase(purchase):
    """Best-effort (course, item_label) for a Purchase, covering every
    real `Purchase.kind` (see billing/models.py: Purchase.KIND_CHOICES).
    Never guesses a course when a purchase genuinely spans more than one
    (combo/grand_test_package) or has none (teacher_course, a marketplace
    concept unrelated to courses.Course) — `course=None` in that case,
    matching create_notification's own `course=None` default rather than
    inventing a fake one."""
    if purchase.kind == 'subscription' and purchase.plan_id:
        return purchase.plan.course, purchase.plan.name
    if purchase.kind == 'grand_test' and purchase.grand_test_id:
        return None, purchase.grand_test.title
    if purchase.kind == 'combo' and purchase.combo_plan_id:
        return purchase.combo_plan.course, purchase.combo_plan.name
    if purchase.kind == 'grand_test_package' and purchase.grand_test_package_id:
        return None, purchase.grand_test_package.name
    if purchase.kind == 'teacher_course' and purchase.teacher_course_id:
        return None, getattr(purchase.teacher_course, 'title', 'your course')
    return None, 'your purchase'


def notify_payment_approved(purchase):
    """Called once, additively, from billing.payment_service.activate() —
    the single real "a purchase was approved" moment in this codebase.
    Fires the generic PAYMENT_APPROVED event for every kind, plus
    SUBSCRIPTION_ACTIVATED specifically for a course subscription/combo
    purchase (the two events the Phase 2 acceptance contract names)."""
    course, item = _course_and_item_for_purchase(purchase)

    def _notify():
        title, body = templates.render('PAYMENT_APPROVED', {'item': item})
        services.create_notification(
            purchase.user, 'PAYMENT_APPROVED', title, body,
            course=course, purchase=purchase, action_url='/billing/history',
            dedupe_key=f'purchase:{purchase.id}:payment_approved',
        )
        if purchase.kind in ('subscription', 'combo') and course is not None:
            title, body = templates.render('SUBSCRIPTION_ACTIVATED', {'course': course.name})
            services.create_notification(
                purchase.user, 'SUBSCRIPTION_ACTIVATED', title, body,
                course=course, purchase=purchase, action_url='/billing/history',
                dedupe_key=f'purchase:{purchase.id}:subscription_activated',
            )

    transaction.on_commit(_notify)
