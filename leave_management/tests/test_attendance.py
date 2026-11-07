"""B5: attendance grid."""
from datetime import date
from decimal import Decimal

from leave_management.models import PublicHoliday
from leave_management.tests.hr_base import HrTestCase

URL = '/api/leave/manager/attendance/'


class TestAttendance(HrTestCase):

    def setUp(self):
        super().setUp()
        self.settings_with(attendance_grid_enabled=True)
        self.m = self.create_user(email='att-m@test.com')
        self.a = self.create_user(email='att-a@test.com', manager=self.m, employment_start_date=date(2026, 10, 6))
        self.other = self.create_user(email='att-o@test.com')
        self.lt = self.create_leave_type(code='ATT', color='#ff0000')
        self.create_leave_request(self.a, self.lt, start_date=date(2026, 10, 15), end_date=date(2026, 10, 16),
                                  total_days=Decimal('2'), status='approved')
        PublicHoliday.objects.create(tenant=self.tenant, name={'en': 'Svetitskhovloba'}, date=date(2026, 10, 14))

    def test_grid(self):
        resp = self.api_get(f'{URL}?period=2026-10', user=self.m)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(len(resp.data['days']), 31)
        self.assertEqual([r['user']['id'] for r in resp.data['rows']], [self.a.id])
        cells = resp.data['rows'][0]['cells']
        self.assertEqual(cells['2026-10-01'], 'not_employed')
        self.assertEqual(cells['2026-10-06'], 'worked')
        self.assertEqual(cells['2026-10-10'], 'off')
        self.assertEqual(cells['2026-10-14'], 'off')
        self.assertEqual(cells['2026-10-15'], f'leave:{self.lt.id}')
        self.assertEqual(resp.data['legend'][0]['color'], '#ff0000')
        holiday = next(d for d in resp.data['days'] if d['date'] == '2026-10-14')
        self.assertEqual(holiday['kind'], 'holiday')

    def test_access(self):
        self.assertEqual(self.api_get(f'{URL}?period=2026-10', user=self.a).status_code, 403)
        self.assertEqual(self.api_get(f'{URL}?period=bad', user=self.m).status_code, 400)
        self.settings_with(attendance_grid_enabled=False)
        self.assertEqual(self.api_get(f'{URL}?period=2026-10', user=self.m).status_code, 403)
