import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import core
import draft_correction
import api_holds
import position_workflow as workflow
from test_recovery import payout
from test_invoice_support import create_matching_invoice
from test_api_holds import snapshot, movement


class TestDiscardTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        paths=patch.multiple(core,PAYOUTS_DB_PATH=str(self.root/'Master_Payouts.csv'),ORDERS_DB_PATH=str(self.root/'Master_Orders.csv'))
        paths.start();self.addCleanup(paths.stop)
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('No HTTP'))
        network.start();self.addCleanup(network.stop)
        frames=[payout('old',str(i),'o'+str(i)) for i in range(37)]+[payout('other','other','other')]
        for frame in frames:
            frame['Auszahlungsstatus']='Betrag überwiesen'
            frame['Transaktionsbetrag (inkl. Kosten)']=frame['Betrag abzügl. Kosten']
            frame['Auszahlungsdatum']='03.09.2026'
        core.import_reports(frames,core.ORDERS_DB_PATH,'orders');core.import_reports(frames,core.PAYOUTS_DB_PATH,'payout')
        master=core.load_master_data();core.sync_status(master)
        payload=core.build_invoice_payload(master,'old','contact',True)
        with core.ledger() as db:
            db.execute("UPDATE payouts SET invoice_id=?,attempt='created',snapshot=?,fingerprint=? WHERE id='old'",(draft_correction.RE0089_ID,json.dumps(payload),core.payout_fingerprint(master[master['Auszahlung Nr.']=='old'])))
            db.commit()

    def discard(self):
        return draft_correction.discard_re0089_test(confirmed_test_only=True,actor='Test',reason='Nachweislich ausschließlich Testentwurf')

    def test_discard_retains_history_hold_and_unrelated_status_even_after_restore(self):
        api_holds.ingest(self.root,snapshot([movement(order='o0')]))
        original=(self.root/'Settlement_Locks.json').read_bytes()
        holds=(self.root/api_holds.FILE).read_bytes()
        with core.ledger() as db:
            other=dict(db.execute("SELECT * FROM payouts WHERE id='other'").fetchone())
        self.assertEqual(self.discard(),37)
        with core.ledger() as db:
            record=db.execute('SELECT * FROM discarded_invoices').fetchone()
            self.assertIn('Testbeleg',record['label'])
            self.assertEqual(len(json.loads(json.loads(record['snapshot'])[0]['snapshot'])['lineItems']),37)
            self.assertEqual(dict(db.execute("SELECT * FROM payouts WHERE id='other'").fetchone()),other)
        rows=workflow.positions()
        self.assertFalse(rows.Lexware_uebertragen.any())
        self.assertFalse(rows[rows.Bestellnummer=='o0'].partner_ready.any())
        self.assertEqual((self.root/api_holds.FILE).read_bytes(),holds)
        with self.assertRaises(ValueError):self.discard()
        (self.root/'Settlement_Locks.json').write_bytes(original)
        (self.root/'Settlement_State.sqlite3').unlink()
        self.assertFalse(workflow.positions().Lexware_uebertragen.any())
        self.assertFalse(workflow.positions().query("Bestellnummer=='o0'").partner_ready.any())

    def test_uploaded_unapproved_invoice_blocks(self):
        create_matching_invoice('NB')
        with self.assertRaisesRegex(ValueError,'Hochgeladener Beleg'):self.discard()

    def status_blocks(self,field):
        row=workflow.positions().query("Bestellnummer=='o0'").iloc[0]
        with core.ledger() as db:
            db.execute(f'INSERT INTO position_workflow(position_key,{field},source) VALUES(?,?,?)',(row.position_key,'2026-09-04',workflow.source_snapshot(row)))
            db.commit()
        with self.assertRaisesRegex(ValueError,'Prüfung, Zahlung oder Abschluss'):self.discard()
        self.assertTrue(workflow.positions().Lexware_uebertragen.any())

    def test_review_blocks(self):self.status_blocks('reviewed_at')
    def test_partner_payment_blocks(self):self.status_blocks('paid_at')
    def test_evelyn_payment_blocks(self):self.status_blocks('received_at')
    def test_completion_blocks(self):self.status_blocks('closed_at')

    def test_explicit_test_confirmation_required(self):
        with self.assertRaises(ValueError):draft_correction.discard_re0089_test()


if __name__=='__main__':unittest.main()
