"""Synthetic incoming-invoice fixtures for workflow regression tests."""
import csv
import io
import uuid
import core
import position_workflow as workflow
import partner_invoices


def invoice_csv(expected, number=None):
    output=io.StringIO()
    fields=['Bestellnummer','SKU','Artikel','Menge','Netto vor Rabatt','Rabatt %','Positionsbetrag brutto','Rechnungsnummer','Rechnungsdatum','Gesamtbetrag brutto']
    writer=csv.writer(output,delimiter=';'); writer.writerow(fields)
    number=number or 'TEST-'+str(uuid.uuid4())
    for row in expected['items']:
        writer.writerow([row['order'],row['sku'],row['article'],row['quantity'],row['net'],row['rate'],row['gross'],number,'03.09.2026',expected['total']])
    return output.getvalue().encode('utf-8-sig')


def create_matching_invoice(partner, number=None):
    raw=core.read_master(core.PAYOUTS_DB_PATH)
    if 'Transaktionsbetrag (inkl. Kosten)' not in raw:
        raw['Transaktionsbetrag (inkl. Kosten)']=raw['Betrag abzügl. Kosten']
        raw['Auszahlungsdatum']='03.09.2026'
        raw.to_csv(core.PAYOUTS_DB_PATH,sep=';',index=False,encoding='utf-8-sig')
    rows=workflow.positions()
    rows=rows[(rows.Partner==partner)&rows.partner_ready]
    expected=partner_invoices.expected_statement(rows)
    return partner_invoices.upload(partner,'invoice.csv',invoice_csv(expected,number))[0]


def review_positions(keys):
    rows=workflow.positions()
    for partner in rows[rows.position_key.isin(keys)].Partner.unique():
        record=create_matching_invoice(partner)
        assert record['report']['status']=='matched',record['report']
        partner_invoices.approve(record['id'],'Synthetic tester')
