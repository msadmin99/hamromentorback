"""Free-vs-Pro entitlement checks, shared by the QBank and Test views.

Kept as plain functions (not model methods) since they need to reason across
apps — Subject (academics), Test (tests_app), and Subscription (billing).
"""
from django.db.models import Q
from django.utils import timezone

from .models import GrandTestAccess, Subscription


def _active_subscriptions(user, product_type, course=None):
    qs = Subscription.objects.filter(user=user, product_type=product_type, is_active=True).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gte=timezone.now())
    )
    if course is not None:
        qs = qs.filter(course=course)
    return qs


def has_qbank_access(user, subject):
    if subject.is_free:
        return True
    if not user or not user.is_authenticated:
        return False
    course_ids = list(subject.courses.values_list('id', flat=True))
    if not course_ids:
        return False
    return _active_subscriptions(user, 'qbank').filter(course_id__in=course_ids).exists()


def has_video_access(user, video):
    if video.access_level == 'public':
        return True
    if not user or not user.is_authenticated:
        return False
    if video.access_level == 'registered':
        return True
    if video.access_level == 'teacher_only':
        return user.is_staff
    course_ids = list(video.courses.values_list('id', flat=True))
    if video.access_level == 'course':
        if not course_ids:
            return True
        from courses.models import Enrollment
        return Enrollment.objects.filter(user=user, course_id__in=course_ids, is_active=True).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gte=timezone.now())
        ).exists()
    if video.access_level == 'premium':
        qs = _active_subscriptions(user, 'video')
        if course_ids:
            qs = qs.filter(course_id__in=course_ids)
        return qs.exists()
    return False


def _quota_subscription(user, product_type):
    """First active subscription of this product type that still has quota left."""
    if not user or not user.is_authenticated:
        return None
    for sub in _active_subscriptions(user, product_type):
        if sub.mock_test_quota is None or sub.mock_test_used < sub.mock_test_quota:
            return sub
    return None


def has_mock_test_access(user, test):
    if not test.is_pro:
        return True
    return _quota_subscription(user, 'mock_test') is not None


def has_daily_test_access(user, test):
    if not test.is_pro:
        return True
    return _quota_subscription(user, 'daily_test') is not None


def consume_quota(user, product_type):
    sub = _quota_subscription(user, product_type)
    if sub and sub.mock_test_quota is not None:
        sub.mock_test_used += 1
        sub.save(update_fields=['mock_test_used'])


def get_grand_test_access(user, test):
    if not user or not user.is_authenticated:
        return None
    return GrandTestAccess.objects.filter(user=user, test=test).first()


def is_preview_only(user, test):
    """True if this student can only see a truncated preview of a PRO daily/live test."""
    if test.exam_type != 'daily' or not test.is_pro:
        return False
    if has_daily_test_access(user, test):
        return False
    return test.free_preview_questions > 0
