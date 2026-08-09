import random
import string

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def _generate_code(User, base):
    base = ''.join(ch for ch in (base or '').upper() if ch.isalnum())[:6] or 'HM'
    while True:
        code = base + ''.join(random.choices(string.digits, k=2))
        if not User.objects.filter(referral_code=code).exists():
            return code


def backfill_referral_codes(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    for user in User.objects.filter(referral_code=''):
        user.referral_code = _generate_code(User, user.first_name or user.email.split('@')[0])
        user.save(update_fields=['referral_code'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0005_backfill_question_entry_permission'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='referral_code',
            field=models.CharField(blank=True, default='', help_text='Auto-generated — this student shares it so friends get a discount and they earn wallet credit.', max_length=20),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='user',
            name='referred_by',
            field=models.ForeignKey(blank=True, help_text='Which student referred this account, if any — set at registration from a referral code.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='referrals', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='user',
            name='wallet_balance',
            field=models.DecimalField(decimal_places=2, default=0, help_text='Reward credit earned from referrals. Accumulates only — not yet redeemable at checkout.', max_digits=9),
        ),
        migrations.RunPython(backfill_referral_codes, noop),
        migrations.AlterField(
            model_name='user',
            name='referral_code',
            field=models.CharField(blank=True, help_text='Auto-generated — this student shares it so friends get a discount and they earn wallet credit.', max_length=20, unique=True),
        ),
    ]
