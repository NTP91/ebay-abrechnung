import io
import unittest
from decimal import Decimal

import pandas as pd

import lexoffice_import as subject


class Upload(io.BytesIO):
    def __init__(self, content, name):
        super().__init__(content)
        self.name = name


class Response:
    def __init__(self, status_code, payload=None, text=''):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self):
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return Response(200, {'content': [
            {'id': 'contact-16335', 'roles': {'customer': {'number': 16335}}},
        ]})

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return Response(201, {'id': 'draft-1'})


class ActiveOfferImportTests(unittest.TestCase):
    def test_csv_prices_are_divided_by_three_with_cent_rounding(self):
        upload = Upload('Titel;Preis;Verfügbare Menge;SKU\nArtikel A;10,00;2;SKU-A\nArtikel B;10,01;1;SKU-B\n'.encode(), 'angebote.csv')
        result = subject.read_active_offers(upload)
        self.assertEqual(result['Bestandswert'].tolist(), [Decimal('3.33'), Decimal('3.34')])
        self.assertEqual(result['Angebotspreis'].tolist(), [Decimal('10.00'), Decimal('10.01')])
        self.assertEqual(result['Menge'].tolist(), [2, 1])

    def test_xlsx_and_us_decimal_prices_are_supported(self):
        source = io.BytesIO()
        pd.DataFrame([{'Artikelname': 'Artikel', 'Price': '1,234.56', 'Quantity': '1'}]).to_excel(source, index=False)
        result = subject.read_active_offers(Upload(source.getvalue(), 'offers.xlsx'))
        self.assertEqual(result.iloc[0].Angebotspreis, Decimal('1234.56'))
        self.assertEqual(result.iloc[0].Bestandswert, Decimal('411.52'))

    def test_line_items_use_divided_value_and_optional_sku(self):
        offers = pd.DataFrame([{'Artikelname': 'A', 'SKU': 'SKU-1', 'Menge': 2,
                                'Angebotspreis': Decimal('30.00'), 'Bestandswert': Decimal('10.00')}])
        item = subject.build_active_offer_line_items(offers)[0]
        self.assertEqual(item['quantity'], 2)
        self.assertEqual(item['unitPrice']['netAmount'], 10.0)
        self.assertEqual(item['description'], 'SKU: SKU-1')

    def test_customer_search_and_draft_use_official_api(self):
        http = FakeHttp()
        offers = pd.DataFrame([{'Artikelname': 'A', 'SKU': '', 'Menge': 1,
                                'Angebotspreis': Decimal('30.00'), 'Bestandswert': Decimal('10.00')}])
        result = subject.create_active_offers_draft('secret', offers, http=http)
        self.assertTrue(result.ok)
        self.assertEqual(result.invoice_id, 'draft-1')
        self.assertEqual(http.get_calls[0][0], 'https://api.lexware.io/v1/contacts')
        self.assertEqual(http.get_calls[0][1]['params'], {'number': 16335, 'customer': 'true'})
        url, request = http.post_calls[0]
        self.assertEqual(url, 'https://api.lexware.io/v1/invoices?finalize=false')
        self.assertEqual(request['json']['address'], {'contactId': 'contact-16335'})
        self.assertEqual(request['json']['lineItems'][0]['unitPrice']['netAmount'], 10.0)

    def test_invalid_or_ambiguous_input_is_blocked(self):
        with self.assertRaises(subject.OrderReportError):
            subject.read_active_offers(Upload(b'foo;bar\nx;y\n', 'bad.csv'))
        http = FakeHttp()
        http.get = lambda *args, **kwargs: Response(200, {'content': []})
        result = subject.create_active_offers_draft('secret', pd.DataFrame([{
            'Artikelname': 'A', 'SKU': '', 'Menge': 1,
            'Angebotspreis': Decimal('3'), 'Bestandswert': Decimal('1'),
        }]), http=http)
        self.assertFalse(result.ok)
        self.assertIn('nicht eindeutig', result.message)


if __name__ == '__main__':
    unittest.main()
