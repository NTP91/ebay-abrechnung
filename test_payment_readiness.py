"""Critical settlement flow only; isolated storage and no live HTTP."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import core
import partner_invoices as incoming
import position_workflow as workflow
import studio_view
from partner_export import prepare_partner_export
from test_recovery import payout
from test_invoice_support import invoice_csv


class PaymentReadinessTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        paths=patch.multiple(core,PAYOUTS_DB_PATH=str(self.root/'Master_Payouts.csv'),ORDERS_DB_PATH=str(self.root/'Master_Orders.csv'))
        paths.start();self.addCleanup(paths.stop)
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('Live HTTP forbidden'))
        network.start();self.addCleanup(network.stop)

    def seed(self,frames):
        for frame in frames:
            frame['Transaktionsbetrag (inkl. Kosten)']=frame['Betrag abzügl. Kosten']
            frame['Auszahlungsdatum']='03.09.2026';frame['Auszahlungsstatus']='Betrag überwiesen'
        core.import_reports(frames,core.ORDERS_DB_PATH,'orders')
        core.import_reports(frames,core.PAYOUTS_DB_PATH,'payout')

    def approve(self,rows,scope='Rechnung',number='TEST'):
        record,_=incoming.upload(rows.iloc[0].Partner,'invoice.csv',invoice_csv(incoming.expected_statement(rows),number),scope)
        self.assertEqual(record['report']['status'],'matched',record['report'])
        incoming.approve(record['id'],'Offline test')
        return record

    def test_group_a_new_positions_after_approved_invoice_remain_payable(self):
        self.seed([payout('p1','old','old',sku='BA / 1')])
        first=self.approve(workflow.positions(),number='FIRST')
        self.seed([payout('p2','new','new',sku='BA / 2')])
        rows=workflow.positions()
        self.approve(rows[rows.Bestellnummer=='new'],number='SECOND')
        self.assertEqual(str(incoming.confirmed_payment_total(workflow.positions())),'236.82')
        self.assertEqual(studio_view.partner_summary(workflow.positions()).iloc[0]['Rechnungsbetrag'],236.82)
        workflow.confirm(workflow.positions().position_key.tolist(),'partner_paid',date.today())
        self.assertTrue(workflow.positions().closed_at.astype(bool).all())
        self.assertEqual(incoming.list_invoices('BA')[-1]['id'],first['id'])

    def test_refunds_export_approval_and_settlement_once_for_both_groups(self):
        from streamlit.testing.v1 import AppTest
        for partner in ['BA','MH']:
            with self.subTest(partner=partner):
                self.seed([payout(partner,partner+'sale',partner+'order',sku=partner+' / 1'),
                           payout(partner,partner+'refund',partner+'order',sku=partner+' / 1',amount='-59,50',kind='Rückerstattung')])
                rows=workflow.positions().query('Partner == @partner')
                model=prepare_partner_export(rows)
                self.assertEqual(len(model['Rechnung']),1);self.assertEqual(len(model['Gutschriften']),1)
                self.assertLess(model['totals']['Gutschriften']['gross'],0)
                refunds=rows[rows.Art=='Erstattung']
                self.approve(refunds,'Gutschriften','CREDIT-'+partner)
                app=AppTest.from_file('app.py').run(timeout=30)
                next(b for b in app.button if b.label=='Erstattung erledigt bestätigen').click().run()
                next(b for b in app.button if b.label=='Verbindlich bestätigen').click().run()
                self.assertFalse(app.exception)
                saved=workflow.positions().set_index('position_key').loc[refunds.position_key]
                self.assertTrue(saved.closed_at.astype(bool).all())
                with self.assertRaises(ValueError):workflow.confirm(refunds.position_key.tolist(),'refund_settled',date.today())

    def test_group_b_new_draft_excludes_old_draft_and_payments_are_independent(self):
        self.seed([payout('old','old','old'),payout('new','new','new')])
        core.sync_status(core.load_master_data())
        old=core.build_invoice_payload(core.load_master_data(),'old','contact',True)
        with core.ledger() as db:
            db.execute("UPDATE payouts SET invoice_id='RE0089',attempt='created',snapshot=? WHERE id='old'",(json.dumps(old),));db.commit()
        ready=studio_view.eligible_rows(core.load_master_data(),core.sync_status(core.load_master_data()))
        self.assertEqual(ready.Bestellnummer.tolist(),['new'])
        http=Mock();http.get.return_value.status_code=200
        http.get.return_value.json.return_value={'content':[{'id':'contact','roles':{'customer':{'number':16335}}}]}
        http.post.return_value.status_code=201;http.post.return_value.json.return_value={'id':'real-draft-simulated'}
        core.confirm_received('new')
        core.create_invoice_draft('FAKE','new',True,http)
        self.assertEqual(len(http.post.call_args.kwargs['json']['lineItems']),1)
        self.assertEqual(http.post.call_args.kwargs['params'],{'finalize':'false'})
        with self.assertRaises(ValueError):core.create_invoice_draft('FAKE','new',True,http)
        self.assertEqual(http.post.call_count,1)
        rows=workflow.positions();new=rows[rows.Bestellnummer=='new']
        workflow.confirm(new.position_key.tolist(),'evelyn_received',date.today())
        self.assertFalse(workflow.positions().paid_at.astype(bool).any())
        self.approve(workflow.positions(),number='PARTNER')
        workflow.confirm(new.position_key.tolist(),'partner_paid',date.today())
        result=workflow.positions().set_index('Bestellnummer')
        self.assertTrue(result.loc['new','closed_at']);self.assertFalse(result.loc['old','closed_at'])

    def test_payload_never_includes_a_reviewed_position_with_changed_source(self):
        self.seed([payout('p1','changed','changed'),payout('p1','good','good')])
        self.approve(workflow.positions())
        orders=core.read_master(core.ORDERS_DB_PATH)
        orders.loc[orders.Bestellnummer=='changed','Angebotstitel']='Changed after invoice approval'
        orders.to_csv(core.ORDERS_DB_PATH,sep=';',index=False,encoding='utf-8-sig')
        master=core.load_master_data()
        ready=studio_view.eligible_rows(master,core.sync_status(master))
        self.assertEqual(ready.Bestellnummer.tolist(),['good'])
        payload=core.build_invoice_payload(master,'p1','contact',True)
        self.assertEqual(len(payload['lineItems']),1)
        self.assertIn('good',payload['lineItems'][0]['description'])


if __name__=='__main__':unittest.main()
