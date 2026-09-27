# Hand-written (not autodetected as-is): the working tree also contains an
# unrelated, uncommitted "Notification Templates" migration
# (0003_notificationtemplate.py) that this release does NOT include —
# autodetecting against that dirty tree would have bundled its
# NotificationTemplate model creation into this migration too. This
# migration depends directly on the last COMMITTED notifications migration
# (0002) and contains ONLY the one real change this release makes: adding
# 'PURCHASE_CANCELLED' to Notification.event_type's choices (see
# notifications/events.py's _EVENT_DEFINITIONS). No data changes.
#
# NOTE for whoever next runs makemigrations on this app: once the
# Notification Templates work is itself committed, Django will need a
# merge migration for the two independent heads this creates (both
# depending on 0002) — expected and safe, not a sign anything here is wrong.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0002_pushsubscription_pushdeliveryattempt_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='notification',
            name='event_type',
            field=models.CharField(choices=[('USER_REGISTERED', 'User Registered'), ('EMAIL_VERIFIED', 'Email Verified'), ('PASSWORD_CHANGED', 'Password Changed'), ('LOGIN_SECURITY_ALERT', 'Login Security Alert'), ('COURSE_ENROLLED', 'Course Enrolled'), ('COURSE_ACCESS_EXPIRING', 'Course Access Expiring'), ('COURSE_ACCESS_EXPIRED', 'Course Access Expired'), ('PURCHASE_CREATED', 'Purchase Created'), ('PAYMENT_PENDING', 'Payment Pending'), ('PAYMENT_APPROVED', 'Payment Approved'), ('PAYMENT_REJECTED', 'Payment Rejected'), ('PAYMENT_FAILED', 'Payment Failed'), ('PURCHASE_CANCELLED', 'Purchase Cancelled'), ('SUBSCRIPTION_ACTIVATED', 'Subscription Activated'), ('SUBSCRIPTION_EXPIRING', 'Subscription Expiring'), ('SUBSCRIPTION_EXPIRED', 'Subscription Expired'), ('DAILY_TEST_PUBLISHED', 'Daily Test Published'), ('DAILY_TEST_AVAILABLE', 'Daily Test Available'), ('DAILY_TEST_ENDING', 'Daily Test Ending'), ('DAILY_TEST_CLOSED', 'Daily Test Closed'), ('DAILY_TEST_RESULT_AVAILABLE', 'Daily Test Result Available'), ('MOCK_TEST_AVAILABLE', 'Mock Test Available'), ('MOCK_TEST_RESULT_AVAILABLE', 'Mock Test Result Available'), ('GRAND_TEST_SCHEDULED', 'Grand Test Scheduled'), ('GRAND_TEST_REMINDER', 'Grand Test Reminder'), ('GRAND_TEST_STARTING', 'Grand Test Starting'), ('GRAND_TEST_LIVE', 'Grand Test Live'), ('GRAND_TEST_ENDING', 'Grand Test Ending'), ('GRAND_TEST_CLOSED', 'Grand Test Closed'), ('GRAND_TEST_RESULT_AVAILABLE', 'Grand Test Result Available'), ('GRAND_TEST_RANK_AVAILABLE', 'Grand Test Rank Available'), ('NEW_QUESTIONS_AVAILABLE', 'New Questions Available'), ('SMART_PRACTICE_READY', 'Smart Practice Ready'), ('REVISION_DUE', 'Revision Due'), ('WEAK_TOPIC_DETECTED', 'Weak Topic Detected'), ('LEARNING_STREAK', 'Learning Streak'), ('NEW_VIDEO_AVAILABLE', 'New Video Available'), ('NEW_CHAPTER_AVAILABLE', 'New Chapter Available'), ('ANNOUNCEMENT', 'Announcement'), ('MAINTENANCE', 'Maintenance'), ('IMPORTANT_SYSTEM_ALERT', 'Important System Alert')], max_length=40),
        ),
    ]
