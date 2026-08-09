from django.db import migrations, models
import django.db.models.deletion


LEGACY_LABELS = {
    'bank': 'Bank Transfer',
    'esewa': 'eSewa',
    'khalti': 'Khalti',
    'fonepay': 'Fonepay',
    'connectips': 'ConnectIPS',
}


def forwards(apps, schema_editor):
    Purchase = apps.get_model('billing', 'Purchase')
    PaymentMethod = apps.get_model('billing', 'PaymentMethod')
    codes = (
        Purchase.objects.exclude(payment_method_legacy='')
        .values_list('payment_method_legacy', flat=True)
        .distinct()
    )
    for code in codes:
        method, _created = PaymentMethod.objects.get_or_create(
            slug=code,
            defaults={
                'name': LEGACY_LABELS.get(code, code),
                'is_active': True,
                'instructions': "Configure this payment method's QR code and instructions in the Payment Methods admin page.",
            },
        )
        Purchase.objects.filter(payment_method_legacy=code).update(payment_method=method)


def backwards(apps, schema_editor):
    Purchase = apps.get_model('billing', 'Purchase')
    for purchase in Purchase.objects.exclude(payment_method__isnull=True).select_related('payment_method'):
        Purchase.objects.filter(pk=purchase.pk).update(payment_method_legacy=purchase.payment_method.slug)


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0006_paymentmethod'),
    ]

    operations = [
        migrations.RenameField(
            model_name='purchase',
            old_name='payment_method',
            new_name='payment_method_legacy',
        ),
        migrations.AddField(
            model_name='purchase',
            name='payment_method',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='purchases', to='billing.paymentmethod',
                help_text='Which channel the student says they paid through — still manually verified, no live gateway.',
            ),
        ),
        migrations.RunPython(forwards, backwards),
        migrations.RemoveField(
            model_name='purchase',
            name='payment_method_legacy',
        ),
    ]
