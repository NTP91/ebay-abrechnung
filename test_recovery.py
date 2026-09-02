"""Synthetic regression checks; not a substitute for the missing original reports."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import core


class Upload(io.BytesIO):
    def __init__(self, data, name='payout.csv'):
        super().__init__(data)
        self.name = name


def payout(payout_id='7700379513', transaction='t1', order='o1', sku='NB / 1', title='Produkt', amount='119,00', kind='Bestellung'):
    return core.canonicalize(pd.DataFrame([{
        'Auszahlung Nr.': payout_id, 'Transaktionsnummer': transaction,
        'Bestellnummer': order, 'SKU': sku, 'Angebotstitel': title,
        'Betrag abzügl. Kosten': amount, 'Typ': kind,
    }]))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.orders = str(Path(self.temp.name) / 'Master_Orders.csv')
        self.payouts = str(Path(self.temp.name) / 'Master_Payouts.csv')
        self.paths = patch.multiple(core, ORDERS_DB_PATH=self.orders, PAYOUTS_DB_PATH=self.payouts)
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def test_csv_metadata_bom_and_amount(self):
        data = 'Hinweis\n\nAuszahlung Nr.;Bestellnummer;Bestandseinheit;Betrag abzügl. Kosten;Angebotstitel\n7700379513;o1;NB / 1;1.234,56;Produkt\n'
        frame = core.read_report(Upload(data.encode('utf-8-sig')))
        self.assertEqual(len(frame), 1)
        self.assertEqual(core.parse_money(frame.iloc[0]['Betrag abzügl. Kosten']), core.Decimal('1234.56'))

    def test_bad_amount_never_becomes_zero(self):
        with self.assertRaises(ValueError):
            core.parse_money('unlesbar')
        self.assertEqual(core.parse_money('0,00'), 0)

    def test_duplicate_payout_and_repeat_leave_bytes_unchanged(self):
        frame = payout()
        self.assertEqual(core.import_reports([frame, frame], self.payouts, 'payout'), 1)
        before = Path(self.payouts).read_bytes()
        self.assertEqual(core.import_reports([frame], self.payouts, 'payout'), 0)
        self.assertEqual(before, Path(self.payouts).read_bytes())

    def test_new_payout_preserves_previous(self):
        core.import_reports([payout()], self.payouts, 'payout')
        core.import_reports([payout('other', 't2')], self.payouts, 'payout')
        self.assertEqual(len(core.read_master(self.payouts)), 2)

    def test_conflicting_payout_is_blocked(self):
        core.import_reports([payout()], self.payouts, 'payout')
        before = Path(self.payouts).read_bytes()
        with self.assertRaises(ValueError):
            core.import_reports([payout(amount='120,00')], self.payouts, 'payout')
        self.assertEqual(before, Path(self.payouts).read_bytes())

    def test_refund_is_not_order_duplicate_and_fee_is_separate(self):
        frame = pd.concat([payout(), payout(amount='-119,00', kind='Erstattung'), payout(transaction='fee', order='', sku='', title='', amount='-1,78', kind='Sonstige eBay-Gebühr')])
        core.import_reports([frame], self.payouts, 'payout')
        master = core.load_master_data()
        self.assertEqual(len(master), 3)
        self.assertEqual(len(core.get_refunds_summary(master)), 1)
        fee = master[master['Art'] == 'Gebühr'].iloc[0]
        self.assertEqual(fee['Partner'], '')
        self.assertEqual(fee['Gruppe'], 'Gebühren')

    def test_orders_multi_item_repeat_and_larger_report(self):
        first = core.canonicalize(pd.DataFrame([{'Bestellnummer': 'o1', 'Transaktionsnummer': 't1', 'Artikelnummer': 'i1', 'SKU': 'NB / 1'}]))
        second = core.canonicalize(pd.DataFrame([{'Bestellnummer': 'o1', 'Transaktionsnummer': 't2', 'Artikelnummer': 'i2', 'SKU': 'MH43 / 2'}]))
        core.import_reports([first], self.orders, 'orders')
        core.import_reports([first, second, first], self.orders, 'orders')
        self.assertEqual(len(core.read_master(self.orders)), 2)

    def test_ambiguous_order_blocks_invoice(self):
        orders = core.canonicalize(pd.DataFrame([
            {'Bestellnummer': 'o1', 'Transaktionsnummer': 'x1', 'Artikelnummer': 'i1', 'SKU': 'NB / 1'},
            {'Bestellnummer': 'o1', 'Transaktionsnummer': 'x2', 'Artikelnummer': 'i2', 'SKU': 'MH / 2'},
        ]))
        core.import_reports([orders], self.orders, 'orders')
        core.import_reports([payout(transaction='', sku='', title='')], self.payouts, 'payout')
        master = core.load_master_data()
        self.assertIn('Mehrdeutig', master.iloc[0]['Prüfhinweis'])
        with self.assertRaises(ValueError):
            core.build_invoice_payload(master, '7700379513', 'contact', True)

    def test_product_priority_net_price_and_quantity_preserved(self):
        core.import_reports([payout()], self.payouts, 'payout')
        master = core.load_master_data()
        item = core.build_invoice_payload(master, '7700379513', 'contact', True)['lineItems'][0]
        self.assertEqual(item['name'], 'Produkt')
        self.assertEqual(item['unitPrice']['netAmount'], 100)
        self.assertEqual(item['quantity'], 1)
        self.assertEqual(item['discountPercentage'], 0.5)
        self.assertIn('SKU: NB / 1', item['description'])
        with self.assertRaises(ValueError):
            core.build_invoice_payload(master, '7700379513', 'contact')

    def test_group_a_mh_and_partial_refund(self):
        frames = [payout(transaction='a', sku='PP / 1'), payout(transaction='b', sku='MH108 / 2'), payout(transaction='c', sku='MH43 / 3', amount='-11,90')]
        core.import_reports(frames, self.payouts, 'payout')
        master = core.load_master_data()
        self.assertEqual(master[master['Gruppe'] == 'Gruppe A'].iloc[0]['Partner'], 'PP')
        self.assertEqual(set(master[master['Gruppe'] == 'Gruppe B']['Partner']), {'MH'})
        refund = core.get_refunds_summary(master).iloc[0]
        self.assertAlmostEqual(refund['Gutschrift_Netto_Auszahlung'], -11.9 * .965)

    def test_xlsx_order_header_and_footer(self):
        buffer = io.BytesIO()
        pd.DataFrame([['Hinweis', '', ''], ['Bestellnummer', 'Artikelnummer', 'Angebotstitel'], ['o1', 'i1', 'Produkt'], ['', '', 'Gesamtsumme']]).to_excel(buffer, index=False, header=False, engine='openpyxl')
        frame = core.read_report(Upload(buffer.getvalue(), 'orders.xlsx'), 'orders')
        self.assertEqual(len(frame), 1)

    def test_title_fallback_by_transaction(self):
        orders = core.canonicalize(pd.DataFrame([{
            'Bestellnummer': 'o1', 'Transaktionsnummer': 't1', 'Artikelnummer': 'i1',
            'SKU': 'MH44 / 1', 'Angebotstitel': 'Vollständiger Produktname',
        }]))
        core.import_reports([orders], self.orders, 'orders')
        core.import_reports([payout(sku='', title='')], self.payouts, 'payout')
        row = core.load_master_data().iloc[0]
        self.assertEqual(row['Partner'], 'MH')
        self.assertEqual(row['Angebotstitel'], 'Vollständiger Produktname')

    def test_payload_isolated_by_payout(self):
        core.import_reports([payout(), payout('other', transaction='t2', amount='238,00')], self.payouts, 'payout')
        payload = core.build_invoice_payload(core.load_master_data(), '7700379513', 'contact', True)
        self.assertEqual(len(payload['lineItems']), 1)
        self.assertEqual(payload['lineItems'][0]['unitPrice']['netAmount'], 100)

    def test_streamlit_with_saved_data_and_payload(self):
        from streamlit.testing.v1 import AppTest
        core.import_reports([payout()], self.payouts, 'payout')
        app = AppTest.from_file(str(Path(__file__).with_name('app.py'))).run()
        self.assertFalse(list(app.exception))
        receipt = next(widget for widget in app.checkbox if 'Geldeingang' in widget.label)
        receipt.check().run()
        next(button for button in app.button if button.label == 'Test-Payload vorbereiten').click().run()
        self.assertFalse(list(app.exception))
        self.assertEqual(len(app.json), 1)


if __name__ == '__main__':
    unittest.main()
