import copy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from filelock import FileLock

import core
import ebay_sync
import api_holds
from ebay_readonly import EbayError
from test_recovery import payout

NOW=datetime(2026,9,4,12,tzinfo=timezone.utc)


def sale(order='o1', tx='t1', pid='p1', amount='119.00'):
    return dict(transactionType='SALE',transactionId=order,orderId=order,payoutId=pid,
                transactionStatus='PAYOUT' if pid else 'FUNDS_ON_HOLD',bookingEntry='CREDIT',
                amount={'value':amount,'currency':'EUR'},totalFeeBasisAmount={'value':amount,'currency':'EUR'},
                transactionDate='2026-09-03T10:00:00Z',orderLineItems=[dict(lineItemId=tx,feeBasisAmount={'value':amount,'currency':'EUR'})])


class FakeClient:
    def __init__(self,rows=None):
        self.rows=rows if rows is not None else [sale()]
        self.calls=[];self.fail=False;self.difference=False
    def redact(self,value):return value
    def pages(self,endpoint,collection,params=None):
        self.calls.append((endpoint,params))
        if self.fail:raise EbayError('API-Fehler (HTTP 503).')
        if endpoint=='payouts':return {'items':[self.get('payout',pid) for pid in sorted({r['payoutId'] for r in self.rows if r.get('payoutId')})]}
        filter=(params or {}).get('filter','')
        rows=self.rows
        if filter.startswith('payoutId:'):rows=[r for r in rows if r.get('payoutId')==filter.split('{')[1].split('}')[0]]
        if filter.startswith('orderId:'):rows=[r for r in rows if r.get('orderId')==filter.split('{')[1].split('}')[0]]
        return {'items':copy.deepcopy(rows)}
    def get(self,endpoint,identifier):
        rows=[r for r in self.rows if r.get('payoutId')==identifier]
        total=sum((ebay_sync.signed(r) for r in rows),core.Decimal(0))
        if self.difference:total+=1
        return dict(payoutId=identifier,payoutStatus='SUCCEEDED',payoutDate='2026-09-04T08:00:00Z',transactionCount=len(rows),amount={'value':str(total),'currency':'EUR'})


class EbaySyncTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        paths=patch.multiple(core,PAYOUTS_DB_PATH=str(self.root/'Master_Payouts.csv'),ORDERS_DB_PATH=str(self.root/'Master_Orders.csv'))
        paths.start();self.addCleanup(paths.stop)
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('No HTTP'))
        network.start();self.addCleanup(network.stop)
        core.import_reports([payout('','t1','o1',sku='BA / 1')],core.ORDERS_DB_PATH,'orders')

    def run_sync(self,client=None,trigger='manual',now=NOW):
        return ebay_sync.run(self.root,trigger,client or FakeClient(),now)

    def test_import_repeat_source_and_control_are_stable(self):
        first=self.run_sync()
        self.assertEqual(first['status'],'success',first)
        self.assertEqual((first['new_payouts'],first['new_transactions']),(1,1))
        master=core.load_master_data()
        self.assertEqual(master.iloc[0].Partner,'BA')
        before=Path(core.PAYOUTS_DB_PATH).read_bytes()
        second=self.run_sync(trigger='automatic',now=NOW+timedelta(days=1))
        self.assertEqual(second['status'],'success',second)
        self.assertEqual(second['new_transactions'],0)
        self.assertEqual(second['known'],1)
        self.assertEqual(before,Path(core.PAYOUTS_DB_PATH).read_bytes())
        state=ebay_sync.load(self.root)
        self.assertEqual(state['payouts']['p1']['amount']['value'],'119.00')
        self.assertEqual([r['trigger'] for r in state['runs']],['manual','automatic'])

    def test_csv_legacy_matches_then_csv_reimport_deduplicates_api(self):
        frame=payout('p1','t1','o1',sku='BA / 1');frame['Auszahlungsstatus']='Betrag überwiesen'
        core.import_reports([frame],core.PAYOUTS_DB_PATH,'payout')
        before=Path(core.PAYOUTS_DB_PATH).read_bytes()
        result=self.run_sync()
        self.assertEqual(result['status'],'success',result)
        self.assertEqual(result['new_transactions'],0)
        self.assertEqual(before,Path(core.PAYOUTS_DB_PATH).read_bytes())
        core.import_reports([frame],core.PAYOUTS_DB_PATH,'payout')
        self.assertEqual(len(core.read_master(core.PAYOUTS_DB_PATH)),1)

    def test_failure_keeps_sources_and_checkpoint_and_catches_up(self):
        self.run_sync();before=Path(core.PAYOUTS_DB_PATH).read_bytes();watermark=ebay_sync.load(self.root)['watermark']
        failed=FakeClient();failed.fail=True
        result=self.run_sync(failed,now=NOW+timedelta(days=2))
        self.assertEqual(result['status'],'failed')
        self.assertEqual(ebay_sync.load(self.root)['watermark'],watermark)
        self.assertEqual(before,Path(core.PAYOUTS_DB_PATH).read_bytes())
        good=FakeClient([sale(),sale('o2','t2','p2')])
        result=self.run_sync(good,now=NOW+timedelta(days=3))
        self.assertEqual(result['status'],'success',result)
        self.assertEqual(result['new_payouts'],1)
        self.assertEqual(result['start'],ebay_sync.iso(NOW-timedelta(days=7)))

    def test_bad_bank_total_writes_no_source_rows(self):
        client=FakeClient();client.difference=True
        result=self.run_sync(client)
        self.assertEqual(result['status'],'failed')
        self.assertFalse(Path(core.PAYOUTS_DB_PATH).exists())
        self.assertIsNone(ebay_sync.load(self.root)['watermark'])

    def test_parent_one_financial_row_refs_preserved_and_csv_parent_not_duplicated(self):
        row=sale(amount='149.96');row['orderLineItems']=[dict(lineItemId='x',feeBasisAmount={'value':'99.98','currency':'EUR'}),dict(lineItemId='y',feeBasisAmount={'value':'49.98','currency':'EUR'})]
        result=self.run_sync(FakeClient([row]));self.assertEqual(result['status'],'success',result)
        raw=core.read_master(core.PAYOUTS_DB_PATH)
        self.assertEqual(len(raw),1);self.assertEqual(core.parse_money(raw.iloc[0]['Betrag abzügl. Kosten']),core.Decimal('149.96'))
        self.assertEqual(len(json.loads(raw.iloc[0].API_Artikelreferenzen)),2)
        parent=payout('p1','','o1',sku='',amount='149,96');parent['Artikelnummer']=''
        core.import_reports([parent],core.PAYOUTS_DB_PATH,'payout')
        self.assertEqual(len(core.read_master(core.PAYOUTS_DB_PATH)),1)

    def test_pending_to_paid_updates_same_position_but_hold_not_assumed_gone(self):
        row=sale(pid='');result=self.run_sync(FakeClient([row]));self.assertEqual(result['status'],'success',result)
        self.assertTrue(core.load_master_data().empty)
        result=self.run_sync(FakeClient([sale()]),now=NOW+timedelta(days=1))
        self.assertEqual(result['status'],'success',result)
        raw=core.read_master(core.PAYOUTS_DB_PATH);self.assertEqual(len(raw),1)
        self.assertEqual(raw.iloc[0]['Auszahlung Nr.'],'p1')
        self.assertFalse(core.load_master_data().API_Hold.any())

    def test_locked_extra_position_is_not_imported_and_checkpoint_not_advanced(self):
        self.run_sync()
        with core.ledger() as db:
            db.execute("UPDATE payouts SET attempt='created',invoice_id='protected' WHERE id='p1'");db.commit()
        # Ensure there is a ledger payout row first.
        core.sync_status(core.load_master_data())
        with core.ledger() as db:
            db.execute("UPDATE payouts SET attempt='created',invoice_id='protected' WHERE id='p1'");db.commit()
        before=ebay_sync.load(self.root)['watermark']
        result=self.run_sync(FakeClient([sale(),sale('o2','t2')]),now=NOW+timedelta(days=1))
        self.assertEqual(result['status'],'partial',result)
        self.assertEqual(len(core.read_master(core.PAYOUTS_DB_PATH)),1)
        self.assertEqual(ebay_sync.load(self.root)['watermark'],before)

    def test_concurrent_run_is_not_started(self):
        with FileLock(str(self.root/ebay_sync.FILE)+'.lock'):
            self.assertEqual(self.run_sync()['status'],'busy')

    def test_refund_and_hold_are_not_double_booked(self):
        refund=dict(transactionType='REFUND',transactionId='RRP-1',orderId='o1',payoutId='p1',transactionStatus='PAYOUT',bookingEntry='DEBIT',amount={'value':'19','currency':'EUR'},transactionDate='2026-09-03T12:00:00Z',references=[{'referenceId':'123','referenceType':'REFUND_ID'}])
        hold=dict(refund,transactionType='DISPUTE',transactionId='RETRO_HOLD-1',amount={'value':'20','currency':'EUR'})
        result=self.run_sync(FakeClient([sale(),refund,hold]));self.assertEqual(result['status'],'success',result)
        master=core.load_master_data();self.assertEqual(len(master),2)
        self.assertEqual((master.Art=='Erstattung').sum(),1)
        self.assertTrue(master[master.Art=='Bestellung'].API_Hold.all())
        self.assertEqual(ebay_sync.load(self.root)['payouts']['p1']['amount']['value'],'80.00')
        csv=payout('p1','','o1',amount='-19,00',kind='Rückerstattung');csv['Referenznummer']='Rückerstattung Nr. 123';csv['Artikelnummer']=''
        core.import_reports([csv],core.PAYOUTS_DB_PATH,'payout')
        self.assertEqual(len(core.read_master(core.PAYOUTS_DB_PATH)),2)

    def test_first_failed_window_is_not_lost_before_initial_success(self):
        client=FakeClient();client.fail=True
        self.run_sync(client)
        result=self.run_sync(now=NOW+timedelta(days=4))
        self.assertEqual(result['start'],ebay_sync.iso(NOW-timedelta(days=90)))

    def test_two_partial_refunds_of_same_item_keep_distinct_references(self):
        orders=core.read_master(core.ORDERS_DB_PATH);orders['Artikelnummer']='item1'
        orders.to_csv(core.ORDERS_DB_PATH,sep=';',index=False,encoding='utf-8-sig')
        refunds=[]
        for reference in ('123','124'):
            refunds.append(dict(transactionType='REFUND',transactionId='RRP-'+reference,orderId='o1',payoutId='p1',transactionStatus='PAYOUT',bookingEntry='DEBIT',amount={'value':'19','currency':'EUR'},transactionDate='2026-09-03T12:00:00Z',references=[{'referenceId':reference,'referenceType':'REFUND_ID'}]))
        client=FakeClient([sale(),*refunds])
        result=self.run_sync(client)
        self.assertEqual(result['status'],'success',result)
        self.assertEqual(len(core.read_master(core.PAYOUTS_DB_PATH)),3)
        import position_workflow
        self.assertEqual(position_workflow.positions().position_key.nunique(),3)
        result=self.run_sync(client,now=NOW+timedelta(days=1))
        self.assertEqual(result['status'],'success',result)
        self.assertEqual(result['new_transactions'],0)

    def test_manual_button_invokes_the_shared_runner_and_history_is_visible(self):
        from streamlit.testing.v1 import AppTest
        with patch('trust_risk_ui.secrets_config',return_value={'configured':'test'}):
            app=AppTest.from_file('app.py').run(timeout=30)
            app.session_state['ebay_readonly_client']=FakeClient()
            app.button(key='ebay-risk-refresh').click().run(timeout=30)
            self.assertFalse(app.exception)
            record=ebay_sync.load(self.root)['runs'][-1]
            self.assertEqual(record['trigger'],'manual')
            self.assertEqual(record['status'],'success',record)
            self.assertIn('eBay API · Abrufhistorie',[e.label for e in app.expander])
            self.assertTrue(any('Datenstand der eBay-API:' in c.value for c in app.caption))


if __name__=='__main__':unittest.main()
