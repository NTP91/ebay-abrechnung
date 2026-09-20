import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import core
import position_workflow as workflow
import partner_invoices
from test_recovery import payout
from test_invoice_support import review_positions, invoice_csv


class PaidWithoutInvoiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        paths = patch.multiple(core, PAYOUTS_DB_PATH=str(self.root / 'Master_Payouts.csv'),
                                ORDERS_DB_PATH=str(self.root / 'Master_Orders.csv'))
        paths.start(); self.addCleanup(paths.stop)

    def seed(self, sku, payout_id, order='o1', transaction='t1'):
        frame = payout(payout_id, transaction=transaction, order=order, sku=sku)
        frame['Auszahlungsstatus'] = 'Betrag überwiesen'
        core.import_reports([frame], core.ORDERS_DB_PATH, 'orders')
        core.import_reports([frame], core.PAYOUTS_DB_PATH, 'payout')
        return core.load_master_data()

    def assign_round(self, position_key, round_id):
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            db.execute('INSERT OR IGNORE INTO group_b_rounds VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (round_id, 2026, 1, 'test', None, None, '0', 'hash-' + round_id, '{}', '2026-01-01T00:00:00Z'))
            db.execute('INSERT OR REPLACE INTO group_b_round_positions VALUES(?,?,?,?)',
                       (position_key, round_id, 'evelyn_invoice', 'test'))
            db.commit()

    def test_marks_position_not_partner_ready_and_leaves_reviewed_at_empty(self):
        master = self.seed('MH / 1', 'p1')
        row = workflow.positions(master, core.sync_status(master)).iloc[0]
        self.assertTrue(row.partner_ready)
        self.assign_round(row.position_key, 'GB-2026-001')
        workflow.mark_paid_without_invoice([row.position_key], date.today(), 'MH', {'GB-2026-001'}, {'p1'}, 'tester', 'historischer MH-Fall')
        row = workflow.positions().iloc[0]
        self.assertFalse(row.partner_ready)
        self.assertEqual(row.reviewed_at, '')
        self.assertEqual(row.paid_at, '')
        self.assertEqual(row.Bearbeitungsstatus, 'bezahlt · Rechnung fehlt')
        self.assertEqual(row.Partnerzahlung, 'bezahlt')
        self.assertEqual(row.Partnerrechnung, 'noch nicht geprüft')

    def test_later_invoice_can_review_but_never_a_second_payment(self):
        # Mirrors partner_invoices.upload()'s own real eligibility filter (not
        # partner_ready): closed_at/paid_at/Prüfhinweis/Quellenpruefung/hold-free
        # and not yet reviewed. A paid_without_invoice_at position passes this
        # filter in production, so a later real MH invoice can still match it.
        master = self.seed('MH / 1', 'p1')
        row = workflow.positions(master, core.sync_status(master)).iloc[0]
        self.assign_round(row.position_key, 'GB-2026-001')
        workflow.mark_paid_without_invoice([row.position_key], date.today(), 'MH', {'GB-2026-001'}, {'p1'}, 'tester', 'note')
        raw = core.read_master(core.PAYOUTS_DB_PATH)
        raw['Transaktionsbetrag (inkl. Kosten)'] = raw['Betrag abzügl. Kosten']
        raw['Auszahlungsdatum'] = '03.09.2026'
        raw.to_csv(core.PAYOUTS_DB_PATH, sep=';', index=False, encoding='utf-8-sig')
        rows = workflow.positions()
        eligible = rows[(rows.Partner == 'MH') & (rows.Art == 'Bestellung') & ~rows.closed_at.astype(bool)
                        & ~rows.paid_at.astype(bool) & ~rows['Prüfhinweis'].astype(bool)
                        & ~rows.Quellenpruefung.astype(bool) & ~rows.reviewed_at.astype(bool)]
        self.assertEqual(len(eligible), 1)
        expected = partner_invoices.expected_statement(eligible)
        record, _ = partner_invoices.upload('MH', 'invoice.csv', invoice_csv(expected))
        partner_invoices.approve(record['id'], 'tester')
        row = workflow.positions().iloc[0]
        self.assertTrue(row.reviewed_at)
        self.assertEqual(row.Bearbeitungsstatus, 'Rechnung/Abrechnung geprüft')
        with self.assertRaisesRegex(ValueError, 'zweite Zahlung'):
            workflow.confirm([row.position_key], 'partner_paid', date.today())
        row = workflow.positions().iloc[0]
        self.assertEqual(row.paid_at, '')

    def test_refund_positions_never_become_payable_regardless_of_marker(self):
        master = self.seed('MH / 1', 'p1', order='o-sale', transaction='t-sale')
        frame = payout('p1', transaction='t-refund', order='o-sale', sku='MH / 1', amount='-10,00', kind='Rückerstattung')
        core.import_reports([frame], core.PAYOUTS_DB_PATH, 'payout')
        rows = workflow.positions()
        refund_row = rows[rows.Art == 'Erstattung'].iloc[0]
        self.assertFalse(refund_row.partner_ready)
        self.assertEqual(refund_row.Bearbeitungsstatus, 'Erstattung zu klären')

    def test_unrelated_position_stays_payable(self):
        self.seed('MH / 1', 'p1', order='o1', transaction='t1')
        second = payout('p2', transaction='t2', order='o2', sku='MH / 2')
        second['Auszahlungsstatus'] = 'Betrag überwiesen'
        core.import_reports([second], core.ORDERS_DB_PATH, 'orders')
        core.import_reports([second], core.PAYOUTS_DB_PATH, 'payout')
        rows = workflow.positions()
        marked_row = rows[rows['Auszahlung Nr.'] == 'p1'].iloc[0]
        other_row = rows[rows['Auszahlung Nr.'] == 'p2'].iloc[0]
        self.assign_round(marked_row.position_key, 'GB-2026-001')
        workflow.mark_paid_without_invoice([marked_row.position_key], date.today(), 'MH', {'GB-2026-001'}, {'p1'}, 'tester', 'note')
        rows = workflow.positions()
        self.assertFalse(rows[rows['Auszahlung Nr.'] == 'p1'].iloc[0].partner_ready)
        self.assertTrue(rows[rows['Auszahlung Nr.'] == 'p2'].iloc[0].partner_ready)

    def test_other_partners_unaffected(self):
        self.seed('MH / 1', 'p1', order='o1', transaction='t1')
        other = payout('p3', transaction='t3', order='o3', sku='BA / 1')
        other['Auszahlungsstatus'] = 'Betrag überwiesen'
        core.import_reports([other], core.ORDERS_DB_PATH, 'orders')
        core.import_reports([other], core.PAYOUTS_DB_PATH, 'payout')
        rows = workflow.positions()
        mh_row = rows[rows.Partner == 'MH'].iloc[0]
        ba_row = rows[rows.Partner == 'BA'].iloc[0]
        self.assign_round(mh_row.position_key, 'GB-2026-001')
        workflow.mark_paid_without_invoice([mh_row.position_key], date.today(), 'MH', {'GB-2026-001'}, {'p1'}, 'tester', 'note')
        rows = workflow.positions()
        self.assertFalse(rows[rows.Partner == 'MH'].iloc[0].partner_ready)
        self.assertTrue(rows[rows.Partner == 'BA'].iloc[0].partner_ready)

    def test_normal_group_a_review_and_payment_flow_is_unchanged(self):
        master = self.seed('BA / 1', 'p1')
        row = workflow.positions(master, core.sync_status(master)).iloc[0]
        review_positions([row.position_key])
        workflow.confirm([row.position_key], 'partner_paid', date.today())
        row = workflow.positions().iloc[0]
        self.assertEqual(row.Bearbeitungsstatus, 'abgeschlossen')

    def test_abort_on_wrong_partner(self):
        master = self.seed('NB / 1', 'p1')
        row = workflow.positions(master, core.sync_status(master)).iloc[0]
        self.assign_round(row.position_key, 'GB-2026-001')
        with self.assertRaisesRegex(ValueError, 'Partner'):
            workflow.mark_paid_without_invoice([row.position_key], date.today(), 'MH', {'GB-2026-001'}, {'p1'}, 'tester', 'note')
        self.assertFalse(workflow.positions().iloc[0].get(workflow.PAID_WITHOUT_INVOICE, ''))

    def test_abort_on_wrong_payout_number(self):
        master = self.seed('MH / 1', 'p9')
        row = workflow.positions(master, core.sync_status(master)).iloc[0]
        self.assign_round(row.position_key, 'GB-2026-001')
        with self.assertRaisesRegex(ValueError, 'Payoutnummern'):
            workflow.mark_paid_without_invoice([row.position_key], date.today(), 'MH', {'GB-2026-001'}, {'p1'}, 'tester', 'note')

    def test_abort_on_wrong_round(self):
        master = self.seed('MH / 1', 'p1')
        row = workflow.positions(master, core.sync_status(master)).iloc[0]
        self.assign_round(row.position_key, 'GB-2026-003')
        with self.assertRaisesRegex(ValueError, 'Runden'):
            workflow.mark_paid_without_invoice([row.position_key], date.today(), 'MH', {'GB-2026-001'}, {'p1'}, 'tester', 'note')

    def test_double_marking_rejected(self):
        master = self.seed('MH / 1', 'p1')
        row = workflow.positions(master, core.sync_status(master)).iloc[0]
        self.assign_round(row.position_key, 'GB-2026-001')
        workflow.mark_paid_without_invoice([row.position_key], date.today(), 'MH', {'GB-2026-001'}, {'p1'}, 'tester', 'note')
        with self.assertRaisesRegex(ValueError, 'bereits historisch'):
            workflow.mark_paid_without_invoice([row.position_key], date.today(), 'MH', {'GB-2026-001'}, {'p1'}, 'tester', 'note')

    def test_already_normally_paid_position_cannot_be_marked(self):
        master = self.seed('MH / 1', 'p1')
        row = workflow.positions(master, core.sync_status(master)).iloc[0]
        review_positions([row.position_key])
        workflow.confirm([row.position_key], 'partner_paid', date.today())
        row = workflow.positions().iloc[0]
        self.assign_round(row.position_key, 'GB-2026-001')
        with self.assertRaisesRegex(ValueError, 'regulär als bezahlt'):
            workflow.mark_paid_without_invoice([row.position_key], date.today(), 'MH', {'GB-2026-001'}, {'p1'}, 'tester', 'note')


if __name__ == '__main__':
    unittest.main()
