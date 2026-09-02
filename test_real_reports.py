"""Read-only source fixtures; all imports write to isolated temporary storage.

Run with EBAY_REAL_TEST_DIR pointing to the folder containing the six originals.
No customer data or source files are embedded in this repository.
"""
import os
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch

import pandas as pd
import core

PAYOUT_FILES = [
    'Payout_7712804241_20260902.csv',
    'Payout_7700379513_20260902 (2).csv',
    'Payout_7710027297_20260902.csv',
    'Payout_7700379513_20260902 (1).csv',
]
ORDER_FILES = [
    'eBay-OrdersReport-Sep-02-2026-07_26_20-0700-11331701806.csv',
    'eBay evica 01.08-30.08 (1).xlsx',
]


@unittest.skipUnless(os.environ.get('EBAY_REAL_TEST_DIR'), 'Set EBAY_REAL_TEST_DIR to run original-file tests')
class RealReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.payouts = str(Path(self.temp.name) / 'Master_Payouts.csv')
        self.orders = str(Path(self.temp.name) / 'Master_Orders.csv')
        root = Path(os.environ['EBAY_REAL_TEST_DIR'])
        self.frames = []
        self.order_frames = []
        for name in PAYOUT_FILES:
            with (root / name).open('rb') as file:
                self.frames.append(core.read_report(file))
        for name in ORDER_FILES:
            with (root / name).open('rb') as file:
                self.order_frames.append(core.read_report(file, 'orders'))
        core.import_reports(self.frames, self.payouts, 'payout')
        core.import_reports(self.order_frames, self.orders, 'orders')
        self.paths = patch.multiple(core, PAYOUTS_DB_PATH=self.payouts, ORDERS_DB_PATH=self.orders)
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.master = core.load_master_data()

    def test_exact_payout_counts_and_sums(self):
        raw = core.read_master(self.payouts)
        actual = {
            payout_id: (len(block), sum(block['Betrag abzügl. Kosten'].map(core.parse_money)))
            for payout_id, block in raw.groupby('Auszahlung Nr.')
        }
        self.assertEqual(actual, {
            '7700379513': (2, Decimal('279.90')),
            '7710027297': (14, Decimal('584.67')),
            '7712804241': (34, Decimal('3563.26')),
        })
        self.assertEqual(sum(raw['Betrag abzügl. Kosten'].map(core.parse_money)), Decimal('4427.83'))
        self.assertEqual(len(raw), 50)
        self.assertEqual(set(raw['Auszahlungsstatus']), {'Betrag überwiesen'})

    def test_types_titles_groups_and_fee(self):
        master = self.master
        self.assertEqual(master.Art.value_counts().to_dict(), {'Bestellung': 43, 'Erstattung': 6, 'Gebühr': 1})
        self.assertEqual(((master.Bestellnummer != '') & (master.Angebotstitel != '')).sum(), 49)
        self.assertFalse(master['Prüfhinweis'].astype(bool).any())
        self.assertEqual(set(master[master.SKU.str.startswith('MH')].Partner), {'MH'})
        self.assertEqual(set(master[master.Partner == 'NB'].Gruppe), {'Gruppe B'})
        fee = master[master.Art == 'Gebühr'].iloc[0]
        self.assertEqual((fee.Partner, fee.Bestellnummer, fee['Erlös_Brutto']), ('', '', -1.78))

    def test_repeat_upload_is_byte_identical(self):
        old_payouts, old_orders = Path(self.payouts).read_bytes(), Path(self.orders).read_bytes()
        self.assertEqual(core.import_reports(self.frames, self.payouts, 'payout'), 0)
        self.assertEqual(core.import_reports(self.order_frames, self.orders, 'orders'), 0)
        self.assertEqual(Path(self.payouts).read_bytes(), old_payouts)
        self.assertEqual(Path(self.orders).read_bytes(), old_orders)
        self.assertEqual(len(core.read_master(self.orders)), 180)
        pd.testing.assert_frame_equal(self.frames[1], self.frames[3])

    def test_later_larger_order_report_preserves_records(self):
        alternate = str(Path(self.temp.name) / 'Sequential_Orders.csv')
        self.assertEqual(core.import_reports([self.order_frames[0]], alternate, 'orders'), 30)
        self.assertEqual(core.import_reports([self.order_frames[1]], alternate, 'orders'), 150)
        self.assertEqual(core.import_reports(self.order_frames, alternate, 'orders'), 0)
        self.assertEqual(len(core.read_master(alternate)), 180)

    def test_each_payload_matches_historical_financial_formula(self):
        # Historical reference: f803b06 app.py:152 and lineItems construction.
        # This is NOT a comparison with an actual Lexoffice invoice example.
        total_lines = 0
        for payout_id in self.master['Auszahlung Nr.'].unique():
            expected = self.master[(self.master['Auszahlung Nr.'] == payout_id) &
                                   (self.master.Gruppe == 'Gruppe B') & (self.master.Art == 'Bestellung')]
            payload = core.build_invoice_payload(self.master, payout_id, 'offline-contact', True)
            self.assertEqual(len(payload['lineItems']), len(expected))
            for item, (_, row) in zip(payload['lineItems'], expected.iterrows()):
                self.assertEqual(item['quantity'], 1)
                self.assertEqual(item['discountPercentage'], 0.5)
                self.assertEqual(item['unitPrice']['taxRatePercentage'], 19)
                self.assertEqual(item['unitPrice']['netAmount'], round(row['Erlös_Brutto'] / 1.19, 2))
                self.assertEqual(item['name'], row['Angebotstitel'])
                self.assertIn(row['SKU'], item['description'])
                self.assertIn(row['Bestellnummer'], item['description'])
            total_lines += len(payload['lineItems'])
        self.assertEqual(total_lines, len(self.master[(self.master.Gruppe == 'Gruppe B') & (self.master.Art == 'Bestellung')]))

    def test_refunds_separate_and_sale_refund_pair_preserved(self):
        refunds = core.get_refunds_summary(self.master)
        self.assertEqual(len(refunds), 6)
        self.assertAlmostEqual(refunds.Gutschrift_Brutto.sum(), -465.56)
        self.assertTrue((refunds.Gutschrift_Brutto < 0).all())
        self.assertTrue((refunds.Angebotstitel != '').all())
        for _, row in refunds.iterrows():
            rate = .005 if row.Partner.startswith(('PP', 'BA', 'MK', '001')) else .035
            self.assertAlmostEqual(row.Gutschrift_Netto_Auszahlung, row.Gutschrift_Brutto * (1-rate))
        raw = core.read_master(self.payouts)
        # In these original exports, refunds have no transaction number.
        paired = [block for _, block in raw.groupby('Bestellnummer')
                  if {'Bestellung', 'Rückerstattung'}.issubset(set(block.Typ))]
        self.assertEqual(len(paired), 5, 'Sale/refund pairs remain separate by order')

    def test_streamlit_real_data_and_all_payout_previews(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(Path(__file__).with_name('app.py'))).run()
        self.assertFalse(list(app.exception))
        for payout_id in sorted(self.master['Auszahlung Nr.'].unique()):
            next(w for w in app.selectbox if w.label == 'Eine Auszahlung wählen').select(payout_id).run()
            next(w for w in app.checkbox if 'Geldeingang' in w.label).check().run()
            next(w for w in app.button if w.label == 'Test-Payload vorbereiten').click().run()
            self.assertFalse(list(app.exception))
            self.assertEqual(len(app.json), 1)


if __name__ == '__main__':
    unittest.main()
