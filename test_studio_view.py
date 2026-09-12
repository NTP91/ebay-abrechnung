import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch
import core
import studio_view
import position_workflow as workflow
from test_recovery import payout


class StudioViewTests(unittest.TestCase):
    def test_lexware_button_requires_key_scope_and_all_three_confirmations(self):
        self.assertFalse(studio_view.lexware_create_ready(['p1'], {'gross': 1}, '', (True, True, True)))
        self.assertFalse(studio_view.lexware_create_ready(['p1'], {'gross': 1}, 'key', (True, False, True)))
        self.assertTrue(studio_view.lexware_create_ready(['p1'], {'gross': 1}, 'key', (True, True, True)))

    def test_register_timestamp_is_rendered_in_berlin_without_iso_details(self):
        values=core.pd.Series(['2026-09-03T14:03:19.099308+00:00','unlesbar'])
        self.assertEqual(studio_view.local_datetime(values).tolist(),['03.09.2026 16:03','nicht bekannt'])

    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        paths=patch.multiple(core,PAYOUTS_DB_PATH=str(Path(temp.name)/'Master_Payouts.csv'),ORDERS_DB_PATH=str(Path(temp.name)/'Master_Orders.csv'))
        paths.start()
        self.addCleanup(paths.stop)
        frames=[payout('p1','t1','o1'),payout('p2','t2','o2'),payout('p3','t3','o3')]
        for frame in frames:
            frame['Auszahlungsstatus']='Betrag überwiesen'
        core.import_reports(frames,core.ORDERS_DB_PATH,'orders')
        core.import_reports(frames,core.PAYOUTS_DB_PATH,'payout')
        core.sync_status(core.load_master_data())
        with core.ledger() as db:
            db.execute("UPDATE payouts SET attempt='created',invoice_id='earlier' WHERE id='p1'")
            db.commit()
        self.http=Mock()
        self.http.get.return_value.status_code=200
        self.http.get.return_value.json.return_value={'content':[{'id':'contact','roles':{'customer':{'number':16335}}}]}
        self.http.post.return_value.status_code=201
        self.http.post.return_value.json.return_value={'id':'new'}

    def test_previous_and_pending_payouts_are_excluded(self):
        master=core.load_master_data()
        ready=studio_view.eligible_rows(master,core.sync_status(master))
        self.assertEqual(set(ready['Auszahlung Nr.']),{'p2','p3'})
        with core.ledger() as db:
            db.execute("UPDATE payouts SET attempt='pending' WHERE id='p2'")
            db.commit()
        ready=studio_view.eligible_rows(master,core.sync_status(master))
        self.assertEqual(set(ready['Auszahlung Nr.']),{'p3'})

    def test_unconfirmed_payout_is_not_shown_as_lexware_ready(self):
        payouts=core.read_master(core.PAYOUTS_DB_PATH)
        payouts.loc[payouts['Auszahlung Nr.']=='p2','Auszahlungsstatus']='In Bearbeitung'
        payouts.to_csv(core.PAYOUTS_DB_PATH,sep=';',index=False,encoding='utf-8-sig')
        master=core.load_master_data()
        ready=studio_view.eligible_rows(master,core.sync_status(master))
        self.assertEqual(set(ready['Auszahlung Nr.']),{'p3'})

    def test_one_ambiguous_position_does_not_block_clean_rows_in_same_payout(self):
        master=core.load_master_data()
        clean=master[master['Auszahlung Nr.']=='p3'].iloc[0].copy()
        ambiguous=clean.copy()
        ambiguous['Transaktionsnummer']='t3-ambiguous';ambiguous['Bestellnummer']='o3-ambiguous'
        ambiguous['Prüfhinweis']='Zuordnung fehlt: mehrdeutig'
        mixed=core.pd.concat([master,core.pd.DataFrame([ambiguous])],ignore_index=True)
        states=core.sync_status(mixed)
        self.assertIn('Prüfung',states.loc[states.Auszahlung=='p3','Status'].iloc[0])
        ready=studio_view.eligible_rows(mixed,states)
        same=ready[ready['Auszahlung Nr.']=='p3']
        self.assertEqual(same.Bestellnummer.tolist(),['o3'])
        payload=core.build_invoice_payload(mixed,'p3','contact',True)
        self.assertEqual(len(payload['lineItems']),1)
        self.assertIn('o3',payload['lineItems'][0]['description'])

    def prepare(self):
        for pid in ['p2','p3']:
            core.confirm_received(pid)

    def test_combined_draft_reserves_both_before_one_post(self):
        self.prepare()
        def post(*args,**kwargs):
            with core.ledger() as db:
                self.assertEqual([row['attempt'] for row in db.execute("SELECT * FROM payouts WHERE id IN ('p2','p3') ORDER BY id")],['pending','pending'])
            self.assertEqual(len(kwargs['json']['lineItems']),2)
            self.assertEqual(kwargs['json']['remark'],'eBay-Auszahlungsnummern: p2, p3')
            self.assertEqual(kwargs['params'],{'finalize':'false'})
            self.assertEqual([i['unitPrice']['netAmount'] for i in kwargs['json']['lineItems']],[100,100])
            return Mock(status_code=201,json=lambda:{'id':'new'})
        self.http.post.side_effect=post
        core.create_invoice_draft('fake',['p2','p3'],True,self.http)
        with self.assertRaises(ValueError):
            core.create_invoice_draft('fake',['p2','p3'],True,self.http)
        self.assertEqual(self.http.post.call_count,1)
        self.assertTrue(studio_view.eligible_rows(core.load_master_data(),core.sync_status(core.load_master_data())).empty)

    def test_combined_timeout_and_locked_selection_fail_closed(self):
        self.prepare()
        with self.assertRaises(ValueError):
            core.create_invoice_draft('fake',['p1','p2'],True,self.http)
        self.http.post.assert_not_called()
        self.http.post.side_effect=TimeoutError()
        with self.assertRaises(ValueError):
            core.create_invoice_draft('fake',['p2','p3'],True,self.http)
        with self.assertRaises(ValueError):
            core.create_invoice_draft('fake',['p2','p3'],True,self.http)
        self.assertEqual(self.http.post.call_count,1)

    def test_changed_display_snapshot_cannot_be_sent(self):
        self.prepare()
        with self.assertRaises(ValueError):
            core.create_invoice_draft('fake',['p2','p3'],True,self.http,expected_fingerprints={'p2':'changed'})
        self.http.post.assert_not_called()


class EvelynRefundOffsetTests(unittest.TestCase):
    """Automatic refund netting for the Group-B Evelyn selection (studio_view.evelyn_overview)."""

    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        paths=patch.multiple(core,PAYOUTS_DB_PATH=str(self.root/'Master_Payouts.csv'),ORDERS_DB_PATH=str(self.root/'Master_Orders.csv'))
        paths.start(); self.addCleanup(paths.stop)
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('Live HTTP forbidden'))
        network.start(); self.addCleanup(network.stop)

    def seed(self,frames,payout_id='p1',invoice=False):
        for frame in frames:
            frame['Transaktionsbetrag (inkl. Kosten)']=frame['Betrag abzügl. Kosten']
            frame['Auszahlungsdatum']='03.09.2026'; frame['Auszahlungsstatus']='Betrag überwiesen'
        core.import_reports(frames,core.ORDERS_DB_PATH,'orders')
        core.import_reports(frames,core.PAYOUTS_DB_PATH,'payout')
        core.sync_status(core.load_master_data())
        if invoice:
            with core.ledger() as db:
                db.execute("UPDATE payouts SET attempt='created',invoice_id='draft' WHERE id=?",(payout_id,))
                db.commit()

    def overview(self):
        business=workflow.positions()
        master=core.load_master_data()
        eligible=studio_view.eligible_rows(master,core.sync_status(master))
        return studio_view.evelyn_overview(business,eligible,{})

    def test_fully_refunded_pending_position_drops_out_of_the_evelyn_run(self):
        sale=payout('p1','sale-full','order-full',sku='NB / 1',amount='119,00')
        refund=payout('p1','refund-full','order-full',sku='NB / 1',amount='-119,00',kind='Rückerstattung')
        for frame in (sale,refund): frame['Artikelnummer']='item-full'
        self.seed([sale,refund])
        overview=self.overview()
        for bucket in ('ready','review','held','new_ready','new_review','new_held'):
            self.assertNotIn('order-full',overview[bucket].Bestellnummer.tolist(),bucket)
        self.assertTrue(overview['refund_cases'].empty)
        # The refund row itself is preserved as its own movement, just outside the sale buckets.
        business=workflow.positions()
        self.assertIn('order-full',business.loc[business.Art=='Erstattung','Bestellnummer'].tolist())

    def test_partial_refund_reduces_open_amount_but_keeps_original_for_payout_matching(self):
        sale=payout('p1','sale-partial','order-partial',sku='NB / 1',amount='100,00')
        refund=payout('p1','refund-partial','order-partial',sku='NB / 1',amount='-40,00',kind='Rückerstattung')
        for frame in (sale,refund): frame['Artikelnummer']='item-partial'
        self.seed([sale,refund])
        business=workflow.positions()
        original=business[(business.Bestellnummer=='order-partial')&(business.Art=='Bestellung')].iloc[0]
        self.assertEqual(round(float(original.Erlös_Brutto),2),100.0)
        overview=self.overview()
        row=overview['new_ready'][overview['new_ready'].Bestellnummer=='order-partial'].iloc[0]
        self.assertEqual(round(float(row.Erlös_Brutto),2),60.0)
        self.assertEqual(round(float(row.Erlös_Brutto_Original),2),100.0)
        ratio_netto=float(row.eBay_Netto)/float(original.eBay_Netto)
        self.assertAlmostEqual(ratio_netto,0.6,places=6)
        # Payout matching must still resolve against the true, unreduced report amount.
        from partner_export import prepare_partner_export
        prepared=prepare_partner_export(overview['new_ready'],statement_type='group_b_evelyn')
        self.assertEqual(len(prepared['Rechnung']),1)
        self.assertGreater(overview['total'],Decimal('0'))

    def test_refund_after_lexware_transfer_leaves_bound_row_untouched_and_opens_a_credit_case(self):
        sale=payout('p1','sale-bound','order-bound',sku='NB / 1',amount='80,00')
        refund=payout('p1','refund-bound','order-bound',sku='NB / 1',amount='-80,00',kind='Rückerstattung')
        for frame in (sale,refund): frame['Artikelnummer']='item-bound'
        self.seed([sale,refund],invoice=True)
        overview=self.overview()
        self.assertEqual(overview['bound'].Bestellnummer.tolist(),['order-bound'])
        self.assertEqual(round(float(overview['bound'].iloc[0].Erlös_Brutto),2),80.0)
        self.assertEqual(overview['refund_cases'].Bestellnummer.tolist(),['order-bound'])
        self.assertEqual(round(float(overview['refund_cases'].iloc[0].Erstattet_Brutto),2),-80.0)
        self.assertTrue(overview['ready'].empty)
        self.assertTrue(overview['review'].empty)

    def test_refund_cannot_apply_twice_when_two_sale_rows_share_the_same_order_line(self):
        first=payout('p1','sale-a','order-ambiguous',sku='NB / 1',amount='50,00')
        second=payout('p1','sale-b','order-ambiguous',sku='NB / 1',amount='50,00')
        refund=payout('p1','refund-ambiguous','order-ambiguous',sku='NB / 1',amount='-50,00',kind='Rückerstattung')
        for frame in (first,second,refund): frame['Artikelnummer']='item-ambiguous'
        self.seed([first,second,refund])
        overview=self.overview()
        untouched=overview['ready'][overview['ready'].Bestellnummer=='order-ambiguous']
        self.assertEqual(len(untouched),2)
        self.assertTrue((untouched['Erlös_Brutto'].round(2)==50.0).all())


if __name__=='__main__':
    unittest.main()
