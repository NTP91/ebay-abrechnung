import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'scripts'))
import run_daily_order_sync as job
from ebay_readonly import EbayError


class EbayProviderTest(unittest.TestCase):
    def test_raises_when_credentials_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(EbayError):
                job.ebay_provider()

    def test_returns_config_when_all_env_vars_present(self):
        env = {'EBAY_CLIENT_ID': 'id', 'EBAY_CLIENT_SECRET': 'secret',
               'EBAY_RU_NAME': 'ru', 'EBAY_REFRESH_TOKEN': 'token'}
        with patch.dict(os.environ, env, clear=True):
            config = job.ebay_provider()
        self.assertEqual(config['client_id'], 'id')
        self.assertEqual(config['refresh_token'], 'token')


class DateMappingTest(unittest.TestCase):
    def test_iso_utc_to_german_date(self):
        self.assertEqual(job.to_de_date('2026-09-20T17:09:30.000Z'), '20.09.2026')

    def test_invalid_date_returns_empty(self):
        self.assertEqual(job.to_de_date(''), '')
        self.assertEqual(job.to_de_date(None), '')
        self.assertEqual(job.to_de_date('not-a-date'), '')


class BuildFrameTest(unittest.TestCase):
    def test_maps_order_and_line_item_fields(self):
        orders = [{
            'orderId': '04-15202-75752',
            'creationDate': '2026-09-20T17:09:30.000Z',
            'lineItems': [{
                'lineItemId': '10084620155104',
                'legacyItemId': '820147772955',
                'sku': 'MF',
                'title': "COMFEE' Luftentfeuchter",
                'quantity': 1,
                'lineItemCost': {'value': '126.75', 'currency': 'EUR'},
                'total': {'value': '136.7', 'currency': 'EUR'},
                'deliveryCost': {'shippingCost': {'value': '9.95', 'currency': 'EUR'}},
            }],
        }]
        frame = job.build_frame(orders)
        self.assertEqual(len(frame), 1)
        row = frame.iloc[0]
        self.assertEqual(row['Bestellnummer'], '04-15202-75752')
        self.assertEqual(row['Transaktionsnummer'], '10084620155104')
        self.assertEqual(row['Artikelnummer'], '820147772955')
        self.assertEqual(row['SKU'], 'MF')
        self.assertEqual(row['Angebotstitel'], "COMFEE' Luftentfeuchter")
        self.assertEqual(row['Typ'], 'Bestellung')
        self.assertEqual(row['Verkauft am'], '20.09.2026')
        self.assertEqual(row['Anzahl'], '1')
        self.assertEqual(row['Verkauft für'], '126.75')
        self.assertEqual(row['Verpackung und Versand'], '9.95')
        self.assertEqual(row['Gesamtbetrag'], '136.7')
        # payout-only fields must stay blank for an order row
        self.assertEqual(row['Auszahlung Nr.'], '')
        self.assertEqual(row['Betrag abzügl. Kosten'], '')

    def test_multiple_line_items_stay_separate_positions(self):
        orders = [{
            'orderId': 'o1',
            'creationDate': '2026-09-01T00:00:00.000Z',
            'lineItems': [
                {'lineItemId': 'tx1', 'legacyItemId': 'item1', 'sku': 'A', 'title': 'Eins', 'quantity': 1},
                {'lineItemId': 'tx2', 'legacyItemId': 'item2', 'sku': 'B', 'title': 'Zwei', 'quantity': 1},
            ],
        }]
        frame = job.build_frame(orders)
        self.assertEqual(len(frame), 2)
        self.assertEqual(list(frame['Transaktionsnummer']), ['tx1', 'tx2'])
        self.assertEqual(list(frame['Bestellnummer']), ['o1', 'o1'])
        self.assertTrue(frame['Transaktionsnummer'].is_unique)

    def test_missing_optional_fields_do_not_crash(self):
        orders = [{'orderId': 'o2', 'creationDate': '2026-09-01T00:00:00.000Z',
                    'lineItems': [{'lineItemId': 'tx3'}]}]
        frame = job.build_frame(orders)
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0]['Artikelnummer'], '')
        self.assertEqual(frame.iloc[0]['Verkauft für'], '')


if __name__ == '__main__':
    unittest.main()
