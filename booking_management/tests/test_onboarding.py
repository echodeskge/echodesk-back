"""Bookings onboarding step: permissions, context (presets, public URL),
prefill, apply (settings incl. encrypted keys, categories/services, staff,
availability), idempotency, seed_defaults."""
import datetime as dt
from decimal import Decimal

from booking_management.emails import booking_site_url
from booking_management.models import BookingSettings, BookingStaff, Service, ServiceCategory, StaffAvailability
from booking_management.onboarding import SERVICE_PRESETS, BookingsStep
from onboarding.models import TenantOnboarding
from onboarding.tests.helpers import enable_features
from users.tests.conftest import EchoDeskTenantTestCase

ONB = '/api/onboarding/'
APPLY = f'{ONB}booking_management/apply/'

HOURS = {
    'mon': {'from': '09:00', 'to': '18:00', 'break_from': '13:00', 'break_to': '14:00'},
    'tue': {'from': '09:00', 'to': '18:00'},
    'wed': {'from': '09:00', 'to': '18:00'},
    'thu': {'from': '09:00', 'to': '18:00'},
    'fri': {'from': '10:00', 'to': '16:00'},
    'sat': None,
    'sun': None,
}


def answers(**overrides):
    base = {
        'timezone': 'Asia/Tbilisi',
        'hours': HOURS,
        'services': [
            {'name': {'en': 'Haircut', 'ka': 'თმის შეჭრა'}, 'duration_minutes': 45, 'price': '40.00',
             'deposit_percentage': 0, 'category': {'en': 'Hair', 'ka': 'თმა'}},
            {'name': {'en': 'Colouring', 'ka': 'თმის შეღებვა'}, 'duration_minutes': 120, 'price': '150.50',
             'deposit_percentage': 20, 'category': {'en': 'Hair', 'ka': 'თმა'}},
            {'name': {'en': '', 'ka': 'მანიკური'}, 'duration_minutes': 60, 'price': '50',
             'category': {'en': 'Nails', 'ka': ''}},
            {'name': {'en': 'Consultation', 'ka': ''}, 'duration_minutes': 30, 'price': '0', 'category': None},
        ],
        'payment': {
            'method': 'manual_transfer', 'allow_cash': True, 'allow_card': False, 'require_deposit': True,
            'bank_name': 'TBC', 'bank_iban': 'GE00TB0000000000000000', 'bank_account_holder': 'Salon LLC',
            'bog_client_id': 'bog-id', 'bog_client_secret': 'bog-secret', 'bog_use_production': False,
        },
        'rules': {'min_hours_before_booking': 3, 'max_days_advance_booking': 30, 'cancellation_hours_before': 12,
                  'refund_policy': 'partial_50', 'auto_confirm': False},
        'reminders': {'reminder_hours_before': 24, 'second_reminder_hours_before': 2, 'sms_enabled': True,
                      'sms_api_key': 'sender-key', 'sms_on_created': True, 'sms_on_confirmed': True,
                      'sms_on_rescheduled': False, 'sms_on_cancelled': True, 'sms_on_reminder': True},
        'public_page': {'enabled': True, 'description': {'en': 'Best salon', 'ka': 'საუკეთესო სალონი'},
                        'address': 'Rustaveli 1', 'phone': '+995555000000'},
    }
    base.update(overrides)
    return base


class BookingsStepTests(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        enable_features(self.tenant, 'booking_management')
        self.owner = self.create_user(
            email='owner@test.com', role='admin', is_staff=True, is_superuser=True,
            first_name='Owner', last_name='One',
        )
        self.nino = self.create_user(email='nino@test.com', first_name='Nino', last_name='K')
        self.agent = self.create_user(email='agent@test.com')

    def _apply(self, data, user=None):
        return self.api_post(APPLY, {'answers': data}, user=user or self.owner)

    def _step(self):
        for step in self.api_get(ONB, user=self.owner).data['steps']:
            if step['key'] == 'booking_management':
                return step
        self.fail('bookings step not listed')

    # ── permissions ──

    def test_non_admin_gets_403(self):
        self.assertEqual(self._apply(answers(), user=self.agent).status_code, 403)

    # ── read ──

    def test_context_shape(self):
        step = self._step()
        self.assertEqual(step['order'], 30)
        ctx = step['context']
        self.assertEqual(set(ctx.keys()), {'users', 'existing', 'service_presets', 'public_url'})
        self.assertEqual(ctx['existing'], {'services': 0, 'staff': 0})
        self.assertEqual(ctx['public_url'], f'{booking_site_url()}/{self.tenant.schema_name}')
        self.assertTrue(ctx['public_url'].startswith('https://book.'))
        self.assertEqual(set(ctx['service_presets'].keys()), set(SERVICE_PRESETS.keys()))
        salon = ctx['service_presets']['salon']
        self.assertEqual(salon[0]['name'], {'en': 'Haircut', 'ka': 'თმის შეჭრა'})
        self.assertEqual(set(salon[0].keys()), {'name', 'duration_minutes', 'price', 'deposit_percentage', 'category'})
        self.assertEqual(ctx['service_presets']['clinic'][0]['name']['en'], 'Consultation')
        self.assertEqual(ctx['service_presets']['services'][0]['name']['en'], 'Consultation')

    def test_prefill_defaults_follow_industry(self):
        state = TenantOnboarding.get()
        state.industry = 'salon'
        state.save()
        prefill = self._step()['prefill']
        self.assertEqual(prefill['timezone'], 'Asia/Tbilisi')
        self.assertEqual(prefill['hours']['mon']['from'], '09:00')
        self.assertIsNone(prefill['hours']['sun'])
        self.assertEqual([s['name']['en'] for s in prefill['services']][:2], ['Haircut', 'Colouring'])
        self.assertEqual(prefill['staff_user_ids'], [])
        self.assertEqual(prefill['payment']['method'], 'manual_transfer')
        self.assertEqual(prefill['payment']['bank_account_holder'], self.tenant.name)
        self.assertIsNone(prefill['payment']['bog_client_secret'])
        self.assertEqual(prefill['rules']['refund_policy'], 'full')
        self.assertTrue(prefill['rules']['auto_confirm'])
        self.assertEqual(prefill['reminders']['reminder_hours_before'], 24)
        self.assertIsNone(prefill['reminders']['sms_api_key'])
        self.assertTrue(prefill['public_page']['enabled'])
        self.assertEqual(prefill['public_page']['description'], {'en': '', 'ka': ''})

    def test_prefill_reflects_applied_data_without_secrets(self):
        self.assertEqual(self._apply(answers(staff_user_ids=[self.nino.id])).status_code, 200)
        prefill = self._step()['prefill']
        self.assertEqual(prefill['staff_user_ids'], [self.nino.id])
        self.assertEqual(prefill['hours']['mon'], HOURS['mon'])
        self.assertEqual(prefill['hours']['fri'], {'from': '10:00', 'to': '16:00', 'break_from': None, 'break_to': None})
        self.assertIsNone(prefill['hours']['sat'])
        names = [s['name']['en'] or s['name']['ka'] for s in prefill['services']]
        self.assertEqual(sorted(names), ['Colouring', 'Consultation', 'Haircut', 'მანიკური'])
        colouring = next(s for s in prefill['services'] if s['name']['en'] == 'Colouring')
        self.assertEqual(colouring['price'], '150.50')
        self.assertEqual(colouring['deposit_percentage'], 20)
        self.assertEqual(colouring['category'], {'en': 'Hair', 'ka': 'თმა'})
        self.assertEqual(prefill['payment']['bank_iban'], 'GE00TB0000000000000000')
        self.assertIsNone(prefill['payment']['bog_client_id'])
        self.assertTrue(prefill['payment']['has_bog_credentials'])
        self.assertIsNone(prefill['reminders']['sms_api_key'])
        self.assertTrue(prefill['reminders']['has_sms_api_key'])
        self.assertFalse(prefill['rules']['auto_confirm'])
        self.assertEqual(prefill['public_page']['description']['ka'], 'საუკეთესო სალონი')
        self.assertNotIn('sender-key', str(prefill))
        self.assertNotIn('bog-secret', str(prefill))

    # ── apply ──

    def test_apply_writes_everything(self):
        resp = self._apply(answers(staff_user_ids=[self.nino.id, self.owner.id]))
        self.assertEqual(resp.status_code, 200, resp.data)
        result = resp.data['result']
        self.assertEqual(result['created']['settings'], 1)
        self.assertEqual(result['created']['staff'], 2)
        self.assertEqual(result['created']['availability'], 14)
        self.assertEqual(result['created']['categories'], 2)
        self.assertEqual(result['created']['services'], 4)
        self.assertIn('bank_iban', result['updated'])
        self.assertIn('sms_api_key', result['updated'])

        s = BookingSettings.objects.get(tenant=self.tenant)
        self.assertEqual(s.timezone, 'Asia/Tbilisi')
        self.assertEqual(s.payment_method, 'manual_transfer')
        self.assertTrue(s.allow_cash_payment)
        self.assertFalse(s.allow_card_payment)
        self.assertTrue(s.require_deposit)
        self.assertEqual((s.bank_name, s.bank_iban, s.bank_account_holder), ('TBC', 'GE00TB0000000000000000', 'Salon LLC'))
        self.assertEqual(s.bog_client_id, 'bog-id')
        self.assertEqual(s.bog_client_secret, 'bog-secret')
        self.assertNotEqual(bytes(s._bog_client_secret_encrypted), b'bog-secret')
        self.assertFalse(s.bog_use_production)
        self.assertEqual(s.min_hours_before_booking, 3)
        self.assertEqual(s.max_days_advance_booking, 30)
        self.assertEqual(s.cancellation_hours_before, 12)
        self.assertEqual(s.refund_policy, 'partial_50')
        self.assertFalse(s.auto_confirm_on_deposit)
        self.assertFalse(s.auto_confirm_on_full_payment)
        self.assertEqual(s.reminder_hours_before, 24)
        self.assertEqual(s.second_reminder_hours_before, 2)
        self.assertTrue(s.sms_enabled)
        self.assertEqual(s.sms_api_key, 'sender-key')
        self.assertNotEqual(bytes(s._sms_api_key_encrypted), b'sender-key')
        self.assertTrue(s.sms_on_created)
        self.assertFalse(s.sms_on_rescheduled)
        self.assertTrue(s.public_page_enabled)
        self.assertEqual(s.public_description, {'en': 'Best salon', 'ka': 'საუკეთესო სალონი'})
        self.assertEqual(s.public_address, 'Rustaveli 1')
        self.assertEqual(s.public_phone, '+995555000000')

        staff = {b.user_id: b for b in BookingStaff.objects.all()}
        self.assertEqual(set(staff.keys()), {self.nino.id, self.owner.id})
        rows = {a.day_of_week: a for a in StaffAvailability.objects.filter(staff=staff[self.nino.id])}
        self.assertEqual(len(rows), 7)
        self.assertEqual((rows[0].start_time, rows[0].end_time), (dt.time(9, 0), dt.time(18, 0)))
        self.assertEqual((rows[0].break_start, rows[0].break_end), (dt.time(13, 0), dt.time(14, 0)))
        self.assertTrue(rows[0].is_available)
        self.assertIsNone(rows[1].break_start)
        self.assertEqual(rows[4].start_time, dt.time(10, 0))
        self.assertFalse(rows[5].is_available)
        self.assertFalse(rows[6].is_available)

        self.assertEqual(sorted(c.name['en'] for c in ServiceCategory.objects.all()), ['Hair', 'Nails'])
        services = {(s.name.get('en') or s.name.get('ka')): s for s in Service.objects.all()}
        self.assertEqual(set(services.keys()), {'Haircut', 'Colouring', 'მანიკური', 'Consultation'})
        colouring = services['Colouring']
        self.assertEqual(colouring.base_price, Decimal('150.50'))
        self.assertEqual(colouring.deposit_percentage, 20)
        self.assertEqual(colouring.duration_minutes, 120)
        self.assertEqual(colouring.status, 'active')
        self.assertEqual(colouring.category.name, {'en': 'Hair', 'ka': 'თმა'})
        self.assertEqual(services['Haircut'].category, colouring.category)
        self.assertEqual(services['მანიკური'].category.name, {'en': 'Nails', 'ka': ''})
        self.assertIsNone(services['Consultation'].category)
        for service in services.values():
            self.assertEqual(set(service.staff_members.values_list('user_id', flat=True)), {self.nino.id, self.owner.id})

    def test_apply_twice_creates_nothing_new_and_keeps_secrets(self):
        data = answers(staff_user_ids=[self.nino.id])
        self.assertEqual(self._apply(data).status_code, 200)
        counts = lambda: (  # noqa: E731
            BookingSettings.objects.count(), BookingStaff.objects.count(), StaffAvailability.objects.count(),
            ServiceCategory.objects.count(), Service.objects.count(),
        )
        before = counts()
        again = answers(staff_user_ids=[self.nino.id])
        again['payment'] = dict(again['payment'], bog_client_id='', bog_client_secret=None)
        again['reminders'] = dict(again['reminders'], sms_api_key=None)
        resp = self._apply(again)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(before, counts())
        self.assertEqual(before, (1, 1, 7, 2, 4))
        self.assertEqual(resp.data['result']['created'], {
            'settings': 0, 'staff': 0, 'availability': 0, 'categories': 0, 'services': 0,
        })
        s = BookingSettings.objects.get(tenant=self.tenant)
        self.assertEqual(s.sms_api_key, 'sender-key')
        self.assertEqual(s.bog_client_secret, 'bog-secret')

    def test_services_match_by_either_language(self):
        Service.objects.create(name={'en': 'haircut', 'ka': 'თმის შეჭრა'}, base_price=10, duration_minutes=30)
        resp = self._apply({'services': [
            {'name': {'en': 'Haircut', 'ka': ''}, 'duration_minutes': 45, 'price': '40.00'},
            {'name': {'en': '', 'ka': 'თმის შეჭრა'}, 'duration_minutes': 45, 'price': '40.00'},
        ]})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(Service.objects.count(), 1)
        self.assertEqual(resp.data['result']['created']['services'], 0)

    def test_partial_answers(self):
        resp = self._apply({'rules': {'auto_confirm': True}, 'hours': HOURS})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIn('Opening hours were not stored: pick at least one staff member.', resp.data['result']['notes'])
        self.assertFalse(StaffAvailability.objects.exists())
        s = BookingSettings.objects.get(tenant=self.tenant)
        self.assertTrue(s.auto_confirm_on_deposit)
        self.assertEqual(s.bank_name, '')

    def test_validation_errors(self):
        resp = self._apply({'timezone': 'Nowhere/Town'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('timezone', resp.data['errors'])
        resp = self._apply({'services': [{'name': {'en': '', 'ka': ''}, 'duration_minutes': 30, 'price': '1'}]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('services', resp.data['errors'])
        resp = self._apply({'services': [{'name': {'en': 'X'}, 'duration_minutes': 30, 'price': '1', 'deposit_percentage': 101}]})
        self.assertEqual(resp.status_code, 400)
        resp = self._apply({'staff_user_ids': [424242]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('staff_user_ids', resp.data['errors'])
        resp = self._apply({'hours': {'mon': {'from': '09:00', 'to': '18:00', 'break_from': '08:00', 'break_to': '09:30'}}})
        self.assertEqual(resp.status_code, 400)
        resp = self._apply({'payment': {'method': 'paypal'}})
        self.assertEqual(resp.status_code, 400)

    # ── seed_defaults ──

    def test_seed_defaults_is_idempotent(self):
        step = BookingsStep()
        step.seed_defaults(self.tenant)
        step.seed_defaults(self.tenant)
        self.assertEqual(BookingSettings.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(BookingSettings.objects.get(tenant=self.tenant).timezone, 'Asia/Tbilisi')
