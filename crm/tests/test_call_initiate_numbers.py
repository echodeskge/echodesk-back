"""initiate_call accepts extensions and feature codes, not only external numbers."""
from django.test import SimpleTestCase

from crm.serializers import CallInitiateSerializer


class TestCallInitiateRecipientNumber(SimpleTestCase):
    def _valid(self, number):
        s = CallInitiateSerializer(data={'recipient_number': number})
        return s.is_valid(), s.errors

    def test_external_numbers_still_accepted(self):
        for number in ['+995 555 12 34 56', '0322421219', '(555) 123-4567', '995598211409']:
            ok, errors = self._valid(number)
            self.assertTrue(ok, (number, errors))

    def test_extensions_accepted(self):
        for number in ['10', '101', '2004', '123456']:
            ok, errors = self._valid(number)
            self.assertTrue(ok, (number, errors))

    def test_feature_codes_accepted(self):
        for number in ['*100', '#99', '*8', '*21*101#']:
            ok, errors = self._valid(number)
            self.assertTrue(ok, (number, errors))

    def test_garbage_rejected(self):
        for number in ['', '1', 'abc', '+', '*', '101abc', '1234567x', '+995 55a 12']:
            ok, _ = self._valid(number)
            self.assertFalse(ok, number)

    def test_value_is_stripped(self):
        s = CallInitiateSerializer(data={'recipient_number': '  101 '})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data['recipient_number'], '101')
