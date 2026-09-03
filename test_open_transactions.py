import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core
import data_status
from test_recovery import payout, Upload


class OpenTransactionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        paths = patch.multiple(core, PAYOUTS_DB_PATH=str(self.root/'Master_Payouts.csv'), ORDERS_DB_PATH=str(self.root/'Master_Orders.csv'))
        paths.start()
        self.addCleanup(paths.stop)

    def upload(self, rows):
        return data_status.import_file(Upload(core.pd.concat(rows).to_csv(sep=';',index=False).encode('utf-8')), 'payout')

    def test_mixed_open_and_paid_repeat_and_promotion(self):
        core.import_reports([payout(),payout(transaction='t2',order='o2')],core.ORDERS_DB_PATH,'orders')
        open_row=payout('',transaction='t2',order='o2',amount='--')
        paid=payout()
        result=self.upload([paid,open_row])
        self.assertFalse(result['error'])
        self.assertEqual(result['transactions']['new_paid'],1)
        self.assertEqual(result['transactions']['new_open'],1)
        self.assertEqual(result['issues'],0)
        before=Path(core.PAYOUTS_DB_PATH).read_bytes()
        result=self.upload([paid,open_row])
        self.assertEqual(result['transactions']['known_paid'],1)
        self.assertEqual(result['transactions']['still_open'],1)
        self.assertEqual(Path(core.PAYOUTS_DB_PATH).read_bytes(),before)
        self.assertEqual(len(core.load_master_data()),1)
        payload=core.build_invoice_payload(core.load_master_data(),'7700379513','contact',True)
        self.assertEqual(len(payload['lineItems']),1)
        self.assertEqual(payload['lineItems'][0]['unitPrice']['netAmount'],100)
        later=payout('7712804241',transaction='t2',order='o2',amount='238,00')
        result=self.upload([later])
        self.assertFalse(result['error'])
        self.assertEqual(result['transactions']['assigned_open'],1)
        self.assertEqual(result['transactions']['new_paid'],1)
        self.assertEqual(len(core.read_master(core.PAYOUTS_DB_PATH)),2)
        self.assertEqual(len(core.load_master_data()),2)
        before=Path(core.PAYOUTS_DB_PATH).read_bytes()
        self.upload([open_row])
        self.upload([later])
        self.assertEqual(Path(core.PAYOUTS_DB_PATH).read_bytes(),before)

    def test_only_open_is_not_error_or_ledger_entry(self):
        result=self.upload([payout('',amount='--')])
        self.assertFalse(result['error'])
        self.assertEqual(result['issues'],0)
        self.assertTrue(core.load_master_data().empty)
        self.assertTrue(core.sync_status(core.load_master_data()).empty)
        from streamlit.testing.v1 import AppTest
        app=AppTest.from_file(str(Path(__file__).with_name('app.py'))).run()
        self.assertFalse(list(app.exception))

    def test_refund_stays_separate_and_locks_unchanged(self):
        paid=payout()
        self.upload([paid])
        with core.ledger() as db:
            db.execute("UPDATE payouts SET attempt='created', invoice_id='saved',status='Lexoffice-Entwurf erstellt'")
            db.commit()
        refund=payout('',amount='-119,00',kind='Erstattung')
        result=self.upload([paid,refund])
        self.assertFalse(result['error'])
        self.assertEqual(len(core.read_master(core.PAYOUTS_DB_PATH)),2)
        self.assertEqual(len(core.load_master_data()),1)
        state=core.sync_status(core.load_master_data()).iloc[0]
        self.assertEqual((state.Sperre,state.Entwurf),('created','saved'))

    def test_same_batch_paid_wins_and_different_payout_is_rejected(self):
        open_row=payout('')
        paid=payout()
        result=self.upload([open_row,paid])
        self.assertFalse(result['error'])
        self.assertEqual(len(core.read_master(core.PAYOUTS_DB_PATH)),1)
        before=Path(core.PAYOUTS_DB_PATH).read_bytes()
        result=self.upload([payout('other')])
        self.assertTrue(result['error'])
        self.assertEqual(Path(core.PAYOUTS_DB_PATH).read_bytes(),before)

    def test_later_transaction_id_enriches_open_item(self):
        open_row=payout('',transaction='')
        open_row['Artikelnummer']='item-1'
        paid=payout()
        paid['Artikelnummer']='item-1'
        self.assertFalse(self.upload([open_row])['error'])
        result=self.upload([paid])
        self.assertFalse(result['error'])
        self.assertEqual(result['transactions']['assigned_open'],1)
        self.assertEqual(len(core.read_master(core.PAYOUTS_DB_PATH)),1)


if __name__=='__main__':
    unittest.main()
