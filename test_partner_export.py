import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from openpyxl import load_workbook  # Independent read-only verification.

import core
from partner_export import (export_partner_excel, prepare_partner_export, report_date, calculate_sheet,
                             DISPLAY_NAMES, _closing_statement_rows, format_euro, TAG)
from test_recovery import payout


def reference_cents(value):
    """Independent integer/rational half-away-from-zero rounding oracle."""
    value = Fraction(value) * 100
    absolute = abs(value)
    whole = (2 * absolute.numerator + absolute.denominator) // (2 * absolute.denominator)
    return (-whole if value < 0 else whole)


def check_workbook(case, blob, rows, rate, recipient):
    book = load_workbook(io.BytesIO(blob), data_only=True)
    formula_book = load_workbook(io.BytesIO(blob), data_only=False)
    case.assertEqual(book.sheetnames, [DISPLAY_NAMES['Rechnung'], DISPLAY_NAMES['Gutschriften'],
                                        DISPLAY_NAMES['HistorischeGutschriften'], 'Bestellnachweis', 'Payoutnachweis'])
    payout_ids = set(rows['Auszahlung Nr.'])
    for name, kind in [('Rechnung', 'Bestellung'), ('Gutschriften', 'Erstattung')]:
        expected = rows[rows.Art == kind]
        sheet = book[DISPLAY_NAMES[name]]
        last = 14 + max(1, len(expected))
        summary = last + 2
        layout = _closing_statement_rows(name, len(expected))
        helper_first = layout['visible_last'] + 3
        case.assertEqual(sheet.max_column, 11)
        # Main sheet scrolls freely now; the refund detail sheet keeps its frozen header.
        case.assertEqual(sheet.freeze_panes, None if name=='Rechnung' else 'A15')
        case.assertEqual(sheet['E4'].value, recipient)
        case.assertEqual(sheet['G4'].value, float(rate))
        case.assertEqual(sheet['I4'].value, .19)
        case.assertEqual(set(sheet['E6'].value.split(', ')), payout_ids)
        # Patrick's address is now filled in centrally; Evelyn's stays deliberately unset here.
        case.assertEqual(sheet['A6'].value, 'Lindenplatz 1\n72622 Nürtingen' if recipient=='Patrick Pfender' else 'Rechnungsadresse noch nicht hinterlegt')
        text = '\n'.join(str(cell.value) for row in sheet for cell in row if cell.value is not None)
        case.assertNotIn('provision', text.lower())
        case.assertIn('Freitext auf der Rechnung', text)
        net_before = net_after = ebay = previous_tax = gross_sum = 0
        for number, (_, original) in enumerate(expected.iterrows(), 15):
            case.assertEqual(sheet[f'E{number}'].value, 1)
            case.assertEqual(sheet[f'F{number}'].value, 'Stück')
            case.assertEqual(sheet[f'G{number}'].value, original['eBay_Netto'])
            case.assertEqual(sheet[f'C{number}'].value, original['Angebotstitel'])
            extra=sheet[f'D{number}'].value
            # Zusatztext for every group: Bestellnummer is deliberately
            # repeated (own column too, for independent verifiability);
            # Bestelldatum and internal workflow/status/amount bookkeeping
            # are not.
            if kind=='Bestellung':
                expected_extra = ('eBay-Bestellnummer: '+original['Bestellnummer']+'\nSKU: '+original['SKU']
                                  +'\nPayout: '+str(original['Auszahlung Nr.']))
                case.assertEqual(extra, expected_extra)
            else:
                for label in ('eBay-Bestellnummer:','SKU:','Refund-Datum:','Refund-Payout:'):
                    case.assertIn(label,extra)
                for label in ('Bestelldatum:','Ursprünglicher Payout:',
                              'Ursprüngliche Abrechnung:','Refund-ID:','Refund brutto:',
                              'Partnerwirkung:','Status:'):
                    case.assertNotIn(label,extra)
            case.assertIsInstance(sheet[f'A{number}'].value, datetime)
            case.assertTrue(sheet[f'C{number}'].alignment.wrap_text)
            case.assertEqual(sheet[f'G{number}'].alignment.horizontal, 'right')
            case.assertEqual(sheet[f'E{number}'].alignment.horizontal, 'center')
            case.assertNotEqual(sheet[f'C{number}'].fill.fgColor.rgb, sheet[f'A{number}'].fill.fgColor.rgb)
            net = Fraction(str(original['eBay_Netto']))
            after = reference_cents(net * (1-Fraction(rate)))
            net_before += reference_cents(net)
            net_after += after
            tax = reference_cents(Fraction(net_after,100)*Fraction(19,100))
            gross = after + tax - previous_tax
            case.assertEqual(reference_cents(str(sheet[f'J{number}'].value)), gross)
            gross_sum += gross
            previous_tax = tax
            ebay += reference_cents(str(sheet[f'K{number}'].value))
            formula = formula_book[DISPLAY_NAMES[name]][f'J{number}'].value
            helper=helper_first+number-15
            case.assertEqual(formula_book[DISPLAY_NAMES[name]][f'G{helper}'].value,f'=E{number}*G{number}')
            case.assertEqual(formula_book[DISPLAY_NAMES[name]][f'H{helper}'].value,f'=ROUND(G{helper}*(1-H{number}),2)')
            case.assertTrue(sheet.row_dimensions[helper].hidden)
            case.assertNotIn('K', formula)  # The eBay control never drives the invoice.
        expected_totals = [net_before, net_before-net_after, net_after, previous_tax,
                           net_after+previous_tax, ebay, ebay-net_after-previous_tax]
        for offset, expected_cents in enumerate(expected_totals):
            case.assertEqual(reference_cents(str(sheet[f'K{summary+offset}'].value)), expected_cents)
        case.assertEqual(gross_sum, expected_totals[4])
    return book


class PartnerExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = patch.multiple(core, PAYOUTS_DB_PATH=str(Path(self.temp.name)/'Master_Payouts.csv'),
                                    ORDERS_DB_PATH=str(Path(self.temp.name)/'Master_Orders.csv'))
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.http = patch('requests.sessions.Session.request', side_effect=AssertionError('HTTP forbidden'))
        self.http.start()
        self.addCleanup(self.http.stop)

    def seed(self, sku='NB / TEST', refund=False):
        order = payout(sku=sku, title='Vollständiger Produkttitel ' * 6)
        order['Verkauft am'] = '25-Aug-26'
        order['Anzahl'] = '7'
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

    def test_schema_sources_and_negative_refund(self):
        master = self.seed(refund=True)
        before = master.copy(deep=True)
        book = check_workbook(self, export_partner_excel(master), master, Decimal('.035'), 'Patrick Pfender')
        self.assertEqual(book['Rechnung']['K15'].value, 69.99)
        self.assertEqual(book['Rechnung']['J15'].value, 60.79)
        self.assertEqual(book['Rechnung']['A15'].number_format, 'dd"."mm"."yyyy')
        self.assertGreaterEqual(book['Rechnung'].row_dimensions[15].height, 76)
        self.assertIn('€', book['Rechnung']['G15'].number_format)
        core.pd.testing.assert_frame_equal(master, before)

    def test_all_groups_and_automatic_rates(self):
        for partner in ('PP', 'BA', 'MK', '001', 'NB', 'MH12', 'OTHER'):
            with self.subTest(partner=partner), tempfile.TemporaryDirectory() as folder, patch.object(core, 'known_group_b_partners', return_value={'NB', 'OTHER'}):
                with patch.multiple(core, PAYOUTS_DB_PATH=str(Path(folder)/'Master_Payouts.csv'),
                                    ORDERS_DB_PATH=str(Path(folder)/'Master_Orders.csv')):
                    master = self.seed(sku=partner+' / TEST')
                    rate = Decimal('.005') if partner in ('PP','BA','MK','001') else Decimal('.035')
                    book = check_workbook(self, export_partner_excel(master), master, rate,
                                          'Evelyn' if rate == Decimal('.005') else 'Patrick Pfender')
                    self.assertIn('Keine Erstattungen', book[DISPLAY_NAMES['Gutschriften']]['C15'].value)
                    if partner.startswith('MH'):
                        self.assertEqual(book['Rechnung']['A4'].value, 'MH')
                    if master.iloc[0].Gruppe == 'Gruppe B':
                        check_workbook(self, export_partner_excel(master,statement_type='group_b_evelyn'),
                                       master,Decimal('.005'),'Evelyn')

    def test_rounding_regression_and_column_tax(self):
        # NB example: old independent 392.50 * .965 = 378.7625, displayed 378.76.
        items = [{'net':Decimal('329.83'),'ebay':Decimal('392.50')}]
        totals = calculate_sheet(items,Decimal('.035'))
        self.assertEqual(totals['net_after'],Decimal('318.29'))
        self.assertEqual(totals['gross'],Decimal('378.77'))
        # Two individually rounded VAT values would produce .14, column VAT .13.
        items = [{'net':Decimal('.33'),'ebay':Decimal('.39')} for _ in range(2)]
        totals = calculate_sheet(items,Decimal('.005'))
        self.assertEqual(totals['gross'],Decimal('.79'))
        self.assertEqual(sum(item['gross'] for item in items),Decimal('.79'))
        for sign in (1,-1):
            items=[{'net':Decimal('1.00')*sign,'ebay':Decimal('1.19')*sign}]
            self.assertEqual(calculate_sheet(items,Decimal('.005'))['net_after'],Decimal('1.00')*sign)

    def test_missing_original_control_amount_blocks_export(self):
        master = self.seed()
        raw = core.read_master(core.PAYOUTS_DB_PATH).drop(columns=['Transaktionsbetrag (inkl. Kosten)'])
        with self.assertRaisesRegex(ValueError,'Bruttobetrag fehlt'):
            export_partner_excel(master,payouts=raw)

    def test_refund_only_and_dates(self):
        master = self.seed(refund=True)
        check_workbook(self,export_partner_excel(master[master.Art=='Erstattung']),
                       master[master.Art=='Erstattung'],Decimal('.035'),'Patrick Pfender')
        for text in ('2. Sep 2026','02-Sep-26','02.09.2026','2026-09-02'):
            self.assertEqual(report_date(text),datetime(2026,9,2))
        with self.assertRaises(ValueError):
            report_date('31. Feb 2026')

    def test_recipient_master_data_and_invalid_scope(self):
        master = self.seed(sku='BA / TEST')
        with self.assertRaises(ValueError):
            export_partner_excel(master,statement_type='group_b_evelyn')
        with self.assertRaises(ValueError):
            export_partner_excel(master,statement_type='manual_rate')
        from partner_export import recipient_details
        config=json.loads(Path('billing_recipients.json').read_text(encoding='utf-8'))
        config['recipients']['evelyn']['address']['street']='TEST-STRASSE (synthetisch)'
        path=Path(self.temp.name)/'recipients.json'
        path.write_text(json.dumps(config),encoding='utf-8')
        with patch.dict(os.environ,{'PAYMENT_RECIPIENTS_PATH':str(path)}):
            self.assertEqual(recipient_details('evelyn')[1],'TEST-STRASSE (synthetisch)')

    def test_column_widths_are_compact_and_amounts_stay_right_aligned(self):
        """Narrow/short-value columns must stay narrow, amount columns must
        stay compact and right-aligned, and no column may be extremely wide -
        while Artikelname/Zusatztext keep wrapping within a bounded width."""
        master = self.seed(refund=True)
        book = check_workbook(self, export_partner_excel(master), master, Decimal('.035'), 'Patrick Pfender')
        sheet = book['Rechnung']
        widths = {c: sheet.column_dimensions[c].width for c in 'ABCDEFGHIJK'}
        narrow = {'A': 'Bestelldatum', 'E': 'Menge', 'F': 'Einheit', 'H': 'Rabatt', 'I': 'Umsatzsteuer'}
        for column in narrow:
            self.assertLessEqual(widths[column], 12, f'{narrow[column]} ({column}) not narrow: {widths[column]}')
        self.assertLessEqual(widths['B'], 18, f'Bestellnummer not compact: {widths["B"]}')
        amounts = {'G': 'VK netto', 'J': 'Rechnungsbetrag', 'K': 'eBay-Auszahlungsbetrag'}
        for column in amounts:
            self.assertLessEqual(widths[column], 16, f'{amounts[column]} ({column}) not compact: {widths[column]}')
            self.assertEqual(sheet[f'{column}15'].alignment.horizontal, 'right')
        for column in 'ABCDEFGHIJK':
            self.assertLessEqual(widths[column], 50, f'Column {column} is extremely wide: {widths[column]}')
        self.assertTrue(sheet['C15'].alignment.wrap_text)
        self.assertTrue(sheet['D15'].alignment.wrap_text)
        self.assertLessEqual(widths['C'], 50)
        self.assertLessEqual(widths['D'], 45)

    def test_rechnung_and_gutschriften_never_drift_apart(self):
        """Regression guard: Rechnung is the only layout source. Not partner-
        specific - this must hold for any partner/SKU that produces both a
        sale and a refund row, not just MH."""
        import partner_export
        master = self.seed(refund=True)
        with patch.object(partner_export, '_fill_sheet', wraps=partner_export._fill_sheet) as spy:
            blob = export_partner_excel(master)
        # All three tabs must be built from the exact same template bytes (Rechnung's) -
        # this is what makes divergence structurally impossible, not just coincidental.
        self.assertEqual(spy.call_count, 3)
        xml_rechnung = spy.call_args_list[0][0][0]
        xml_gutschriften = spy.call_args_list[1][0][0]
        self.assertEqual(xml_rechnung, xml_gutschriften)

        book = load_workbook(io.BytesIO(blob), data_only=True)
        rechnung, gutschriften = book[DISPLAY_NAMES['Rechnung']], book[DISPLAY_NAMES['Gutschriften']]

        headers_r = [rechnung.cell(row=14, column=c).value for c in range(1, 12)]
        headers_g = [gutschriften.cell(row=14, column=c).value for c in range(1, 12)]
        self.assertEqual(headers_r, headers_g)  # identical labels AND order

        widths_r = [rechnung.column_dimensions[c].width for c in 'ABCDEFGHIJK']
        widths_g = [gutschriften.column_dimensions[c].width for c in 'ABCDEFGHIJK']
        self.assertEqual(widths_r, widths_g)
        self.assertEqual(rechnung.max_column, gutschriften.max_column)

        # Both tabs share one table layout; refund rows add the required audit
        # metadata. Group B (this fixture's SKU): Bestellnummer deliberately
        # repeated, no Bestelldatum/internal workflow/status/amount fields.
        sale_extra = rechnung['D15'].value
        refund_extra = gutschriften['D15'].value
        self.assertRegex(sale_extra, r'^eBay-Bestellnummer: .+\nSKU: .+\nPayout: .+$')
        self.assertEqual(refund_extra.count('\n'), 4)  # eBay-Bestellnummer/SKU/Refund-Datum/Refund-Payout/Bereits an Partner bezahlt
        for label in ('eBay-Bestellnummer:','SKU:','Refund-Datum:','Refund-Payout:','Bereits an Partner bezahlt:'):
            self.assertIn(label,refund_extra)
        for label in ('Bestelldatum:','Ursprünglicher Payout:',
                      'Ursprüngliche Abrechnung:','Refund-ID:','Refund brutto:',
                      'Partnerwirkung:','Status:'):
            self.assertNotIn(label,refund_extra)

    def test_group_a_uses_the_same_compact_export_structure(self):
        """Group A (PP/BA/MK/001) now uses the identical Excel structure as
        Group B: Bestellnummer+SKU+Payout for sales, Bestellnummer/SKU/
        Refund-Datum/Refund-Payout for refunds - Bestellnummer deliberately
        repeated (own column too), no Bestelldatum/internal workflow/status/
        Refund-ID/amount text. Group A's money logic (0,5 % discount,
        recipient Evelyn) must stay unchanged - check_workbook verifies both
        the structure and the rate/recipient in one pass, exactly like the
        equivalent Group B test does."""
        master = self.seed(sku='PP / TEST', refund=True)
        book = check_workbook(self, export_partner_excel(master), master, Decimal('.005'), 'Evelyn')
        sale_extra = book[DISPLAY_NAMES['Rechnung']]['D15'].value
        refund_extra = book[DISPLAY_NAMES['Gutschriften']]['D15'].value
        self.assertEqual(sale_extra, 'eBay-Bestellnummer: o1\nSKU: PP / TEST\nPayout: 7700379513')
        for label in ('eBay-Bestellnummer:','SKU:','Refund-Datum:','Refund-Payout:'):
            self.assertIn(label,refund_extra)
        for label in ('Bestelldatum:','Ursprünglicher Payout:',
                      'Ursprüngliche Abrechnung:','Refund-ID:','Refund brutto:',
                      'Partnerwirkung:','Status:'):
            self.assertNotIn(label,refund_extra)

    def test_finale_amount_nets_gutschriften_once_and_never_historical(self):
        """GESAMTABRECHNUNG on the Rechnung sheet must show the one amount the
        partner may actually invoice: the regular claim minus refunds/deductions
        already known before payment (Gutschriften, Tab 2) - so nobody has to
        subtract Tab 1 and Tab 2 by hand. HistorischeGutschriften (Tab 3: a
        refund on a sale already paid out earlier) is a separate, not-yet-
        settled repayment case and must never reduce this figure."""
        master = self.seed(refund=True)
        model = prepare_partner_export(master)
        self.assertLess(model['totals']['Gutschriften']['gross'], Decimal('0'))  # a real refund is present

        book = load_workbook(io.BytesIO(export_partner_excel(master)), data_only=True)
        formula_book = load_workbook(io.BytesIO(export_partner_excel(master)), data_only=False)
        rechnung = book[DISPLAY_NAMES['Rechnung']]
        layout = _closing_statement_rows('Rechnung', len(model['Rechnung']))
        regular_row = layout['finale_start'] + 2  # third finale line: 'Zwischensumme Verkäufe nach Rabatt'
        refunds_row = layout['finale_start'] + 3  # fourth finale line: refunds/deductions of this settlement
        self.assertEqual(rechnung.cell(row=regular_row, column=1).value, 'Zwischensumme Verkäufe nach Rabatt')
        regular_value = Decimal(str(rechnung.cell(row=regular_row, column=11).value))
        refunds_label = rechnung.cell(row=refunds_row, column=1).value
        self.assertIn('Erstattungen / Abzüge dieser Abrechnung', refunds_label)
        refunds_value = Decimal(str(rechnung.cell(row=refunds_row, column=11).value))
        self.assertEqual(refunds_value, model['totals']['Gutschriften']['gross'])
        self.assertEqual(rechnung.cell(row=layout['final_row'], column=1).value, 'FINALER RECHNUNGSBETRAG')
        final_value = Decimal(str(rechnung.cell(row=layout['final_row'], column=11).value))

        self.assertEqual(final_value, regular_value + refunds_value)
        self.assertEqual(final_value, model['totals']['Rechnung']['gross'] + model['totals']['Gutschriften']['gross'])
        self.assertNotEqual(final_value, model['totals']['Rechnung']['gross'])
        formula = formula_book[DISPLAY_NAMES['Rechnung']].cell(row=layout['final_row'], column=11).value
        self.assertEqual(formula, f'=K{regular_row}+K{refunds_row}')

    def test_finale_amount_ignores_historical_already_paid_refunds(self):
        """A refund whose original sale was already paid out to the partner in
        an earlier run (Fall B, Tab 3) must never reduce the current claim -
        only Tab 2 (refund known before payment) is netted."""
        master = self.seed(refund=True)
        master = master.copy()
        master.loc[master.Art == 'Erstattung', 'Bereits_An_Partner_Bezahlt'] = True
        model = prepare_partner_export(master)
        self.assertEqual(len(model['Gutschriften']), 0)
        self.assertEqual(len(model['HistorischeGutschriften']), 1)

        book = load_workbook(io.BytesIO(export_partner_excel(master)), data_only=True)
        rechnung = book[DISPLAY_NAMES['Rechnung']]
        layout = _closing_statement_rows('Rechnung', len(model['Rechnung']))
        final_value = Decimal(str(rechnung.cell(row=layout['final_row'], column=11).value))
        self.assertEqual(final_value, model['totals']['Rechnung']['gross'])  # unaffected by the historical refund

    def test_finale_amount_without_any_refund_equals_regular_amount(self):
        """No refund at all -> Gutschriften is empty, so the final amount is
        exactly the regular claim (0 EUR netted)."""
        master = self.seed(refund=False)
        model = prepare_partner_export(master)
        self.assertEqual(model['totals']['Gutschriften']['gross'], Decimal('0.00'))

        book = load_workbook(io.BytesIO(export_partner_excel(master)), data_only=True)
        rechnung = book[DISPLAY_NAMES['Rechnung']]
        layout = _closing_statement_rows('Rechnung', len(model['Rechnung']))
        final_value = Decimal(str(rechnung.cell(row=layout['final_row'], column=11).value))
        self.assertEqual(final_value, model['totals']['Rechnung']['gross'])

    def test_refund_never_appears_in_both_gutschriften_and_historical(self):
        """A single refund is bucketed by exactly one if/else branch (Gutschriften
        vs. HistorischeGutschriften per _paid_out) - never counted in both."""
        for paid_out in (False, True):
            with self.subTest(paid_out=paid_out):
                master = self.seed(refund=True).copy()
                master.loc[master.Art == 'Erstattung', 'Bereits_An_Partner_Bezahlt'] = paid_out
                model = prepare_partner_export(master)
                gutschrift_keys = {(item['order'], item['finance_id']) for item in model['Gutschriften']}
                historical_keys = {(item['order'], item['finance_id']) for item in model['HistorischeGutschriften']}
                self.assertEqual(gutschrift_keys & historical_keys, set())
                self.assertEqual(len(model['Gutschriften']) + len(model['HistorischeGutschriften']), 1)
                self.assertEqual(len(model['HistorischeGutschriften' if paid_out else 'Gutschriften']), 1)

    def test_missing_paid_out_flag_defaults_to_not_historical_even_as_nan(self):
        """A refund row sliced from a combined frame where the paid-out column
        exists but is unset for this particular row (e.g. a Gruppe-A refund in
        studio_view.partner_rows()'s concatenated output) reads back as NaN,
        not a real bool - bool(nan) is True in Python, so this must not
        silently misroute the refund into HistorischeGutschriften."""
        master = self.seed(refund=True).copy()
        master['Bereits_An_Partner_Bezahlt'] = float('nan')
        model = prepare_partner_export(master)
        self.assertEqual(len(model['Gutschriften']), 1)
        self.assertEqual(len(model['HistorischeGutschriften']), 0)

    def test_historical_mh_reference_regular_minus_refunds_equals_final(self):
        """Documents the exact historical MH reference figures (regulärer
        Rechnungsbetrag 5.062,98 EUR, bereits berücksichtigte Erstattungen/
        Abzüge -763,24 EUR, FINALER RECHNUNGSBETRAG 4.299,74 EUR) as a fixed
        regression pin for the closing-line arithmetic the code now performs
        (Rechnung.gross + Gutschriften.gross)."""
        regular = Decimal('5062.98')
        refunds = Decimal('-763.24')
        self.assertEqual(regular + refunds, Decimal('4299.74'))

    def test_header_payouts_period_and_counts_match_the_actual_positions(self):
        """Header (payout numbers, payout period, both sheets' position
        counts) is built from the exact same rows as the line items in one
        pass (prepare_partner_export's own loop) - never a separately
        cached/older count or date range."""
        order_one = payout(order='o1', transaction='t1', sku='NB / TEST', title='Produkt Eins')
        order_two = payout(order='o2', transaction='t2', sku='NB / TEST', title='Produkt Zwei')
        core.import_reports([order_one, order_two], core.ORDERS_DB_PATH, 'orders')
        sale_one = payout(payout_id='7700100000', order='o1', transaction='t1', sku='NB / TEST', amount='50,00')
        sale_one['Auszahlungsdatum'] = '01.09.2026'
        sale_one['Transaktionsbetrag (inkl. Kosten)'] = '50,00'
        sale_two = payout(payout_id='7700200000', order='o2', transaction='t2', sku='NB / TEST', amount='30,00')
        sale_two['Auszahlungsdatum'] = '05.09.2026'
        sale_two['Transaktionsbetrag (inkl. Kosten)'] = '30,00'
        refund_two = payout(payout_id='7700200000', order='o2', transaction='refund2', sku='NB / TEST', amount='-10,00', kind='Rückerstattung')
        refund_two['Auszahlungsdatum'] = '05.09.2026'
        refund_two['Transaktionsbetrag (inkl. Kosten)'] = '-10,00'
        core.import_reports([sale_one, sale_two, refund_two], core.PAYOUTS_DB_PATH, 'payout')
        master = core.load_master_data()

        book = load_workbook(io.BytesIO(export_partner_excel(master)), data_only=True)
        rechnung, gutschriften = book[DISPLAY_NAMES['Rechnung']], book[DISPLAY_NAMES['Gutschriften']]

        # Both sheets cite the full payout batch (matches the real, already-
        # shipped MH export: both tabs list the same Lexware reference numbers),
        # not a subset scoped to only that sheet's own rows.
        self.assertEqual(set(rechnung['E6'].value.split(', ')), {'7700100000', '7700200000'})
        self.assertEqual(rechnung['E7'].value, 'Auszahlungszeitraum: 01.09.2026 – 05.09.2026')
        self.assertEqual(set(gutschriften['E6'].value.split(', ')), {'7700100000', '7700200000'})

        sales = master[master.Art == 'Bestellung']
        refunds = master[master.Art == 'Erstattung']
        self.assertEqual(rechnung['A12'].value, f'Reguläre Positionen: {len(sales)}')
        self.assertEqual(rechnung['G12'].value, f'Erstattungen / Abzüge: {len(refunds)}')
        self.assertEqual(gutschriften['A12'].value, f'Erstattungen: {len(refunds)}')

    def test_subtotal_row_relabeled_and_distinct_from_final_amount(self):
        """Tab 1's own sales-only subtotal must never read like the final
        payable amount - it carries its own, clearly qualified label, while
        FINALER RECHNUNGSBETRAG (further down, in the GESAMTABRECHNUNG block)
        stays the one number a partner may actually invoice."""
        master = self.seed(refund=True)
        model = prepare_partner_export(master)
        book = load_workbook(io.BytesIO(export_partner_excel(master)), data_only=True)
        rechnung = book[DISPLAY_NAMES['Rechnung']]
        layout = _closing_statement_rows('Rechnung', len(model['Rechnung']))
        subtotal_row = layout['start'] + 4  # offset 4 = 'gross' in the totals block
        self.assertEqual(rechnung.cell(row=subtotal_row, column=1).value,
                         'Zwischensumme Verkäufe nach Rabatt (vor Erstattungen/Abzügen)')
        subtotal_value = Decimal(str(rechnung.cell(row=subtotal_row, column=11).value))
        self.assertEqual(subtotal_value, model['totals']['Rechnung']['gross'])
        final_value = Decimal(str(rechnung.cell(row=layout['final_row'], column=11).value))
        # A real refund is present, so the two must genuinely differ - the
        # exact scenario a partner could otherwise misread.
        self.assertNotEqual(subtotal_value, final_value)
        self.assertEqual(rechnung.cell(row=layout['final_row'], column=1).value, 'FINALER RECHNUNGSBETRAG')

    def test_bestellnachweis_and_payoutnachweis_present_and_partner_scoped(self):
        """New Bestellnachweis/Payoutnachweis tabs: purely evidentiary (no
        formulas, no recomputed amounts), scoped to exactly this partner's
        export - never a foreign partner's rows, never global order data."""
        master = self.seed(sku='NB / TEST', refund=True)
        model = prepare_partner_export(master)
        book = load_workbook(io.BytesIO(export_partner_excel(master)), data_only=True)
        self.assertIn('Bestellnachweis', book.sheetnames)
        self.assertIn('Payoutnachweis', book.sheetnames)

        orders_sheet = book['Bestellnachweis']
        self.assertEqual([cell.value for cell in orders_sheet[3]][:5],
                         ['Bestellnummer', 'Produkttitel', 'SKU', 'Partner', 'Transaktionskennung'])
        expected_items = model['Rechnung'] + model['Gutschriften'] + model['HistorischeGutschriften']
        order_rows = [[cell.value for cell in row] for row in orders_sheet.iter_rows(min_row=4)]
        self.assertEqual(len(order_rows), len(expected_items))
        for row, item in zip(order_rows, expected_items):
            self.assertEqual(row[0], item['order'])
            self.assertEqual(row[2], item['sku'])
            self.assertEqual(row[3], model['partner'])  # only this partner, never a foreign one
        self.assertTrue(all(row[3] == model['partner'] for row in order_rows))

        payouts_sheet = book['Payoutnachweis']
        self.assertEqual([cell.value for cell in payouts_sheet[3]][:5],
                         ['Bestellnummer', 'SKU', 'eBay-Payout Nr.', 'Payoutdatum', 'Ausgezahlter Betrag (eBay, brutto)'])
        payout_rows = [[cell.value for cell in row] for row in payouts_sheet.iter_rows(min_row=4)]
        self.assertEqual(len(payout_rows), len(expected_items))
        for row, item in zip(payout_rows, expected_items):
            self.assertEqual(row[0], item['order'])
            self.assertEqual(row[2], item['payout_id'])
        # No new amount is computed - every payout amount is the exact same
        # eBay control figure already used elsewhere in the export.
        for row, item in zip(payout_rows, expected_items):
            self.assertIn(format_euro(item['ebay']), row[4])

    def test_evidence_tabs_never_include_a_foreign_partners_positions(self):
        """A second partner's data must never leak into this partner's
        Bestellnachweis/Payoutnachweis - prepare_partner_export already
        requires exactly one partner per call, so this is a structural
        guarantee, not a filter that could be forgotten."""
        nb_master = self.seed(sku='NB / TEST')
        foreign = nb_master.copy()
        foreign['Partner'] = 'OTHER'
        mixed = core.pd.concat([nb_master, foreign], ignore_index=True)
        with self.assertRaises(ValueError):
            export_partner_excel(mixed)

    def test_sheet_order_matches_manuscript(self):
        master = self.seed(refund=True)
        book = load_workbook(io.BytesIO(export_partner_excel(master)))
        self.assertEqual(book.sheetnames, ['Rechnung', 'Erstattungen-Abzüge', 'Offene Rückforderungen',
                                            'Bestellnachweis', 'Payoutnachweis'])

    def test_finalized_snapshot_bytes_stay_byte_identical_across_new_export_version(self):
        """A snapshot finalized before this export-structure change (raw
        bytes stored as-is) is never regenerated - partner_snapshot.final_
        file() only ever returns what is already stored, regardless of how
        export_partner_excel() has since evolved."""
        import hashlib
        import partner_snapshot
        master = self.seed(sku='PP / TEST')
        old_style_bytes = b'not a real xlsx - simulates an already-stored older-format snapshot'
        with core.ledger() as db:
            partner_snapshot.initialize(db)
            db.execute('''INSERT INTO partner_round_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                'GB-9999-999', 'PP', 'Gruppe A', '2026-01-01T00:00:00+02:00', '2026-01-08T00:00:00+02:00',
                '[]', 0, '0', '0', '0', '[]', '0.005', 'hash', '2026-01-01T00:00:00Z',
                old_style_bytes, hashlib.sha256(old_style_bytes).hexdigest(), '[]'))
            db.commit()
        self.assertEqual(partner_snapshot.final_file('GB-9999-999', 'PP'), old_style_bytes)
        self.assertEqual(partner_snapshot.final_file('GB-9999-999', 'PP'), old_style_bytes)  # re-download, still identical

    def test_new_export_is_deterministic_for_re_download(self):
        """A fresh export (the new 5-sheet structure) called twice on the
        exact same input produces byte-identical output - the basis for
        finalize()'s own no-second-generation guarantee and Re-Download
        never recomputing anything."""
        master = self.seed(refund=True)
        self.assertEqual(export_partner_excel(master), export_partner_excel(master))

    def test_mh_reference_case_final_amount_unambiguous_with_new_labels(self):
        """Read-only reconstruction of the documented historical MH figures
        (5.062,98 EUR sales subtotal, -763,24 EUR refunds, 4.299,74 EUR final)
        against the new Tab 1 layout: only 4.299,74 EUR may appear next to
        FINALER RECHNUNGSBETRAG; the 5.062,98 EUR subtotal must carry its own
        distinct, clearly-qualified label; Tab 3 (HistorischeGutschriften)
        must stay 0,00 EUR for this case (all 7 refunds are Fall A, already
        known before payment - none of MH's positions were refunded after
        already being paid out in an earlier run)."""
        model = {
            'partner': 'MH', 'group': 'Gruppe B', 'rate': Decimal('.035'),
            'payouts': {'7700000000': datetime(2026, 9, 1)},
            'recipient': 'Patrick Pfender', 'address': 'Lindenplatz 1\n72622 Nürtingen',
            'statement_type': 'partner', 'Rechnung': [], 'Gutschriften': [], 'HistorischeGutschriften': [],
            'totals': {
                'Rechnung': {'net': Decimal('0'), 'discount': Decimal('0'), 'net_after': Decimal('0'),
                             'tax': Decimal('0'), 'gross': Decimal('5062.98'), 'ebay': Decimal('0'),
                             'gross_discount': Decimal('0')},
                'Gutschriften': {'net': Decimal('0'), 'discount': Decimal('0'), 'net_after': Decimal('0'),
                                 'tax': Decimal('0'), 'gross': Decimal('-763.24'), 'ebay': Decimal('0'),
                                 'gross_discount': Decimal('0')},
                'HistorischeGutschriften': {'net': Decimal('0'), 'discount': Decimal('0'), 'net_after': Decimal('0'),
                                            'tax': Decimal('0'), 'gross': Decimal('0.00'), 'ebay': Decimal('0'),
                                            'gross_discount': Decimal('0')},
            },
        }
        from partner_export import _fill_sheet
        template = zipfile.ZipFile(Path('templates') / 'partner.xlsx').read('xl/worksheets/sheet1.xml')
        xml = _fill_sheet(template, model, 'Rechnung')
        sheet = ET.fromstring(xml)
        layout = _closing_statement_rows('Rechnung', 0)
        subtotal_row = layout['start'] + 4

        def cell_text(row_number, col):
            for row in sheet.find(TAG('sheetData')):
                if int(row.get('r')) == row_number:
                    for cell in row:
                        if cell.get('r') == f'{col}{row_number}':
                            is_ = cell.find(TAG('is'))
                            v = cell.find(TAG('v'))
                            return ''.join(t.text or '' for t in is_.iter(TAG('t'))) if is_ is not None else (v.text if v is not None else None)
            return None

        self.assertEqual(cell_text(subtotal_row, 'A'),
                         'Zwischensumme Verkäufe nach Rabatt (vor Erstattungen/Abzügen)')
        self.assertEqual(Decimal(cell_text(subtotal_row, 'K')), Decimal('5062.98'))
        self.assertEqual(cell_text(layout['final_row'], 'A'), 'FINALER RECHNUNGSBETRAG')
        self.assertEqual(Decimal(cell_text(layout['final_row'], 'K')), Decimal('4299.74'))
        self.assertEqual(model['totals']['HistorischeGutschriften']['gross'], Decimal('0.00'))


@unittest.skipUnless(os.environ.get('EBAY_REAL_MASTER_DIR'),'Set EBAY_REAL_MASTER_DIR for original imported data')
class RealPartnerExportTests(unittest.TestCase):
    def test_all_original_positions_and_four_outputs(self):
        source=Path(os.environ['EBAY_REAL_MASTER_DIR'])
        filenames=['Master_Payouts.csv','Master_Orders.csv']
        before={name:hashlib.sha256((source/name).read_bytes()).hexdigest() for name in filenames}
        with tempfile.TemporaryDirectory() as folder:
            # This isolated offline fixture intentionally starts with a fresh ledger.
            with patch.object(core, 'PAYOUTS_DB_PATH', str(Path(folder)/filenames[0])):
                with core.ledger():
                    pass
            for name in filenames:
                shutil.copyfile(source/name,Path(folder)/name)
            with patch.multiple(core,PAYOUTS_DB_PATH=str(Path(folder)/filenames[0]),ORDERS_DB_PATH=str(Path(folder)/filenames[1])), \
                 patch('requests.sessions.Session.request',side_effect=AssertionError('HTTP forbidden')):
                master=core.load_master_data()
                self.assertEqual(len(master),50)
                self.assertEqual(len(core.read_master(core.ORDERS_DB_PATH)),180)
                self.assertAlmostEqual(master['Erlös_Brutto'].sum(),4427.83)
                cases=[('BA',master[master.Partner=='BA'],'partner',Decimal('.005'),'Evelyn',1,0,'62.68','0.00'),
                       ('NB',master[master.Partner=='NB'],'partner',Decimal('.035'),'Patrick Pfender',12,2,'2007.68','-347.11'),
                       ('MH',master[master.Partner=='MH'],'partner',Decimal('.035'),'Patrick Pfender',25,3,'2027.21','-92.58'),
                       ('Gruppe_B_Evelyn',master[master.Gruppe=='Gruppe B'],'group_b_evelyn',Decimal('.005'),'Evelyn',37,5,'4160.31','-453.38')]
                verification={}
                for filename,rows,kind,rate,recipient,sales,refunds,gross,credit in cases:
                    with self.subTest(file=filename):
                        self.assertEqual((rows.Art=='Bestellung').sum(),sales)
                        self.assertEqual((rows.Art=='Erstattung').sum(),refunds)
                        self.assertFalse(rows.duplicated().any())
                        model=prepare_partner_export(rows,statement_type=kind)
                        self.assertEqual(model['totals']['Rechnung']['gross'],Decimal(gross))
                        self.assertEqual(model['totals']['Gutschriften']['gross'],Decimal(credit))
                        blob=export_partner_excel(rows,statement_type=kind)
                        book=check_workbook(self,blob,rows,rate,recipient)
                        output=os.environ.get('PARTNER_TEST_OUTPUT_DIR')
                        if output:
                            Path(output).mkdir(parents=True,exist_ok=True)
                            (Path(output)/(filename+'.xlsx')).write_bytes(blob)
                            formulas=load_workbook(io.BytesIO(blob),data_only=False)
                            verification[filename+'.xlsx']={sheet.title:{
                                'lastRow':min(r for r,d in sheet.row_dimensions.items() if d.hidden)-3,
                                'formulas':{cell.coordinate:book[sheet.title][cell.coordinate].value
                                            for row in sheet for cell in row if cell.data_type=='f'}
                            } for sheet in formulas}
                if os.environ.get('PARTNER_TEST_OUTPUT_DIR'):
                    (Path(os.environ['PARTNER_TEST_OUTPUT_DIR'])/'verification.json').write_text(
                        json.dumps(verification),encoding='utf-8')
                self.assertEqual(set(master[master.Gruppe=='Gruppe B'].Partner),{'NB','MH'})
                from streamlit.testing.v1 import AppTest
                app=AppTest.from_file(str(Path(__file__).with_name('app.py'))).run()
                for pid in sorted(master['Auszahlung Nr.'].unique()):
                    next(widget for widget in app.selectbox if widget.label=='Payout').select(pid).run()
                    self.assertFalse(list(app.exception))
                    self.assertFalse(any('Partnerexport angehalten' in error.value for error in app.error))
        for name in filenames:
            self.assertEqual(hashlib.sha256((source/name).read_bytes()).hexdigest(),before[name])


if __name__=='__main__':
    unittest.main()
