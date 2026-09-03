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
        frames=[payout('p1','t1','o1',sku='MH43 / A',title='Vollständiger Artikel A'),payout('p2','t2','o2',sku='MH44 / B',title='Vollständiger Artikel B'),payout('p3','t3','o3',sku='NB / C')]
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
        path.write_bytes(b'changed')
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

    def test_existing_partner_workbook_with_empty_credit_sheet_is_readable(self):
        from partner_export import export_partner_excel
        record=self.upload(export_partner_excel(self.rows),'partner.xlsx')
        self.assertEqual(record['report']['status'],'matched',record['report'])

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


if __name__=='__main__':unittest.main()
