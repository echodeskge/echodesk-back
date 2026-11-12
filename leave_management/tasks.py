import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def generate_georgian_holidays():
    """
    For tenants with ``auto_georgian_holidays`` on: load next year's
    holidays (runs on 1 Dec) and make sure the current year is complete
    (1 Jan). Existing dates are skipped.
    """
    from tenant_schemas.utils import schema_context
    from tenants.models import Tenant
    from .holidays_ge import load_georgian_holidays
    from .audit import log_event
    from .models import LeaveSettings

    today = timezone.localdate()
    years = [today.year + 1] if today.month == 12 else [today.year]
    total = 0
    for tenant in Tenant.objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                settings_obj = LeaveSettings.objects.filter(auto_georgian_holidays=True).first()
                if settings_obj is None:
                    continue
                for year in years:
                    created, _ = load_georgian_holidays(settings_obj.tenant, year)
                    if created:
                        total += len(created)
                        log_event(
                            settings_obj.tenant, 'holiday', 'georgian_loaded', object_id=year,
                            data={'year': year, 'created': [str(d) for d in created], 'auto': True},
                        )
        except Exception:
            logger.exception('Georgian holiday generation failed for %s', tenant.schema_name)
    return total


@shared_task
def process_leave_accruals():
    """
    Monthly accrual for accrual-based leave types: for every tenant with
    such types, credit each active user's balance with the months elapsed
    since the last accrual (:func:`leave_management.utils.process_accrual`
    is idempotent — it records ``last_accrual_date``). Scheduled from
    ``CELERY_BEAT_SCHEDULE`` on the 1st of every month; it was never
    scheduled before, so accrual types never accrued.
    """
    from decimal import Decimal

    from django.contrib.auth import get_user_model
    from tenant_schemas.utils import schema_context
    from tenants.models import Tenant
    from .models import LeaveType
    from .utils import process_accrual

    User = get_user_model()
    total = Decimal('0')
    for tenant in Tenant.objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                leave_types = list(LeaveType.objects.filter(
                    calculation_method='accrual', is_active=True, accrual_rate_per_month__gt=0,
                ))
                if not leave_types:
                    continue
                for user in User.objects.filter(is_active=True).order_by('id'):
                    for leave_type in leave_types:
                        total += process_accrual(user, leave_type, leave_type.tenant) or Decimal('0')
        except Exception:
            logger.exception('Leave accrual failed for %s', tenant.schema_name)
    return str(total)
