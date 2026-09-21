import csv
import io
import json
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch
import core
import partner_invoices as incoming
import position_workflow as workflow
from test_recovery import payout
from test_invoice_support import invoice_csv


class PartnerInvoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        paths=patch.multiple(core,PAYOUTS_DB_PATH=str(self.root/'Master_Payouts.csv'),ORDERS_DB_PATH=str(self.root/'Master_Orders.csv'))
        paths.start();self.addCleanup(paths.stop)
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('HTTP forbidden'))
        network.start();self.addCleanup(network.stop)
        frames=[payout('7700000001','t1','o1',sku='MH43 / A',title='Vollständiger Artikel A'),payout('7700000002','t2','o2',sku='MH44 / B',title='Vollständiger Artikel B'),payout('7700000003','t3','o3',sku='NB / C')]
        for frame in frames:
            frame['Transaktionsbetrag (inkl. Kosten)']=frame['Betrag abzügl. Kosten']
            frame['Auszahlungsdatum']='03.09.2026';frame['Auszahlungsstatus']='Betrag überwiesen'
        core.import_reports(frames,core.ORDERS_DB_PATH,'orders');core.import_reports(frames,core.PAYOUTS_DB_PATH,'payout')
        rows=workflow.positions();self.rows=rows[rows.Partner=='MH']
        self.expected=incoming.expected_statement(self.rows)

    def content(self, change=None):
        blob=invoice_csv(self.expected,'MH-2026-1')
        if change is None:return blob
        rows=list(csv.DictReader(io.StringIO(blob.decode('utf-8-sig')),delimiter=';'))
        change(rows)
        output=io.StringIO();writer=csv.DictWriter(output,fieldnames=rows[0].keys(),delimiter=';');writer.writeheader();writer.writerows(rows)
        return output.getvalue().encode('utf-8-sig')

    def upload(self,blob=None,name='invoice.csv'):
        return incoming.upload('MH',name,blob or self.content())[0]

    def test_exact_csv_approval_and_audit(self):
        record=self.upload()
        self.assertEqual(record['report']['status'],'matched',record['report'])
        self.assertFalse(workflow.positions().reviewed_at.astype(bool).any())
        incoming.approve(record['id'],'Patrick Test')
        saved=incoming.list_invoices('MH')[0]
        self.assertEqual(saved['approval_mode'],'automatic_match')
        self.assertEqual(saved['approved_by'],'Patrick Test')
        self.assertTrue(workflow.positions().query("Partner == 'MH'").reviewed_at.astype(bool).all())
        with self.assertRaises(ValueError):incoming.approve(record['id'],'Patrick Test')

    def test_no_invoice_no_review_and_no_payment(self):
        key=self.rows.iloc[0].position_key
        with self.assertRaisesRegex(ValueError,'beleglose'):workflow.confirm([key],'review',date.today())
        with self.assertRaisesRegex(ValueError,'Zuerst'):workflow.confirm([key],'partner_paid',date.today())

    def test_wrong_sku_amount_quantity_discount_and_total_are_red(self):
        cases={'SKU':'MH99 / WRONG','Menge':'2','Netto vor Rabatt':'104.00','Rabatt %':'0.5','Positionsbetrag brutto':'120.00','Gesamtbetrag brutto':'999.00'}
        for field,value in cases.items():
            with self.subTest(field=field):
                blob=self.content(lambda rows:rows[0].update({field:value,'Rechnungsnummer':'BAD-'+field}))
                record=self.upload(blob)
                self.assertEqual(record['report']['status'],'deviation',record['report'])
                with self.assertRaises(ValueError): incoming.approve(record['id'],'Tester','Dokument geprüft und freigegeben',True)

    def test_invoice_number_is_required_for_automatic_match(self):
        record=self.upload(self.content(lambda rows:[row.update({'Rechnungsnummer':''}) for row in rows]))
        self.assertEqual(record['report']['status'],'manual_required')
        self.assertTrue(any('Rechnungsnummer' in warning for warning in record['report']['warnings']))

    def test_payout_number_is_checked_when_invoice_contains_it(self):
        def content(number, wrong=False):
            source=list(csv.DictReader(io.StringIO(self.content().decode('utf-8-sig')),delimiter=';'))
            fields=list(source[0])+['Payout-Nummer']
            output=io.StringIO();writer=csv.DictWriter(output,fieldnames=fields,delimiter=';');writer.writeheader()
            payouts={item['order']:item['payout'] for item in self.expected['items']}
            for row in source:
                row['Rechnungsnummer']=number
                row['Payout-Nummer']='9999999999' if wrong else payouts[row['Bestellnummer']]
                writer.writerow(row)
            return output.getvalue().encode('utf-8-sig')
        self.assertEqual(self.upload(content('PAYOUT-OK'))['report']['status'],'matched')
        wrong=self.upload(content('PAYOUT-BAD',True))
        self.assertEqual(wrong['report']['status'],'deviation')
        self.assertTrue(any('Payout-Nummer stimmt nicht' in error for error in wrong['report']['errors']))

    def test_missing_and_additional_positions_are_red(self):
        missing=self.upload(self.content(lambda rows:rows.pop()))
        self.assertTrue(any('fehlt auf der Rechnung' in error for error in missing['report']['errors']))
        extra=self.upload(self.content(lambda rows:rows.append(dict(rows[0],Bestellnummer='unknown',Rechnungsnummer='extra'))))
        self.assertEqual(extra['report']['status'],'deviation')

    def test_line_rounding_cannot_hide_inconsistent_invoice_total(self):
        from decimal import Decimal
        record=self.upload(self.content(lambda rows:[row.update({'Positionsbetrag brutto':str(Decimal(row['Positionsbetrag brutto'])+Decimal('.01'))}) for row in rows]))
        self.assertEqual(record['report']['status'],'deviation')
        self.assertTrue(any('Summe der Rechnungspositionen' in error for error in record['report']['errors']))

    def test_open_order_without_payout_is_not_billable(self):
        opened=payout('','open','open-order',sku='MH / OPEN')
        core.import_reports([opened],core.ORDERS_DB_PATH,'orders')
        core.import_reports([opened],core.PAYOUTS_DB_PATH,'payout')
        record=self.upload(self.content(lambda rows:rows[0].update({'Bestellnummer':'open-order','SKU':''})))
        self.assertEqual(record['report']['status'],'deviation')
        self.assertTrue(any('kein abrechenbarer Payout' in error for error in record['report']['errors']))

    def test_wrong_partner_selection_cannot_pass(self):
        record,_=incoming.upload('NB','invoice.csv',self.content())
        self.assertEqual(record['report']['status'],'deviation')
        self.assertTrue(any('anderen Partner' in message for message in record['report']['errors']))

    def test_missing_fields_only_allow_explicit_reasoned_override(self):
        record=self.upload(self.content(lambda rows:rows[0].update({'Menge':''})))
        self.assertEqual(record['report']['status'],'manual_required')
        with self.assertRaises(ValueError):incoming.approve(record['id'],'Tester')
        with self.assertRaises(ValueError):incoming.approve(record['id'],'Tester','Zu kurz',True)
        incoming.approve(record['id'],'Tester','Menge im Originalbeleg für beide Positionen als 1 geprüft.',True)
        saved=incoming.list_invoices()[0]
        self.assertEqual(saved['approval_mode'],'manual_override')
        self.assertTrue(saved['override_reason'])

    def test_hash_number_and_position_duplicates_remain_locked(self):
        first=self.upload()
        same,duplicate=incoming.upload('MH','renamed.csv',self.content())
        self.assertTrue(duplicate);self.assertEqual(same['id'],first['id'])
        changed=self.upload(self.content(lambda rows:rows[0].update({'Rechnungsdatum':'04.09.2026'})))
        self.assertEqual(changed['report']['status'],'deviation')
        second=self.upload(self.content(lambda rows:[r.update({'Rechnungsnummer':'MH-2026-2'}) for r in rows]))
        self.assertEqual(second['report']['status'],'matched')
        incoming.approve(first['id'],'Tester')
        with self.assertRaisesRegex(ValueError,'bereits in Rechnung'):incoming.approve(second['id'],'Tester')

    def test_original_tampering_and_changed_source_prevent_approval(self):
        record=self.upload()
        path=self.root/'Partner_Invoices'/record['file_ref'];original=path.read_bytes()
        self.assertEqual(incoming.stored_original(record),path)
        path.write_bytes(b'changed')
        self.assertIsNone(incoming.stored_original(record))
        with self.assertRaisesRegex(ValueError,'Originalrechnung'):incoming.approve(record['id'],'Tester')
        path.write_bytes(original)
        orders=core.read_master(core.ORDERS_DB_PATH);orders.loc[0,'Angebotstitel']='Veränderter Titel'
        orders.to_csv(core.ORDERS_DB_PATH,sep=';',index=False,encoding='utf-8-sig')
        with self.assertRaisesRegex(ValueError,'verändert'):incoming.approve(record['id'],'Tester')

    def test_backup_restore_retains_invoice_and_allocations(self):
        record=self.upload();incoming.approve(record['id'],'Tester')
        blob=core.backup_data()
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            self.assertIn('Partner_Invoices/'+record['file_ref'],archive.namelist())
            self.assertIn('Settlement_Partner_Invoices.json',archive.namelist())
        (self.root/'Settlement_State.sqlite3').unlink()
        self.assertTrue(incoming.list_invoices()[0]['approved_at'])
        with core.ledger() as db:self.assertEqual(db.execute('SELECT count(*) FROM partner_invoice_positions').fetchone()[0],2)
        with self.assertRaises(ValueError):incoming.approve(record['id'],'Tester')

    def test_xlsx_structured_invoice_matches(self):
        from openpyxl import Workbook
        workbook=Workbook()
        for row in csv.reader(io.StringIO(self.content().decode('utf-8-sig')),delimiter=';'):workbook.active.append(row)
        output=io.BytesIO();workbook.save(output)
        record=self.upload(output.getvalue(),'invoice.xlsx')
        self.assertEqual(record['report']['status'],'matched',record['report'])

    def test_existing_partner_workbook_with_empty_credit_sheet_requires_invoice_number(self):
        """Bestellnachweis/Payoutnachweis are visible by design (Mini-Fix
        "Sichtbarkeit ändern"), so invoice_parser.extract() - unchanged,
        scans every visible sheet - now also sees each position restated on
        those two evidence tabs. Re-uploading our own full export as if it
        were a partner invoice therefore correctly reports every position as
        a duplicate ('deviation'), never silently accepting an ambiguous
        re-upload as 'matched'/'manual_required'."""
        from partner_export import export_partner_excel
        record=self.upload(export_partner_excel(self.rows),'partner.xlsx')
        self.assertEqual(record['report']['status'],'deviation',record['report'])
        self.assertTrue(all('doppelt enthalten' in error for error in record['report']['errors']))
        self.assertEqual(record['extracted']['payouts'],['7700000001','7700000002'])

    def test_pdf_table_matches_and_textless_pdf_stays_manual(self):
        from reportlab.platypus import SimpleDocTemplate,Table,TableStyle
        from reportlab.lib import colors
        table_rows=list(csv.reader(io.StringIO(self.content().decode('utf-8-sig')),delimiter=';'))
        output=io.BytesIO();table=Table(table_rows,colWidths=[150]*10)
        table.setStyle(TableStyle([('GRID',(0,0),(-1,-1),1,colors.black),('FONTSIZE',(0,0),(-1,-1),7)]))
        SimpleDocTemplate(output,pagesize=(1700,600)).build([table])
        record=self.upload(output.getvalue(),'invoice.pdf')
        self.assertEqual(record['report']['status'],'matched',record['report'])
        from reportlab.pdfgen import canvas
        blank=io.BytesIO();doc=canvas.Canvas(blank);doc.rect(10,10,100,100);doc.showPage();doc.save()
        manual=self.upload(blank.getvalue(),'scan.pdf')
        self.assertEqual(manual['report']['status'],'manual_required')

    def test_supplier_pdf_uses_order_prefix_and_invoice_date_cutoff(self):
        from reportlab.platypus import SimpleDocTemplate,Table,TableStyle,Paragraph,Spacer
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet
        historical=self.expected
        output=io.BytesIO()
        rows=[['Pos.','Bezeichnung','Menge','Einzel EUR','USt. %','Gesamt EUR']]
        for index,item in enumerate(historical['items'],1):
            rows.append([str(index),item['order']+' '+item['article'],'1',item['gross'],'19,00',item['gross']])
        rows.append(['Gesamtbetrag*','','','','',historical['total']])
        table=Table(rows,colWidths=[90,370,55,80,55,80])
        table.setStyle(TableStyle([('GRID',(0,0),(-1,-1),1,colors.black),('FONTSIZE',(0,0),(-1,-1),7)]))
        styles=getSampleStyleSheet()
        SimpleDocTemplate(output,pagesize=(800,600)).build([
            Paragraph('Rechnungsnr.: MH-2026-1',styles['Normal']),
            Paragraph('Datum: 03.09.2026',styles['Normal']),Spacer(1,10),table])

        later=payout('p4','t4','o4',sku='MH45 / C',title='Später hinzugekommener Artikel')
        later['Transaktionsbetrag (inkl. Kosten)']=later['Betrag abzügl. Kosten']
        later['Auszahlungsdatum']='04.09.2026';later['Auszahlungsstatus']='Betrag überwiesen'
        core.import_reports([later],core.ORDERS_DB_PATH,'orders')
        core.import_reports([later],core.PAYOUTS_DB_PATH,'payout')

        record=self.upload(output.getvalue(),'supplier.pdf')
        self.assertEqual(record['report']['status'],'matched',record)
        self.assertEqual(len(record['extracted']['items']),2)
        self.assertEqual(len(record['expected']['items']),2)
        self.assertEqual(record['extracted']['total'],historical['total'])
        incoming.approve(record['id'],'Tester')
        positions=workflow.positions().query("Partner == 'MH'")
        self.assertTrue(positions[positions.Bestellnummer.isin(['o1','o2'])].reviewed_at.astype(bool).all())
        self.assertFalse(positions[positions.Bestellnummer=='o4'].reviewed_at.astype(bool).any())
        self.assertFalse(positions.received_at.astype(bool).any())


if __name__=='__main__':unittest.main()
