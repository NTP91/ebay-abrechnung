import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core
import data_status
from test_recovery import payout, Upload


class OverlappingPayoutTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        paths=patch.multiple(core,PAYOUTS_DB_PATH=str(self.root/'Master_Payouts.csv'),ORDERS_DB_PATH=str(self.root/'Master_Orders.csv'))
        paths.start()
        self.addCleanup(paths.stop)
        self.old=[payout('7700379513','a','oa'),payout('7700379513','b','ob'),payout('7710027297','c','oc'),payout('7712804241','d','od')]
        core.import_reports(self.old,core.PAYOUTS_DB_PATH,'payout')
        core.sync_status(core.load_master_data())
        with core.ledger() as db:
            db.execute("UPDATE payouts SET attempt='created',invoice_id='RE0089-test',status='Lexoffice-Entwurf erstellt'")
            db.commit()

    def upload(self, frames):
        return data_status.import_file(Upload(core.pd.concat(frames).to_csv(sep=';',index=False).encode('utf-8')),'payout')

    def test_partial_old_payouts_new_fourth_and_open_lifecycle(self):
        new=payout('7714928937','e','oe')
        opened=payout('','f','of',amount='--')
        result=self.upload([self.old[0],self.old[2],self.old[3],new,opened])
        self.assertFalse(result['error'])
        self.assertFalse(result['transactions']['warnings'])
        self.assertEqual(result['transactions']['known_paid'],3)
        self.assertEqual(result['transactions']['new_paid'],1)
        self.assertEqual(result['transactions']['new_open'],1)
        master=core.load_master_data()
        self.assertEqual(master['Auszahlung Nr.'].nunique(),4)
        self.assertEqual(len(master),5) # omitted old transaction still exists
        before=Path(core.PAYOUTS_DB_PATH).read_bytes()
        repeated=self.upload([self.old[0],self.old[2],self.old[3],new,opened])
        self.assertEqual(repeated['transactions']['known_paid'],4)
        self.assertEqual(repeated['transactions']['still_open'],1)
        self.assertEqual(before,Path(core.PAYOUTS_DB_PATH).read_bytes())
        # A later addition to an unlocked fourth payout promotes the open row.
        promoted=self.upload([payout('7714928937','f','of')])
        self.assertEqual(promoted['transactions']['assigned_open'],1)
        self.assertEqual(len(core.read_master(core.PAYOUTS_DB_PATH)),6)
        self.assertEqual(core.load_master_data()['Auszahlung Nr.'].nunique(),4)

    def test_unknown_locked_position_warns_without_blocking_new_payout(self):
        before=core.read_master(core.PAYOUTS_DB_PATH)
        result=self.upload([self.old[0],payout('7700379513','extra','ox'),payout('7714928937','new','on')])
        self.assertFalse(result['error'])
        self.assertEqual(result['transactions']['new_paid'],1)
        self.assertEqual(result['transactions']['warnings'][0]['payout'],'7700379513')
        after=core.read_master(core.PAYOUTS_DB_PATH)
        core.pd.testing.assert_frame_equal(before,after[after['Auszahlung Nr.']!='7714928937'].reset_index(drop=True))
        with core.ledger() as db:
            rows=db.execute("SELECT * FROM payouts WHERE invoice_id='RE0089-test'").fetchall()
            self.assertEqual(len(rows),3)
            self.assertTrue(all(row['attempt']=='created' for row in rows))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM import_warnings').fetchone()[0],1)

    def test_bad_amount_is_isolated_to_its_payout(self):
        result=self.upload([payout('7700379513','a','oa',amount='invalid'),payout('7714928937','new','on')])
        self.assertFalse(result['error'])
        self.assertEqual(result['transactions']['new_paid'],1)
        self.assertEqual(len(result['transactions']['warnings']),1)

    def test_71_known_orders_are_not_duplicated(self):
        orders=core.pd.concat([payout(transaction=f't{i}',order=f'o{i}') for i in range(71)])
        report=Upload(orders.to_csv(sep=';',index=False).encode('utf-8'),'orders.csv')
        first=data_status.import_file(report,'orders')
        self.assertEqual(first['added'],71)
        before=Path(core.ORDERS_DB_PATH).read_bytes()
        second=data_status.import_file(report,'orders')
        self.assertEqual((second['added'],second['present']),(0,71))
        self.assertEqual(before,Path(core.ORDERS_DB_PATH).read_bytes())


if __name__=='__main__':
    unittest.main()
