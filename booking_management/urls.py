from django.urls import path, include
from rest_framework.routers import DefaultRouter

# Import client views
from .views_client import (
    client_register,
    client_login,
    client_verify_email,
    client_password_reset_request,
    client_password_reset_confirm,
    client_profile,
    client_resend_verification,
    client_token_refresh,
    public_info,
    guest_booking_create,
    manage_booking,
    manage_booking_cancel,
    payment_webhook,
    ClientServiceCategoryViewSet,
    ClientServiceViewSet,
    ClientBookingStaffViewSet,
    ClientBookingViewSet,
    ClientRecurringBookingViewSet,
)

# Import admin views
from .views_admin import (
    dashboard_stats,
    staff_schedule,
    booking_settings,
    booking_settings_test_sms,
    AdminServiceCategoryViewSet,
    AdminServiceViewSet,
    AdminBookingStaffViewSet,
    AdminStaffAvailabilityViewSet,
    AdminStaffExceptionViewSet,
    AdminBookingViewSet,
    AdminRecurringBookingViewSet,
    AdminBookingClientViewSet,
)


# ============================================================================
# CLIENT ROUTER - Requires BookingClient JWT authentication
# ============================================================================

client_router = DefaultRouter()
client_router.register(r'categories', ClientServiceCategoryViewSet, basename='client-category')
client_router.register(r'services', ClientServiceViewSet, basename='client-service')
client_router.register(r'staff', ClientBookingStaffViewSet, basename='client-staff')
client_router.register(r'bookings', ClientBookingViewSet, basename='client-booking')
# Customer-managed recurring bookings are not exposed: they are created with
# no availability / lead-time / payment checks. Staff manage them in the admin API.


# ============================================================================
# ADMIN ROUTER - Requires Admin JWT authentication
# ============================================================================

admin_router = DefaultRouter()
admin_router.register(r'categories', AdminServiceCategoryViewSet, basename='admin-category')
admin_router.register(r'services', AdminServiceViewSet, basename='admin-service')
admin_router.register(r'staff', AdminBookingStaffViewSet, basename='admin-staff')
admin_router.register(r'availability', AdminStaffAvailabilityViewSet, basename='admin-availability')
admin_router.register(r'exceptions', AdminStaffExceptionViewSet, basename='admin-exception')
admin_router.register(r'bookings', AdminBookingViewSet, basename='admin-booking')
admin_router.register(r'recurring-bookings', AdminRecurringBookingViewSet, basename='admin-recurring-booking')
admin_router.register(r'clients', AdminBookingClientViewSet, basename='admin-client')


# ============================================================================
# URL PATTERNS
# ============================================================================

app_name = 'booking_management'

urlpatterns = [
    # ========================================================================
    # PUBLIC CLIENT AUTHENTICATION ENDPOINTS
    # ========================================================================
    # These must come before router includes to prevent detail lookup conflicts
    path('clients/register/', client_register, name='client-register'),
    path('clients/login/', client_login, name='client-login'),
    path('clients/verify-email/', client_verify_email, name='client-verify-email'),
    path('clients/password-reset/request/', client_password_reset_request, name='client-password-reset-request'),
    path('clients/password-reset/confirm/', client_password_reset_confirm, name='client-password-reset-confirm'),
    path('clients/profile/', client_profile, name='client-profile'),
    path('clients/resend-verification/', client_resend_verification, name='client-resend-verification'),
    path('clients/token/refresh/', client_token_refresh, name='client-token-refresh'),

    # ========================================================================
    # PAYMENT WEBHOOK (public, called by BOG gateway)
    # ========================================================================
    path('payment-webhook/', payment_webhook, name='payment-webhook'),

    # ========================================================================
    # CLIENT-FACING ENDPOINTS (requires BookingClient JWT)
    # ========================================================================
    path('client/info/', public_info, name='client-info'),
    path('client/guest-bookings/', guest_booking_create, name='client-guest-booking'),
    path('client/manage/<str:token>/', manage_booking, name='client-manage-booking'),
    path('client/manage/<str:token>/cancel/', manage_booking_cancel, name='client-manage-booking-cancel'),
    path('client/', include(client_router.urls)),

    # ========================================================================
    # ADMIN ENDPOINTS (requires Admin JWT)
    # ========================================================================
    path('admin/dashboard/', dashboard_stats, name='admin-dashboard'),
    path('admin/schedule/', staff_schedule, name='admin-schedule'),
    path('admin/settings/', booking_settings, name='admin-settings'),
    path('admin/settings/test-sms/', booking_settings_test_sms, name='admin-settings-test-sms'),
    path('admin/', include(admin_router.urls)),
]
