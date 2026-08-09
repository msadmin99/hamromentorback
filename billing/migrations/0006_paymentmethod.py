from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0005_purchase_payment_method_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='PaymentMethod',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, unique=True)),
                ('slug', models.SlugField(blank=True, unique=True)),
                ('qr_code_image', models.ImageField(blank=True, null=True, upload_to='payment_method_qr/')),
                ('instructions', models.TextField(blank=True, help_text='"How to Pay" steps shown on the QR Payment Page.')),
                ('is_active', models.BooleanField(default=True)),
                ('order', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['order', 'name'],
            },
        ),
    ]
