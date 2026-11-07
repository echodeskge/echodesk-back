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
