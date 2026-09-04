"""Offline regressions for restrictive API evidence and protected business states."""
import io
import json
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

import api_holds
import core
import partner_invoices
import position_workflow as workflow
import payout_reconciliation as manual
import studio_view
from test_recovery import payout
from test_invoice_support import create_matching_invoice


def movement(order='held', identifier='DISPUTE_HOLD-1', **values):
    row = dict(orderId=order, transactionId=identifier, transactionType='DISPUTE',
               bookingEntry='DEBIT', amount={'value': '119.00', 'currency': 'EUR'},
               transactionStatus='PAYOUT', payoutId='later', transactionDate='2026-09-03T10:00:00Z',
               references=[{'referenceType': 'RETURN_ID', 'referenceId': 'r1'}])
    row.update(values)
    return row


def snapshot(rows, at='2026-09-04T10:00:00Z'):
    return dict(account='ebay_durchstart', fetched_at=at,
                resources={'transactions': {'available': True, 'data': {'items': rows}}})


class ApiHoldTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        paths = patch.multiple(core, PAYOUTS_DB_PATH=str(self.root/'Master_Payouts.csv'), ORDERS_DB_PATH=str(self.root/'Master_Orders.csv'))
        paths.start(); self.addCleanup(paths.stop)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('No HTTP'))
        network.start(); self.addCleanup(network.stop)

    def seed(self, sku='MH / 1'):
        frames = [payout('p1','held-t','held',sku=sku), payout('p1','free-t','free',sku=sku)]
        for frame in frames:
            frame['Transaktionsbetrag (inkl. Kosten)'] = frame['Betrag abzügl. Kosten']
            frame['Auszahlungsdatum']='03.09.2026'; frame['Auszahlungsstatus']='Betrag überwiesen'
        core.import_reports(frames,core.ORDERS_DB_PATH,'orders')
        core.import_reports(frames,core.PAYOUTS_DB_PATH,'payout')
        return workflow.positions()

    def test_hold_excludes_only_order_from_partner_and_evelyn_without_source_change(self):
        for sku in ['MH / 1', 'BA / 1']:
            with self.subTest(sku=sku):
                # Separate stores per group.
                with tempfile.TemporaryDirectory() as other, patch.multiple(core,PAYOUTS_DB_PATH=str(Path(other)/'Master_Payouts.csv'),ORDERS_DB_PATH=str(Path(other)/'Master_Orders.csv')):
                    before=self.seed(sku); fingerprint=core.payout_fingerprint(core.load_master_data())
                    api_holds.ingest(other,snapshot([movement()]))
                    after=workflow.positions()
                    self.assertEqual(before.position_key.tolist(),after.position_key.tolist())
                    self.assertEqual(fingerprint,core.payout_fingerprint(core.load_master_data()))
                    self.assertEqual(studio_view.partner_rows(after).Bestellnummer.tolist(),['free'])
                    self.assertEqual(studio_view.eligible_rows(core.load_master_data(),core.sync_status(core.load_master_data())).Bestellnummer.tolist(),['free'])
                    if sku.startswith('MH'):
                        payload=core.build_invoice_payload(core.load_master_data(),'p1','contact',True)
                        self.assertEqual(len(payload['lineItems']),1)
                        self.assertIn('free',payload['lineItems'][0]['description'])

    def test_protected_snapshot_and_paid_closed_fields_are_preserved(self):
        self.seed()
        invoice=create_matching_invoice('MH'); partner_invoices.approve(invoice['id'],'Test')
        payload=core.build_invoice_payload(core.load_master_data(),'p1','contact',True)
        with core.ledger() as db:
            db.execute("UPDATE payouts SET attempt='created',invoice_id='RE0089',snapshot=?,fingerprint=? WHERE id='p1'",(json.dumps(payload),core.payout_fingerprint(core.load_master_data())))
            db.commit()
        before=workflow.positions()
        workflow.confirm(before.position_key.tolist(),'partner_paid',date.today())
        workflow.confirm(before.position_key.tolist(),'evelyn_received',date.today())
        saved=workflow.positions()
        api_holds.ingest(self.root,snapshot([movement()]))
        after=workflow.positions()
        self.assertEqual(saved[list(workflow.FIELDS)].to_dict('records'),after[list(workflow.FIELDS)].to_dict('records'))
        self.assertTrue(after.Lexware_uebertragen.all())
        self.assertTrue(after.query("Bestellnummer=='held'").iloc[0].API_Korrekturfall)
        with core.ledger() as db:
            row=db.execute('SELECT invoice_id,attempt,snapshot FROM payouts').fetchone()
            self.assertEqual(tuple(row),('RE0089','created',json.dumps(payload)))

    def test_hold_after_upload_blocks_approval_and_after_review_blocks_payment(self):
        self.seed('BA / 1'); invoice=create_matching_invoice('BA')
        api_holds.ingest(self.root,snapshot([movement()]))
        with self.assertRaisesRegex(ValueError,'API-Einbehalt'):
            partner_invoices.approve(invoice['id'],'Test')
        self.assertFalse(workflow.positions().reviewed_at.astype(bool).any())

    def test_missing_refresh_refund_and_ordinary_credit_never_release(self):
        self.seed();api_holds.ingest(self.root,snapshot([movement()]))
        changes=[[], [movement(identifier='refund',transactionType='REFUND')],
                 [movement(identifier='sale',transactionType='SALE',bookingEntry='CREDIT')]]
        for rows in changes:
            api_holds.ingest(self.root,snapshot(rows,'2026-09-05T10:00:00Z'))
            self.assertIn('held',api_holds.active(api_holds.load(self.root)))
        before=api_holds.load(self.root)
        api_holds.ingest(self.root,dict(account='ebay_durchstart',fetched_at='2026-09-06T10:00:00Z',resources={'transactions':{'available':False}}))
        self.assertEqual(before,api_holds.load(self.root))

    def test_release_requires_same_order_exact_amount_later_time_and_reference(self):
        held=movement()
        for overrides in [dict(orderId='other'),dict(amount={'value':'118','currency':'EUR'}),dict(transactionDate='2026-09-01T00:00:00Z'),dict(references=[])]:
            release=movement(identifier='DISPUTE_RELEASE-1',bookingEntry='CREDIT',transactionDate='2026-09-05T10:00:00Z')
            release.update(overrides)
            self.assertIn('held',api_holds.active({'observations':[{'at':'2026-09-06T00:00:00Z','transaction':t} for t in [held,release]]}))
        release=movement(identifier='DISPUTE_RELEASE-1',bookingEntry='CREDIT',transactionDate='2026-09-05T10:00:00Z')
        self.assertFalse(api_holds.active({'observations':[{'at':'2026-09-06T00:00:00Z','transaction':t} for t in [held,release]]}))

    def test_manual_holds_remain_even_with_api_release_and_reimport(self):
        self.seed(); state=manual.inspect('p1')
        decisions={r.abgleich_key: 'einbehalten' if r.Bestellnummer=='held' else 'freigegeben' for _,r in state['financial'].iterrows()}
        manual.save('p1','119',decisions,'Test','Bankbeleg geprüft',state['version'],state['source_digest'])
        hold=movement();release=movement(identifier='DISPUTE_RELEASE-1',bookingEntry='CREDIT',transactionDate='2026-09-05T00:00:00Z')
        api_holds.ingest(self.root,snapshot([hold,release]))
        self.assertFalse(workflow.positions().query("Bestellnummer=='held'").iloc[0].partner_ready)
        raw=core.read_master(core.PAYOUTS_DB_PATH)
        core.import_reports([raw],core.PAYOUTS_DB_PATH,'payout')
        self.assertEqual(len(core.read_master(core.PAYOUTS_DB_PATH)),2)

    def test_bank_summary_is_control_only_and_refund_remains(self):
        self.seed()
        summary=payout('p1','','',sku='',title='',amount='-238,00',kind='Auszahlung')
        refund=payout('p2','refund','held',amount='-119,00',kind='Rückerstattung')
        core.import_reports([summary,refund],core.PAYOUTS_DB_PATH,'payout')
        master=core.load_master_data()
        self.assertEqual(len(master),3)
        self.assertEqual((master.Art=='Erstattung').sum(),1)
        self.assertFalse(master['Prüfhinweis'].astype(bool).any())
        self.assertEqual(len(manual.rows('p1')[0]),2)
        api_holds.ingest(self.root,snapshot([movement()]))
        self.assertFalse(core.load_master_data().query("Art=='Erstattung'").API_Hold.any())

    def test_evidence_dedup_backup_restore_and_mirror_conflict(self):
        self.seed();api_holds.ingest(self.root,snapshot([movement()]))
        before=api_holds.load(self.root)
        api_holds.ingest(self.root,snapshot([movement()]))
        self.assertEqual(before,api_holds.load(self.root))
        with zipfile.ZipFile(io.BytesIO(core.backup_data())) as archive:
            self.assertIn(api_holds.FILE,archive.namelist())
        (self.root/api_holds.FILE).unlink()
        self.assertEqual(before,api_holds.load(self.root))
        wrong=dict(before,observations=[])
        (self.root/api_holds.FILE).write_text(json.dumps(wrong))
        with self.assertRaises(ValueError): api_holds.load(self.root)

    def test_invoice_snapshot_does_not_mark_excluded_hold_transferred(self):
        self.seed(); api_holds.ingest(self.root,snapshot([movement()]))
        payload=core.build_invoice_payload(core.load_master_data(),'p1','contact',True)
        with core.ledger() as db:
            db.execute("UPDATE payouts SET attempt='created',invoice_id='draft',snapshot=?",(json.dumps(payload),));db.commit()
        rows=workflow.positions().set_index('Bestellnummer')
        self.assertFalse(rows.loc['held','Lexware_uebertragen'])
        self.assertTrue(rows.loc['free','Lexware_uebertragen'])

    def test_all_group_a_partners_keep_ui_upload_approval_payment_and_completion(self):
        import streamlit as st
        from streamlit.testing.v1 import AppTest
        from test_invoice_support import invoice_csv
        for partner in ['PP','BA','MK','001']:
            with self.subTest(partner=partner), tempfile.TemporaryDirectory() as other, patch.multiple(core,PAYOUTS_DB_PATH=str(Path(other)/'Master_Payouts.csv'),ORDERS_DB_PATH=str(Path(other)/'Master_Orders.csv')):
                self.seed(partner+' / 1')
                app=AppTest.from_file('app.py').run(timeout=30)
                self.assertFalse(app.exception)
                self.assertIn('Einzelabrechnung herunterladen',[b.label for b in app.get('download_button')])
                self.assertNotIn('Bezahlt / abgeschlossen',[b.label for b in app.button])
                self.assertTrue(any('Zahlungsabschluss erst möglich' in c.value for c in app.caption))
                uploaded=io.BytesIO(invoice_csv(partner_invoices.expected_statement(workflow.positions())))
                uploaded.name='invoice.csv'
                original=st.file_uploader
                def uploader(*args,**kwargs):
                    value=original(*args,**kwargs)
                    return uploaded if kwargs.get('key')=='Gruppe_A_'+partner+'-invoice-file' else value
                with patch.object(st,'file_uploader',side_effect=uploader):
                    app.run()
                    next(b for b in app.button if b.key=='Gruppe_A_'+partner+'-invoice-upload').click().run()
                self.assertEqual(partner_invoices.list_invoices(partner)[0]['report']['status'],'matched')
                next(b for b in app.button if b.label=='Geprüfte Rechnung freigeben').click().run()
                next(b for b in app.button if b.label=='Bezahlt / abgeschlossen').click().run()
                self.assertFalse(workflow.positions().paid_at.astype(bool).any())
                next(b for b in app.button if b.label=='Verbindlich bestätigen').click().run()
                self.assertFalse(app.exception)
                self.assertTrue(workflow.positions().closed_at.astype(bool).all())

    def test_global_upload_requires_partner_and_uses_same_panel(self):
        from streamlit.testing.v1 import AppTest
        self.seed('BA / 1')
        app=AppTest.from_file('app.py').run(timeout=30)
        next(b for b in app.button if b.key=='open-incoming-invoice').click().run()
        selector=next(s for s in app.selectbox if s.key=='import-invoice-invoice-partner')
        self.assertIsNone(selector.value)
        self.assertNotIn('import-invoice-invoice-upload',[b.key for b in app.button])
        selector.select('BA').run()
        self.assertIn('import-invoice-invoice-upload',[b.key for b in app.button])
        self.assertFalse(app.exception)


if __name__=='__main__': unittest.main()
