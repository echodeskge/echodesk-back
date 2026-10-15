"""
Tests for POST /api/leave/employee/my-requests/preview/ — the dry-run
check the mobile form runs before submitting a leave request.
"""
from decimal import Decimal
from datetime import date, timedelta
from unittest.mock import patch

from rest_framework import status

from leave_management.models import LeaveRequest, LeaveBalance
from leave_management.tests.conftest import LeaveTestCase

PREVIEW_URL = '/api/leave/employee/my-requests/preview/'


def _patch_feature(test_method):
    """Decorator that patches tenant_has_feature to always return True."""
    @patch('leave_management.permissions.HasLeaveManagementFeature.has_permission', return_value=True)
    def wrapper(self, mock_perm, *args, **kwargs):
        return test_method(self, *args, **kwargs)
    wrapper.__name__ = test_method.__name__
    wrapper.__doc__ = test_method.__doc__
    return wrapper


def _next_monday(after_days=7):
    """A Monday at least ``after_days`` days out, so Mon-Wed is 3 working days."""
    d = date.today() + timedelta(days=after_days)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d


class TestLeavePreview(LeaveTestCase):

    def setUp(self):
        super().setUp()
        self.create_leave_settings()
        self.leave_type = self.create_leave_type()
        self.employee = self.create_user(email='pv-emp@test.com', role='agent')
        self.monday = _next_monday()
        self.wednesday = self.monday + timedelta(days=2)

    def _preview(self, user=None, **overrides):
        body = {
            'leave_type': self.leave_type.id,
            'start_date': self.monday.isoformat(),
            'end_date': self.wednesday.isoformat(),
        }
        body.update(overrides)
        return self.api_post(PREVIEW_URL, body, user=user or self.employee)

    @_patch_feature
    def test_happy_path(self):
        resp = self._preview()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['allowed'])
        self.assertEqual(resp.data['message'], '')
        self.assertEqual(Decimal(resp.data['total_days']), Decimal('3'))
        # The balance is allocated lazily from the type's yearly allowance
        self.assertEqual(Decimal(resp.data['available_days']), Decimal('20'))

    @_patch_feature
    def test_preview_creates_no_request(self):
        before = LeaveRequest.objects.count()
        self._preview()
        self.assertEqual(LeaveRequest.objects.count(), before)

    @_patch_feature
    def test_overlap_is_refused(self):
        self.create_leave_request(
            self.employee, self.leave_type,
            start_date=self.monday, end_date=self.monday,
        )
        resp = self._preview()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data['allowed'])
        self.assertIn('overlapping', resp.data['message'])
        self.assertEqual(Decimal(resp.data['total_days']), Decimal('3'))

    @_patch_feature
    def test_insufficient_balance_is_refused(self):
        self.create_leave_balance(
            self.employee, self.leave_type,
            year=self.monday.year, allocated_days=Decimal('1'),
        )
        resp = self._preview()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data['allowed'])
        self.assertIn('Insufficient balance', resp.data['message'])
        self.assertEqual(Decimal(resp.data['available_days']), Decimal('1'))

    @_patch_feature
    def test_end_before_start_is_refused(self):
        resp = self._preview(
            start_date=self.wednesday.isoformat(),
            end_date=self.monday.isoformat(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data['allowed'])
        self.assertEqual(Decimal(resp.data['total_days']), Decimal('0'))
        self.assertIn('End date', resp.data['message'])

    @_patch_feature
    def test_weekend_only_range_has_no_working_days(self):
        saturday = self.monday + timedelta(days=5)
        sunday = saturday + timedelta(days=1)
        resp = self._preview(
            start_date=saturday.isoformat(), end_date=sunday.isoformat()
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data['allowed'])
        self.assertIn('no working days', resp.data['message'])

    @_patch_feature
    def test_inactive_leave_type_is_rejected(self):
        inactive = self.create_leave_type(is_active=False)
        resp = self._preview(leave_type=inactive.id)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('leave_type', resp.data)

    @_patch_feature
    def test_missing_fields_are_rejected(self):
        resp = self.api_post(
            PREVIEW_URL, {'leave_type': self.leave_type.id}, user=self.employee
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthenticated_is_401(self):
        resp = self.api_post(PREVIEW_URL, {})
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
