# "Clean order list": reversible soft delete on orders.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('ecommerce_crm', '0046_order_item_detach_deleted_products'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='is_deleted',
            field=models.BooleanField(db_index=True, default=False, help_text='Hidden from the order list, statistics and the storefront (reversible)'),
        ),
        migrations.AddField(
            model_name='order',
            name='deleted_at',
            field=models.DateTimeField(blank=True, help_text='When the order was hidden', null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='deleted_by',
            field=models.ForeignKey(blank=True, help_text='Staff user who hid the order', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
    ]
