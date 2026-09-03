"""Offline workflow regressions: isolated stores, mocked HTTP only."""
import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch
import core
import draft_correction
import position_workflow as workflow
import studio_view
from test_recovery import payout


class WeeklyFinishTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        paths = patch.multiple(core, PAYOUTS_DB_PATH=str(self.root/'Master_Payouts.csv'), ORDERS_DB_PATH=str(self.root/'Master_Orders.csv'))
        paths.start(); self.addCleanup(paths.stop)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('Live HTTP forbidden'))
        network.start(); self.addCleanup(network.stop)

    def seed(self, frames):
        for frame in frames:
            frame['Transaktionsbetrag (inkl. Kosten)'] = frame['Betrag abzügl. Kosten']
            frame['Auszahlungsdatum'] = '03.09.2026'
            frame['Auszahlungsstatus'] = 'Betrag überwiesen'
        core.import_reports(frames, core.ORDERS_DB_PATH, 'orders')
        core.import_reports(frames, core.PAYOUTS_DB_PATH, 'payout')
        return core.load_master_data()

    def draft(self):
        self.seed([payout('p1','t1','o1',sku='NB / 1'), payout('p1','t2','o2',sku='BA / 1')])
        master = core.load_master_data()
        core.sync_status(master)
        payload = core.build_invoice_payload(master,'p1','contact',True)
        with core.ledger() as db:
            db.execute("UPDATE payouts SET invoice_id='draft',attempt='created',snapshot=?,fingerprint=? WHERE id='p1'", (json.dumps(payload),core.payout_fingerprint(master)))
            db.commit()
        http = Mock()
        http.get.side_effect = [Mock(status_code=200, json=lambda: {'id':'contact','roles':{'customer':{'number':16335}}}), Mock(status_code=404)]
        return http

    def test_hold_does_not_block_payout_and_followup_is_normal(self):
        sale = payout('p1','t1','o1')
        hold = payout('p1','t2','o2',amount='-48,99',kind='Einbehalten')
        master = self.seed([sale,hold])
        self.assertEqual(len(master),1)
        self.assertFalse(master['Prüfhinweis'].astype(bool).any())
        self.assertEqual(len(core.build_invoice_payload(master,'p1','contact',True)['lineItems']),1)
        self.assertEqual(len(studio_view.holds(core.read_master(core.PAYOUTS_DB_PATH))),1)
        self.assertEqual(len(studio_view.eligible_rows(master,core.sync_status(master))),1)
        follow = payout('p2','t2','o2',amount='48,99')
        core.import_reports([follow],core.PAYOUTS_DB_PATH,'payout')
        self.assertEqual(len(core.load_master_data()),2)
        refund = payout('p3','t2','o2',amount='-48,99',kind='Rückerstattung')
        core.import_reports([refund],core.PAYOUTS_DB_PATH,'payout')
        self.assertEqual((core.load_master_data().Art=='Erstattung').sum(),1)

    def test_dashboard_net_commission_scope_and_refunds(self):
        frames=[payout('p1','a','a',sku='BA / 1'),payout('p1','b','b'),payout('p1','r','r',amount='-59,50',kind='Rückerstattung'),payout('p1','h','h',amount='-48,99',kind='Einbehalten'),payout('','open','open')]
        totals=studio_view.project_totals(self.seed(frames))
        self.assertEqual(totals,dict(ebay=Decimal('178.50'),evelyn=Decimal('.75'),patrick=Decimal('1.50')))

    def test_discard_preserves_partner_status_history_and_restore(self):
        http=self.draft()
        rows=workflow.positions()
        workflow.confirm(rows.position_key.tolist(),'review',date.today())
        before={r.position_key:r.reviewed_at for _,r in workflow.positions().iterrows()}
        old_guard=(self.root/'Settlement_Locks.json').read_bytes()
        self.assertEqual(draft_correction.discard('mock-key','draft',True,http),1)
        http.post.assert_not_called(); http.delete.assert_not_called()
        self.assertFalse(workflow.positions().Lexware_uebertragen.any())
        self.assertEqual({r.position_key:r.reviewed_at for _,r in workflow.positions().iterrows()},before)
        self.assertTrue(studio_view.invoice_history()['draft']['discarded'])
        # A stale pre-correction lock mirror must not resurrect the discarded draft.
        (self.root/'Settlement_Locks.json').write_bytes(old_guard)
        (self.root/'Settlement_State.sqlite3').unlink()
        states=core.sync_status(core.load_master_data())
        self.assertTrue(states.Entwurf.isna().all())
        self.assertTrue(studio_view.invoice_history()['draft']['discarded'])
        self.assertEqual(len(studio_view.eligible_rows(core.load_master_data(),states)),2)
        with self.assertRaises(ValueError):
            draft_correction.discard('mock-key','draft',True,http)

    def test_existing_invoice_error_timeout_never_releases(self):
        http=self.draft()
        contact=Mock(status_code=200,json=lambda:{'id':'contact','roles':{'customer':{'number':16335}}})
        for result in (Mock(status_code=200),Mock(status_code=403),Mock(status_code=500),TimeoutError()):
            with self.subTest(response=result):
                http.get.side_effect=[contact,result]
                with self.assertRaises(ValueError):
                    draft_correction.discard('mock-key','draft',True,http)
                self.assertTrue(workflow.positions().Lexware_uebertragen.any())
                self.assertFalse(studio_view.invoice_history()['draft']['discarded'])
        http.post.assert_not_called(); http.delete.assert_not_called()

    def test_wrong_contact_and_received_payment_cannot_discard(self):
        http=self.draft()
        http.get.side_effect=[Mock(status_code=200,json=lambda:{'id':'other'})]
        with self.assertRaises(ValueError):
            draft_correction.discard('mock-key','draft',True,http)
        self.assertEqual(http.get.call_count,1)
        row=workflow.positions().query("Gruppe == 'Gruppe B'").iloc[0]
        workflow.confirm([row.position_key],'evelyn_received',date.today())
        with self.assertRaisesRegex(ValueError,'Bereits erhaltene'):
            draft_correction.discard('mock-key','draft',True,http)
        self.assertEqual(http.get.call_count,1)

    def test_ui_concrete_confirmation_and_completed_archive(self):
        from streamlit.testing.v1 import AppTest
        self.seed([payout('p1','a','a',sku='BA / 1')])
        app=AppTest.from_file('app.py').run(timeout=30)
        self.assertFalse(app.exception)
        self.assertIn('Dashboard',[t.label for t in app.tabs])
        self.assertNotIn('Prüfung & Zahlungen manuell bestätigen',[e.label for e in app.expander])
        review=next(b for b in app.button if b.label=='Partnerrechnung geprüft bestätigen')
        review.click().run()
        self.assertFalse(workflow.positions().reviewed_at.astype(bool).any())
        next(b for b in app.button if b.label=='Verbindlich bestätigen').click().run()
        self.assertTrue(workflow.positions().reviewed_at.astype(bool).all())
        next(b for b in app.button if b.label=='Bezahlt / abgeschlossen').click().run()
        next(b for b in app.button if b.label=='Verbindlich bestätigen').click().run()
        self.assertFalse(app.exception)
        self.assertTrue(workflow.positions().closed_at.astype(bool).all())
        self.assertNotIn('Bezahlt / abgeschlossen',[b.label for b in app.button])
        self.assertFalse(list(app.get('download_button'))[1:]) # backup only
        self.assertEqual(workflow.payout_status(workflow.positions())['p1'],'abgeschlossen')

    def test_ui_one_weekly_statement_for_partner_across_payouts(self):
        from streamlit.testing.v1 import AppTest
        self.seed([payout('p1','a','a',sku='BA / 1'),payout('p2','b','b',sku='BA / 2')])
        app=AppTest.from_file('app.py').run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual([b.label for b in app.get('download_button')].count('Einzelabrechnung herunterladen'),1)
        self.assertNotIn('Teilabrechnung herunterladen',[b.label for b in app.get('download_button')])
        self.assertEqual([b.label for b in app.button].count('Partnerrechnung geprüft bestätigen'),1)
        self.assertFalse([s.label for s in app.selectbox if s.label=='Payouts für die Gesamtrechnung'])

    def test_group_b_primary_action_stays_visible_without_authorization(self):
        from streamlit.testing.v1 import AppTest
        self.seed([payout('p1','a','a',sku='NB / 1')])
        app=AppTest.from_file('app.py').run(timeout=30)
        self.assertFalse(app.exception)
        action=next(button for button in app.button if button.label=='An Lexware übermitteln')
        self.assertTrue(action.disabled)
        self.assertEqual([b.label for b in app.button].count('An Lexware übermitteln'),1)
        self.assertIn('Lexware-Aktion',[heading.value for heading in app.subheader])
        labels=[metric.label for metric in app.metric]
        for label in ('Offene Positionen','Abrechnungsbasis netto','Rabatt 0,5 % netto','Auszahlungsbetrag brutto'):
            self.assertIn(label,labels)

    def test_evelyn_payment_is_checkbox_with_existing_confirmation(self):
        from streamlit.testing.v1 import AppTest
        self.draft()
        app=AppTest.from_file('app.py').run(timeout=30)
        self.assertFalse(app.exception)
        self.assertNotIn('Zahlung von Evelyn erhalten',[button.label for button in app.button])
        payment=next(box for box in app.checkbox if box.label=='Zahlung von Evelyn erhalten')
        payment.set_value(True).run()
        self.assertFalse(workflow.positions().received_at.astype(bool).any())
        self.assertIn('Verbindlich bestätigen',[button.label for button in app.button])
        next(button for button in app.button if button.label=='Verbindlich bestätigen').click().run()
        self.assertTrue(workflow.positions().query("Gruppe == 'Gruppe B'").received_at.astype(bool).all())


if __name__=='__main__':
    unittest.main()
