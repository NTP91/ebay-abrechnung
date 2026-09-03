import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import core
import studio_view
from test_recovery import payout


class StudioViewTests(unittest.TestCase):
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


if __name__=='__main__':
    unittest.main()
