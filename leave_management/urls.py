from django.urls import path, include
from rest_framework.routers import DefaultRouter, SimpleRouter

from .views import (
    LeaveSettingsViewSet,
    LeaveTypeViewSet,
    AdminLeaveBalanceViewSet,
    AdminLeaveRequestViewSet,
    PublicHolidayViewSet,
    LeaveApprovalChainViewSet,
    LeaveAuditEventViewSet,
)
from .views_manager import (
    ManagerLeaveRequestViewSet,
    ManagerTeamBalanceViewSet
)
from .views_substitutes import MySubstitutesViewSet, CoveringViewSet
from .views_kpi import KpiGoalViewSet
from .views_attendance import AttendanceViewSet
from .views_payroll import (
    SubstitutionRateViewSet, CoverageViewSet, CompensationViewSet,
    PayrollReportViewSet, MyPayrollViewSet, PayrollRunViewSet,
)
from .views_employee import (
    EmployeeLeaveRequestViewSet,
    EmployeeLeaveBalanceViewSet,
    EmployeePublicHolidayViewSet,
    EmployeeLeaveTypeViewSet
)

# Admin Router - Full administrative access
admin_router = DefaultRouter()
admin_router.register(r'settings', LeaveSettingsViewSet, basename='leave-settings')
admin_router.register(r'leave-types', LeaveTypeViewSet, basename='leave-type')
admin_router.register(r'leave-requests', AdminLeaveRequestViewSet, basename='admin-leave-request')
admin_router.register(r'leave-balances', AdminLeaveBalanceViewSet, basename='admin-leave-balance')
admin_router.register(r'public-holidays', PublicHolidayViewSet, basename='public-holiday')
admin_router.register(r'approval-chains', LeaveApprovalChainViewSet, basename='approval-chain')
admin_router.register(r'audit', LeaveAuditEventViewSet, basename='leave-audit')

# Manager Router - Team management
manager_router = DefaultRouter()
manager_router.register(r'team-requests', ManagerLeaveRequestViewSet, basename='manager-leave-request')
manager_router.register(r'team-balances', ManagerTeamBalanceViewSet, basename='manager-team-balance')
manager_router.register(r'attendance', AttendanceViewSet, basename='manager-attendance')

# Employee Router - Self-service
employee_router = DefaultRouter()
employee_router.register(r'my-requests', EmployeeLeaveRequestViewSet, basename='employee-leave-request')
employee_router.register(r'my-balance', EmployeeLeaveBalanceViewSet, basename='employee-leave-balance')
employee_router.register(r'holidays', EmployeePublicHolidayViewSet, basename='employee-holiday')
employee_router.register(r'leave-types', EmployeeLeaveTypeViewSet, basename='employee-leave-type')
employee_router.register(r'my-substitutes', MySubstitutesViewSet, basename='employee-my-substitutes')
employee_router.register(r'covering', CoveringViewSet, basename='employee-covering')

# KPI goals - /api/leave/kpis/
kpi_router = SimpleRouter()
kpi_router.register(r'kpis', KpiGoalViewSet, basename='kpi-goal')

# Payroll / substitution pay - /api/leave/payroll/
payroll_router = SimpleRouter()
payroll_router.register(r'substitution-rates', SubstitutionRateViewSet, basename='payroll-substitution-rate')
payroll_router.register(r'coverage', CoverageViewSet, basename='payroll-coverage')
payroll_router.register(r'compensations', CompensationViewSet, basename='payroll-compensation')
payroll_router.register(r'report', PayrollReportViewSet, basename='payroll-report')
payroll_router.register(r'my', MyPayrollViewSet, basename='payroll-my')
payroll_router.register(r'runs', PayrollRunViewSet, basename='payroll-run')

urlpatterns = [
    path('payroll/', include(payroll_router.urls)),
    path('', include(kpi_router.urls)),

    # Admin endpoints - /api/leave/admin/
    path('admin/', include(admin_router.urls)),

    # Manager endpoints - /api/leave/manager/
    path('manager/', include(manager_router.urls)),

    # Employee endpoints - /api/leave/employee/
    path('employee/', include(employee_router.urls)),
]
