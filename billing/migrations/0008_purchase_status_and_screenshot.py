from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0007_purchase_payment_method_fk'),
    ]

    operations = [
        migrations.AlterField(
            model_name='purchase',
            name='status',
            field=models.CharField(
                choices=[
                    ('unpaid', 'Unpaid'),
                    ('pending', 'Pending Verification'),
                    ('resubmission_requested', 'Resubmission Requested'),
                    ('approved', 'Approved'),
                    ('rejected', 'Rejected'),
                ],
                default='unpaid', max_length=25,
            ),
        ),
        migrations.AlterField(
            model_name='purchase',
            name='admin_note',
            field=models.CharField(
                blank=True, max_length=255,
                help_text='Rejection reason or resubmission-request reason — which one is determined by status.',
            ),
        ),
        migrations.AddField(
            model_name='purchase',
            name='payment_screenshot',
            field=models.ImageField(
                blank=True, null=True, upload_to='payment_screenshots/',
                help_text='Screenshot/photo of the payment receipt, submitted as proof of payment.',
            ),
        ),
    ]
