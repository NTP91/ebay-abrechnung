import json
import tempfile
import unittest
import zipfile
import io
from pathlib import Path
from unittest.mock import patch
import core
import payout_reconciliation as manual
import position_workflow
from test_recovery import payout


class ManualPayoutTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        paths=patch.multiple(core,PAYOUTS_DB_PATH=str(self.root/'Master_Payouts.csv'),ORDERS_DB_PATH=str(self.root/'Master_Orders.csv'))
        paths.start();self.addCleanup(paths.stop)
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('No HTTP'))
        network.start();self.addCleanup(network.stop)
        self.pid='7700379513'
        frames=[payout(transaction='t1',order='o1',amount='119.00'),payout(transaction='t2',order='o2',amount='238.00'),payout(transaction='hold',order='other',amount='-19.00',kind='Einbehalten')]
        for f in frames:
            f['Datum']='04.09.2026';f['Auszahlungsdatum']='04.09.2026'
            f['Transaktionsbetrag (inkl. Kosten)']=f['Betrag abzügl. Kosten']
        core.import_reports(frames[:2],core.ORDERS_DB_PATH,'orders')
        core.import_reports(frames,core.PAYOUTS_DB_PATH,'payout')
        core.sync_status(core.load_master_data())

    def save(self,statuses=('freigegeben','einbehalten','freigegeben'),bank='100.00',state=None):
        state=state or manual.inspect(self.pid)
        choices=dict(zip(state['financial'].abgleich_key,statuses))
        return manual.save(self.pid,bank,choices,'Testperson','Banknachweis kontrolliert',state['version'],state['source_digest'])

    def test_match_excludes_hold_and_preserves_rows(self):
        before=Path(core.PAYOUTS_DB_PATH).read_bytes()
        result=self.save()
        self.assertTrue(result['matched']);self.assertEqual(str(result['included']),'100.00')
        self.assertEqual(result['difference'],0)
        rows=position_workflow.positions()
        self.assertTrue(rows.loc[rows.Bestellnummer=='o1','partner_ready'].iloc[0])
        self.assertFalse(rows.loc[rows.Bestellnummer=='o2','partner_ready'].iloc[0])
        self.assertEqual(Path(core.PAYOUTS_DB_PATH).read_bytes(),before)
        self.assertEqual(len(core.read_master(core.PAYOUTS_DB_PATH)),3)
        self.assertEqual(len(manual.load()['audit']),1)

    def test_later_release_updates_same_position(self):
        self.save()
        before=Path(core.PAYOUTS_DB_PATH).read_bytes()
        keys=position_workflow.positions().position_key.tolist()
        result=self.save(('freigegeben',)*3,'338.00')
        self.assertTrue(result['matched'])
        self.assertTrue(position_workflow.positions().partner_ready.all())
        self.assertEqual(position_workflow.positions().position_key.tolist(),keys)
        self.assertEqual(Path(core.PAYOUTS_DB_PATH).read_bytes(),before)
        self.assertEqual(len(manual.load()['payouts']),1)
        self.assertEqual(len(manual.load()['audit']),2)

    def test_difference_and_unknown_never_release(self):
        state=self.save(bank='99.99')
        self.assertEqual(str(state['difference']),'0.01')
        self.assertFalse(state['matched'])
        self.assertFalse(position_workflow.positions().partner_ready.any())
        state=self.save(('freigegeben','unklar','freigegeben'))
        self.assertEqual(state['difference'],0)
        self.assertFalse(state['matched'])
        self.assertFalse(position_workflow.positions().partner_ready.any())

    def test_reserved_or_completed_cannot_be_changed(self):
        for field,value in [('attempt','reserved'),('invoice_id','RE0089')]:
            with core.ledger() as db:
                db.execute(f'UPDATE payouts SET {field}=?',(value,));db.commit()
            with self.assertRaisesRegex(ValueError,'Lexware'):self.save()
        self.assertFalse(manual.load()['payouts'])

    def test_completed_workflow_is_preserved(self):
        row=position_workflow.positions().iloc[0]
        with core.ledger() as db:
            db.execute('INSERT INTO position_workflow(position_key,closed_at,source) VALUES(?,?,?)',(row.position_key,'2026-09-01',position_workflow.source_snapshot(row)));db.commit()
        with self.assertRaisesRegex(ValueError,'Abschluss'):self.save()
        self.assertEqual(position_workflow.positions().iloc[0].closed_at,'2026-09-01')

    def test_source_and_concurrent_edits_block(self):
        old=manual.inspect(self.pid);self.save()
        with self.assertRaisesRegex(ValueError,'verändert'):self.save(state=old)
        frame=core.read_master(core.PAYOUTS_DB_PATH);frame.loc[0,'Betrag abzügl. Kosten']='120.00'
        frame.to_csv(core.PAYOUTS_DB_PATH,sep=';',index=False)
        result=manual.inspect(self.pid)
        self.assertFalse(result['matched']);self.assertEqual(result['unknown'],1)
        self.assertFalse(position_workflow.positions().partner_ready.any())

    def test_new_position_needs_new_manual_decision(self):
        self.save()
        core.import_reports([payout(transaction='new',order='new')],core.PAYOUTS_DB_PATH,'payout')
        self.assertFalse(manual.inspect(self.pid)['matched'])
        self.assertEqual(manual.inspect(self.pid)['unknown'],1)

    def test_backup_and_redundant_storage(self):
        self.save();expected=manual.load()
        with zipfile.ZipFile(io.BytesIO(core.backup_data())) as z:
            self.assertIn(manual.FILE,z.namelist())
        (self.root/manual.FILE).unlink()
        self.assertEqual(manual.load(),expected)
        (self.root/manual.FILE).write_text(json.dumps(expected),encoding='utf-8')
        (self.root/'Settlement_State.sqlite3').unlink()
        self.assertEqual(manual.load(),expected)
        self.assertFalse(position_workflow.positions().loc[lambda x:x.Bestellnummer=='o2','partner_ready'].iloc[0])

    def test_existing_invoice_review_cannot_bypass_hold(self):
        self.save()
        row=position_workflow.positions().loc[lambda x:x.Bestellnummer=='o2'].iloc[0]
        with self.assertRaises(ValueError):
            position_workflow.confirm([row.position_key],'partner_paid','2026-09-01')

    def test_ui_renders_saved_difference(self):
        self.save(bank='99.99')
        from streamlit.testing.v1 import AppTest
        app=AppTest.from_file('app.py').run(timeout=30)
        self.assertFalse(list(app.exception))
        self.assertTrue(any(x.label=='Differenz zum Bankbetrag' and x.value=='0,01 €' for x in app.metric))

    def test_reference_49180_with_parent_children_and_return_hold(self):
        pid='7718008497'
        held_amounts=['97.98','40.99','168.98','86.99','26.90','86.99','62.99','289.80','39.90','27.00','149.96']
        frames=[]
        for i,amount in enumerate(held_amounts+['86.99','119.80','315.00']):
            f=payout(pid,transaction='' if i==10 else f'ref-{i}',order=f'ref-order-{i}',amount=amount)
            f['Transaktionsbetrag (inkl. Kosten)']=amount
            frames.append(f)
        frames.append(payout(pid,transaction='return',order='return-order',amount='-29.99',kind='Einbehalten'))
        for i,amount in enumerate(['99.98','49.98']):
            f=payout(pid,transaction=f'child-{i}',order='ref-order-10',amount='--')
            f['Artikelnummer']=f'item-{i}';f['Zwischensumme Artikel']=amount;f['Transaktionsbetrag (inkl. Kosten)']='--'
            frames.append(f)
        core.import_reports(frames,core.PAYOUTS_DB_PATH,'payout')
        state=manual.inspect(pid)
        decisions={r.abgleich_key:('einbehalten' if r.Bestellnummer in {f'ref-order-{i}' for i in range(11)} else 'freigegeben') for _,r in state['financial'].iterrows()}
        result=manual.save(pid,'491.80',decisions,'Testperson','eBay-Referenzfall',state['version'],state['source_digest'])
        self.assertTrue(result['matched'])
        self.assertEqual(str(result['positive']),'1600.27')
        self.assertEqual(str(result['held']),'1078.48')
        self.assertEqual(str(result['negative']),'-29.99')
        self.assertEqual(str(result['included']),'491.80')
        self.assertEqual(len(result['children']),2)
        self.assertEqual(len(result['financial']),15)
