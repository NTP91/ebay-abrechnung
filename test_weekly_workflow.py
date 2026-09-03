import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core
import data_status
from test_recovery import payout, Upload


class WeeklyWorkflowTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        paths = patch.multiple(core, PAYOUTS_DB_PATH=str(self.root/'Master_Payouts.csv'), ORDERS_DB_PATH=str(self.root/'Master_Orders.csv'))
        paths.start()
        self.addCleanup(paths.stop)

    def seed(self, sku='NB /'):
        core.import_reports([payout(sku=sku)], core.ORDERS_DB_PATH, 'orders')
        core.import_reports([payout()], core.PAYOUTS_DB_PATH, 'payout')
        core.sync_status(core.load_master_data())

    def lock(self):
        with core.ledger() as db:
            db.execute("UPDATE payouts SET attempt='created',invoice_id='existing-draft',status='Lexoffice-Entwurf erstellt'")
            db.commit()

    def test_register_rebuild_and_reimport_preserve_lock(self):
        self.seed()
        self.lock()
        (self.root/'Settlement_State.sqlite3').unlink()
        self.assertEqual(core.import_reports([payout()],core.PAYOUTS_DB_PATH,'payout'),0)
        state=core.sync_status(core.load_master_data()).iloc[0]
        self.assertEqual(state.Sperre,'created')
        self.assertEqual(state.Entwurf,'existing-draft')
        # Replacing the register with an empty SQLite file also cannot release it.
        (self.root/'Settlement_State.sqlite3').unlink()
        sqlite3.connect(self.root/'Settlement_State.sqlite3').close()
        self.assertEqual(core.sync_status(core.load_master_data()).iloc[0].Sperre,'created')

    def test_missing_all_register_files_fails_closed(self):
        self.seed()
        self.lock()
        (self.root/'Settlement_State.sqlite3').unlink()
        (self.root/'Settlement_Locks.json').unlink()
        with self.assertRaisesRegex(ValueError,'Backup'):
            core.sync_status(core.load_master_data())

    def test_empty_sku_suffix_valid_unknown_partner_blocked(self):
        self.seed('NB /')
        row=core.load_master_data().iloc[0]
        self.assertEqual(row.SKU,'NB /')
        self.assertEqual(row['Prüfhinweis'],'')
        with patch.object(core,'known_group_b_partners',return_value=set()):
            self.assertIn('unbekannter Partner',core.load_master_data().iloc[0]['Prüfhinweis'])

    def test_order_identity_enrichment_and_conflict(self):
        first=payout()
        second=first.copy()
        second['Artikelnummer']='item'
        core.import_reports([first],core.ORDERS_DB_PATH,'orders')
        self.assertEqual(core.import_reports([second],core.ORDERS_DB_PATH,'orders'),0)
        self.assertEqual(len(core.read_master(core.ORDERS_DB_PATH)),1)
        before=Path(core.ORDERS_DB_PATH).read_bytes()
        self.assertEqual(core.import_reports([second],core.ORDERS_DB_PATH,'orders'),0)
        self.assertEqual(before,Path(core.ORDERS_DB_PATH).read_bytes())
        second['SKU']='MH /'
        with self.assertRaisesRegex(ValueError,'Widersprüchliche'):
            core.import_reports([second],core.ORDERS_DB_PATH,'orders')
        self.assertEqual(before,Path(core.ORDERS_DB_PATH).read_bytes())

    def test_duplicate_receipt_status_and_order_gap(self):
        self.seed()
        self.lock()
        upload=Upload(payout().to_csv(sep=';',index=False).encode('utf-8'))
        receipt=data_status.import_file(upload,'payout')
        self.assertEqual((receipt['added'],receipt['present'],receipt['error']),(0,1,''))
        self.assertTrue(receipt['payouts'][0]['locked'])
        for day, transaction in [('31.08.2026','t2'),('03.09.2026','t3')]:
            frame=payout(transaction=transaction)
            frame['Verkauft am']=day
            result=data_status.import_file(Upload(frame.to_csv(sep=';',index=False).encode('utf-8'),'orders.csv'),'orders')
            self.assertFalse(result['error'])
        state=core.sync_status(core.load_master_data())
        overview=data_status.overview(core.load_master_data(),state)
        self.assertEqual(overview['order_end'],'03.09.2026')
        self.assertEqual(len(overview['gaps']),1)


if __name__=='__main__':
    unittest.main()
