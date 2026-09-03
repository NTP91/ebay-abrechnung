import io
import hashlib
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook  # Independent read-only verification.

import core
from partner_export import export_partner_excel, prepare_partner_export, report_date
from test_recovery import payout


class PartnerExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = patch.multiple(core, PAYOUTS_DB_PATH=str(Path(self.temp.name) / 'Master_Payouts.csv'),
                                    ORDERS_DB_PATH=str(Path(self.temp.name) / 'Master_Orders.csv'))
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.http = patch('requests.sessions.Session.request', side_effect=AssertionError('HTTP forbidden'))
        self.http.start()
        self.addCleanup(self.http.stop)

    def seed(self, sku='NB / TEST', refund=False):
        order = payout(sku=sku, title='Vollständiger Produkttitel ' * 6)
        order['Verkauft am'] = '25-Aug-26'
        order['Anzahl'] = '7'  # Export quantity is a transaction, not order quantity.
        core.import_reports([order], core.ORDERS_DB_PATH, 'orders')
        sale = payout(sku='WRONG', title='Abgeschnittener Payout-Titel', amount='62,99')
        sale['Auszahlungsdatum'] = '2. Sep 2026'
        sale['Transaktionsbetrag (inkl. Kosten)'] = '69,99'
        frames = [sale]
        if refund:
            credit = payout(transaction='refund1', amount='-9,90', kind='Rückerstattung')
            credit['Auszahlungsdatum'] = '2. Sep 2026'
            credit['Transaktionsbetrag (inkl. Kosten)'] = '-9,90'
            frames.append(credit)
        core.import_reports(frames, core.PAYOUTS_DB_PATH, 'payout')
        return core.load_master_data()

    def test_exact_columns_formats_sources_and_separate_refund(self):
        master = self.seed(refund=True)
        before = master.copy(deep=True)
        data = export_partner_excel(master)
        values = load_workbook(io.BytesIO(data), data_only=True)
        formulas = load_workbook(io.BytesIO(data), data_only=False)
        self.assertEqual(values.sheetnames, ['Rechnung', 'Gutschriften'])
        for sheet in values:
            self.assertEqual(sheet.max_column, 8)
            self.assertEqual(sheet.freeze_panes, 'A11')
            self.assertEqual(sheet['A11'].value, datetime(2026, 8, 25))
            self.assertEqual(sheet['D11'].value, 1)
            self.assertEqual(sheet['B11'].value, 'o1')
            self.assertEqual(sheet['C11'].value, ('Vollständiger Produkttitel ' * 6).strip() + '\nSKU: NB / TEST')
            self.assertTrue(sheet['C11'].alignment.wrap_text)
            self.assertEqual(sheet['A11'].number_format, 'dd"."mm"."yyyy')
            self.assertIn('€', sheet['H11'].number_format)
            self.assertGreaterEqual(sheet.row_dimensions[11].height, 80)
            self.assertEqual(sheet['F10'].value, 'Rabatt 3,5 %')
        sale, credit = values.worksheets
        self.assertEqual(sale['H11'].value, 69.99)
        self.assertEqual(sale['E11'].value, round(62.99 / 1.19, 2))
        self.assertAlmostEqual(sale['G11'].value, 62.99 * .965)
        self.assertAlmostEqual(credit['G11'].value, -9.90 * .965)
        self.assertEqual(formulas['Rechnung']['F11'].value, '=E11*$H$4')
        self.assertEqual(formulas['Rechnung']['G11'].value, '=62.99*(1-$H$4)')
        core.pd.testing.assert_frame_equal(master, before)

    def test_all_rates_and_empty_sheets(self):
        for partner in ('PP', 'BA', 'MK', '001', 'NB', 'MH12', 'OTHER'):
            with self.subTest(partner=partner):
                # Isolate each fixture without replacing any real source file.
                with tempfile.TemporaryDirectory() as folder:
                    with patch.multiple(core, PAYOUTS_DB_PATH=str(Path(folder)/'Master_Payouts.csv'),
                                        ORDERS_DB_PATH=str(Path(folder)/'Master_Orders.csv')):
                        master = self.seed(sku=partner + ' / TEST')
                        model = prepare_partner_export(master)
                        self.assertEqual(model['rate'], Decimal('.005') if partner in ('PP','BA','MK','001') else Decimal('.035'))
                        if partner.startswith('MH'):
                            self.assertEqual(model['partner'], 'MH')
                        wb = load_workbook(io.BytesIO(export_partner_excel(master)), data_only=True)
                        self.assertEqual(wb['Gutschriften']['H18'].value, 0)
                        self.assertIn('Keine Erstattungen', wb['Gutschriften']['C11'].value)

    def test_missing_original_amount_never_substitutes_net_payout(self):
        master = self.seed()
        raw = core.read_master(core.PAYOUTS_DB_PATH).drop(columns=['Transaktionsbetrag (inkl. Kosten)'])
        with self.assertRaisesRegex(ValueError, 'Bruttobetrag fehlt'):
            export_partner_excel(master, payouts=raw)

    def test_refund_uses_original_order_date_and_preserves_negative_net(self):
        master = self.seed(refund=True)
        refunds = master[master.Art == 'Erstattung']
        wb = load_workbook(io.BytesIO(export_partner_excel(refunds)), data_only=True)
        self.assertIn('Keine Rechnungspositionen', wb['Rechnung']['C11'].value)
        self.assertEqual(wb['Gutschriften']['E11'].value, round(-9.90 / 1.19, 2))

    def test_locale_independent_dates(self):
        for text in ('2. Sep 2026', '02-Sep-26', '02.09.2026', '2026-09-02'):
            self.assertEqual(report_date(text), datetime(2026, 9, 2))
        self.assertEqual(report_date('3. Mär 2026'), datetime(2026, 3, 3))
        with self.assertRaises(ValueError):
            report_date('31. Feb 2026')


@unittest.skipUnless(os.environ.get('EBAY_REAL_MASTER_DIR'), 'Set EBAY_REAL_MASTER_DIR for original imported data')
class RealPartnerExportTests(unittest.TestCase):
    def test_all_original_transactions_and_downloads(self):
        source = Path(os.environ['EBAY_REAL_MASTER_DIR'])
        filenames = ['Master_Payouts.csv', 'Master_Orders.csv']
        before = {name: hashlib.sha256((source/name).read_bytes()).hexdigest() for name in filenames}
        with tempfile.TemporaryDirectory() as folder:
            for name in filenames:
                shutil.copyfile(source/name, Path(folder)/name)
            with patch.multiple(core, PAYOUTS_DB_PATH=str(Path(folder)/filenames[0]),
                                ORDERS_DB_PATH=str(Path(folder)/filenames[1])), \
                 patch('requests.sessions.Session.request', side_effect=AssertionError('HTTP forbidden')):
                master = core.load_master_data()
                self.assertEqual(len(master), 50)
                self.assertEqual(len(core.read_master(core.ORDERS_DB_PATH)), 180)
                self.assertAlmostEqual(master['Erlös_Brutto'].sum(), 4427.83)
                scopes = [(partner, '', rows) for partner, rows in master[master.Gruppe == 'Gruppe A'].groupby('Partner')]
                scopes += [(partner, pid, rows) for (partner, pid), rows in
                           master[master.Gruppe == 'Gruppe B'].groupby(['Partner', 'Auszahlung Nr.'])]
                count = 0
                for partner, pid, rows in scopes:
                    with self.subTest(partner=partner, payout=pid):
                        blob = export_partner_excel(rows)
                        book = load_workbook(io.BytesIO(blob), data_only=True)
                        rate = .995 if rows.iloc[0].Gruppe == 'Gruppe A' else .965
                        for name, kind in [('Rechnung', 'Bestellung'), ('Gutschriften', 'Erstattung')]:
                            expected = rows[rows.Art == kind]
                            sheet = book[name]
                            last = 10 + max(1, len(expected))
                            summary = last + 3
                            self.assertEqual(sheet.freeze_panes, 'A11')
                            self.assertEqual(sheet.max_column, 8)
                            for row_index, (_, original) in enumerate(expected.iterrows(), 11):
                                self.assertEqual(sheet[f'D{row_index}'].value, 1)
                                self.assertEqual(sheet[f'E{row_index}'].value, original['eBay_Netto'])
                                self.assertAlmostEqual(sheet[f'G{row_index}'].value, original['Erlös_Brutto'] * rate, places=10)
                                self.assertEqual(sheet[f'C{row_index}'].value, original['Angebotstitel']+'\nSKU: '+original['SKU'])
                                self.assertIsInstance(sheet[f'A{row_index}'].value, datetime)
                            self.assertAlmostEqual(sheet[f'H{summary}'].value, expected['eBay_Netto'].sum(), places=10)
                            self.assertAlmostEqual(sheet[f'H{summary+4}'].value, (expected['Erlös_Brutto'] * rate).sum(), places=10)
                            count += len(expected)
                        output = os.environ.get('PARTNER_TEST_OUTPUT_DIR')
                        if output and (partner == 'BA' or partner in ('NB', 'MH') and pid == '7712804241'):
                            Path(output).mkdir(parents=True, exist_ok=True)
                            filename = f'{partner}_{pid or "7712804241"}.xlsx'
                            (Path(output)/filename).write_bytes(blob)
                self.assertEqual(count, 49)
                from streamlit.testing.v1 import AppTest
                app = AppTest.from_file(str(Path(__file__).with_name('app.py'))).run()
                self.assertFalse(list(app.exception))
                for pid in sorted(master['Auszahlung Nr.'].unique()):
                    next(widget for widget in app.selectbox if widget.label == 'Eine Auszahlung wählen').select(pid).run()
                    self.assertFalse(list(app.exception))
                    self.assertFalse(any('Partnerexport angehalten' in error.value for error in app.error))
        for name in filenames:
            self.assertEqual(hashlib.sha256((source/name).read_bytes()).hexdigest(), before[name])


if __name__ == '__main__':
    unittest.main()
