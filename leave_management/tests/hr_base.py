"""Shared base for the HR feature tests (substitutes, payroll, KPI ...)."""
from unittest.mock import patch

from leave_management.tests.conftest import LeaveTestCase


class HrTestCase(LeaveTestCase):
    """LeaveTestCase with the subscription feature check patched open."""

    def setUp(self):
        super().setUp()
        patcher = patch(
            'leave_management.permissions.HasLeaveManagementFeature.has_permission',
            return_value=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def settings_with(self, **flags):
        from leave_management.models import LeaveSettings
        obj = LeaveSettings.objects.filter(tenant=self.tenant).first()
        if obj is None:
            return self.create_leave_settings(**flags)
        for key, value in flags.items():
            setattr(obj, key, value)
        obj.save()
        return obj
