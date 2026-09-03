import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
import core
import position_workflow as workflow
import studio_view
from test_recovery import payout


class PositionWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        paths=patch.multiple(core,PAYOUTS_DB_PATH=str(self.root/'Master_Payouts.csv'),ORDERS_DB_PATH=str(self.root/'Master_Orders.csv'))
        paths.start(); self.addCleanup(paths.stop)

    def seed(self, sku, payout_id='p1', invoice=False):
        frame=payout(payout_id,sku=sku)
        frame['Auszahlungsstatus']='Betrag überwiesen'
        core.import_reports([frame],core.ORDERS_DB_PATH,'orders')
        core.import_reports([frame],core.PAYOUTS_DB_PATH,'payout')
        master=core.load_master_data(); core.sync_status(master)
        if invoice:
            with core.ledger() as db:
                db.execute("UPDATE payouts SET attempt='created',invoice_id='draft',status='Lexoffice-Entwurf erstellt' WHERE id=?",(payout_id,))
                db.commit()
        return master

    def test_group_a_ignores_lexware_payout_lock_and_closes_after_payment(self):
        master=self.seed('BA / 1',invoice=True)
        business=workflow.positions(master,core.sync_status(master))
        row=business.iloc[0]
        self.assertFalse(row.Lexware_uebertragen)
        self.assertEqual(row.Bearbeitungsstatus,'abrechnungsbereit')
        self.assertTrue(row.partner_ready)
        today=date.today()
        workflow.confirm([row.position_key],'review',today)
        business=workflow.positions(); row=business.iloc[0]
        self.assertEqual(row.Bearbeitungsstatus,'Rechnung/Abrechnung geprüft')
        workflow.confirm([row.position_key],'partner_paid',today)
        row=workflow.positions().iloc[0]
        self.assertEqual(row.Bearbeitungsstatus,'abgeschlossen')
        self.assertEqual(row.closed_at,today.isoformat())
        self.assertEqual(workflow.payout_status(workflow.positions())['p1'],'abgeschlossen')
        self.assertTrue(studio_view.partner_rows(workflow.positions()).empty)

    def test_re0089_means_transferred_but_not_paid_or_closed(self):
        master=self.seed('NB / 1',invoice=True)
        row=workflow.positions(master,core.sync_status(master)).iloc[0]
        self.assertTrue(row.Lexware_uebertragen)
        self.assertEqual(row.Bearbeitungsstatus,'in Bearbeitung · Lexware-Entwurf erstellt')
        self.assertEqual((row.Partnerzahlung,row.Evelyn_Zahlung,row.closed_at),('offen','offen',''))
        self.assertEqual(workflow.payout_status(workflow.positions())['p1'],'teilweise in Bearbeitung')
        self.assertTrue(row.partner_ready)

    def test_group_b_requires_both_independent_payment_paths(self):
        self.seed('NB / 1',invoice=True)
        row=workflow.positions().iloc[0]; today=date.today()
        workflow.confirm([row.position_key],'review',today)
        workflow.confirm([row.position_key],'partner_paid',today)
        row=workflow.positions().iloc[0]
        self.assertFalse(row.closed_at)
        self.assertEqual((row.Partnerzahlung,row.Evelyn_Zahlung),('bezahlt','offen'))
        workflow.confirm([row.position_key],'evelyn_received',today)
        row=workflow.positions().iloc[0]
        self.assertEqual(row.Bearbeitungsstatus,'abgeschlossen')
        self.assertEqual(workflow.payout_status(workflow.positions())['p1'],'abgeschlossen')

    def test_group_b_evelyn_received_first_stays_partial(self):
        self.seed('NB / 1',invoice=True)
        row=workflow.positions().iloc[0]; today=date.today()
        workflow.confirm([row.position_key],'evelyn_received',today)
        self.assertEqual(workflow.positions().iloc[0].Bearbeitungsstatus,'teilweise bezahlt / erhalten')
        self.assertEqual(workflow.payout_status(workflow.positions())['p1'],'teilweise in Bearbeitung')

    def test_invalid_order_and_future_date_fail_closed(self):
        self.seed('NB / 1')
        row=workflow.positions().iloc[0]
        with self.assertRaisesRegex(ValueError,'Zuerst'):
            workflow.confirm([row.position_key],'partner_paid',date.today())
        with self.assertRaisesRegex(ValueError,'zukünftiges'):
            workflow.confirm([row.position_key],'review',date.today()+timedelta(days=1))
        with self.assertRaisesRegex(ValueError,'bereits übertragene'):
            workflow.confirm([row.position_key],'evelyn_received',date.today())

    def test_workflow_sidecar_restores_completed_state(self):
        self.seed('BA / 1')
        row=workflow.positions().iloc[0]; today=date.today()
        workflow.confirm([row.position_key],'review',today)
        workflow.confirm([row.position_key],'partner_paid',today)
        self.assertTrue((self.root/'Settlement_Workflow.json').exists())
        (self.root/'Settlement_State.sqlite3').unlink()
        self.assertEqual(workflow.positions().iloc[0].Bearbeitungsstatus,'abgeschlossen')

    def test_payout_mixed_positions_is_partial_until_all_closed(self):
        first=payout('p1','t1','o1',sku='BA / 1'); second=payout('p1','t2','o2',sku='BA / 2')
        for frame in (first,second): frame['Auszahlungsstatus']='Betrag überwiesen'
        core.import_reports([first,second],core.ORDERS_DB_PATH,'orders'); core.import_reports([first,second],core.PAYOUTS_DB_PATH,'payout')
        rows=workflow.positions(); today=date.today(); key=rows.iloc[0].position_key
        workflow.confirm([key],'review',today); workflow.confirm([key],'partner_paid',today)
        self.assertEqual(workflow.payout_status(workflow.positions())['p1'],'teilweise in Bearbeitung')


if __name__=='__main__': unittest.main()
