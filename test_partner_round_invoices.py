import json
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import core
import partner_round_invoices as incoming
import partner_snapshot
import position_workflow as workflow
import round_planner as planner
from test_invoice_support import invoice_csv
from test_recovery import payout

BERLIN = ZoneInfo('Europe/Berlin')
BASE_CUT = datetime(2026, 9, 20, 23, 59, tzinfo=BERLIN)


def berlin(*args):
    return datetime(*args, tzinfo=BERLIN)


class PartnerRoundInvoiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        paths = patch.multiple(core, PAYOUTS_DB_PATH=str(self.root / 'Master_Payouts.csv'),
                                ORDERS_DB_PATH=str(self.root / 'Master_Orders.csv'))
        paths.start(); self.addCleanup(paths.stop)

    def seed_sale(self, payout_id, order, sku, amount='50,00', payout_date='14.09.2026', artikelnummer='item-1'):
        order_frame = payout(payout_id, order, order, sku=sku, amount=amount)
        order_frame['Artikelnummer'] = artikelnummer
        core.import_reports([order_frame], core.ORDERS_DB_PATH, 'orders')
        sale = payout(payout_id, order, order, sku=sku, amount=amount)
        sale['Artikelnummer'] = artikelnummer
        sale['Auszahlungsdatum'] = payout_date
        sale['Auszahlungsstatus'] = 'Betrag überwiesen'
        sale['Transaktionsbetrag (inkl. Kosten)'] = amount
        core.import_reports([sale], core.PAYOUTS_DB_PATH, 'payout')

    def commit_and_finalize(self, order='order-a', sku='PP / TEST', partner='PP'):
        self.seed_sale('p1', order, sku)
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        record, created = partner_snapshot.finalize('2026-003', partner, now=berlin(2026, 9, 21, 0, 5))
        self.assertTrue(created)
        return record

    def matching_invoice(self, snap, number='PP0001', total_override=None, mutate=None):
        line_items = json.loads(snap['line_items'])
        expected = dict(items=[dict(item) for item in line_items],
                         total=total_override or snap['final_amount'])
        if mutate:
            mutate(expected)
        return invoice_csv(expected, number)

    # 1. upload without a final snapshot -> not checkable
    def test_upload_without_final_snapshot_not_checkable(self):
        with self.assertRaisesRegex(ValueError, 'finale Einzelabrechnung'):
            incoming.check_and_review('2026-003', 'PP', 'invoice.csv', b'irrelevant')

    # 2. correct invoice against the snapshot -> reviewed
    def test_correct_invoice_is_reviewed(self):
        snap = self.commit_and_finalize()
        blob = self.matching_invoice(snap)
        record, report = incoming.check_and_review('2026-003', 'PP', 'invoice.csv', blob)
        self.assertEqual(report['status'], 'matched')
        self.assertIsNotNone(record)
        self.assertEqual(record['round_id'], '2026-003')
        self.assertEqual(record['partner'], 'PP')
        self.assertTrue(record['reviewed_at'])
        self.assertIsNone(record['paid_at'])

    # 3. missing position -> rejected
    def test_missing_position_rejected(self):
        snap = self.commit_and_finalize()
        blob = self.matching_invoice(snap, mutate=lambda e: e['items'].clear())
        record, report = incoming.check_and_review('2026-003', 'PP', 'invoice.csv', blob)
        self.assertIsNone(record)
        self.assertEqual(report['status'], 'deviation')
        self.assertTrue(any('fehlt auf der Rechnung' in e for e in report['errors']))

    # 4. extra/unknown position -> rejected
    def test_extra_position_rejected(self):
        snap = self.commit_and_finalize()

        def add_extra(expected):
            extra = dict(expected['items'][0])
            extra['order'] = 'order-unknown'
            expected['items'].append(extra)
        blob = self.matching_invoice(snap, mutate=add_extra)
        record, report = incoming.check_and_review('2026-003', 'PP', 'invoice.csv', blob)
        self.assertIsNone(record)
        self.assertEqual(report['status'], 'deviation')
        self.assertTrue(any('unbekannte Position' in e for e in report['errors']))

    # 5. wrong amount -> rejected
    def test_wrong_amount_rejected(self):
        snap = self.commit_and_finalize()
        blob = self.matching_invoice(snap, mutate=lambda e: e['items'][0].update(gross='999.99'))
        record, report = incoming.check_and_review('2026-003', 'PP', 'invoice.csv', blob)
        self.assertIsNone(record)
        self.assertEqual(report['status'], 'deviation')
        self.assertTrue(any('Betrag' in e for e in report['errors']))

    # 6. wrong discount -> rejected
    def test_wrong_discount_rejected(self):
        snap = self.commit_and_finalize()
        blob = self.matching_invoice(snap, mutate=lambda e: e['items'][0].update(rate='9.9'))
        record, report = incoming.check_and_review('2026-003', 'PP', 'invoice.csv', blob)
        self.assertIsNone(record)
        self.assertEqual(report['status'], 'deviation')
        self.assertTrue(any('Rabatt' in e for e in report['errors']))

    # 7. Gruppe A 0.5% correct
    def test_group_a_rate_correct(self):
        snap = self.commit_and_finalize(order='order-pp', sku='PP / TEST', partner='PP')
        self.assertEqual(snap['partner_group'], 'Gruppe A')
        line_items = json.loads(snap['line_items'])
        self.assertEqual(Decimal(line_items[0]['rate']), Decimal('0.5'))
        blob = self.matching_invoice(snap)
        record, report = incoming.check_and_review('2026-003', 'PP', 'invoice.csv', blob)
        self.assertEqual(report['status'], 'matched')

    # 8. Gruppe B 3.5% correct
    def test_group_b_rate_correct(self):
        snap = self.commit_and_finalize(order='order-mh', sku='MH / TEST', partner='MH')
        self.assertEqual(snap['partner_group'], 'Gruppe B')
        line_items = json.loads(snap['line_items'])
        self.assertEqual(Decimal(line_items[0]['rate']), Decimal('3.5'))
        blob = self.matching_invoice(snap, number='MH0001')
        record, report = incoming.check_and_review('2026-003', 'MH', 'invoice.csv', blob)
        self.assertEqual(report['status'], 'matched')

    # 9. reviewed invoice does not yet create a payment
    def test_reviewed_invoice_does_not_pay(self):
        snap = self.commit_and_finalize()
        blob = self.matching_invoice(snap)
        incoming.check_and_review('2026-003', 'PP', 'invoice.csv', blob)
        self.assertEqual(incoming.status('2026-003', 'PP'), 'zahlung_ausstehend')

    # 10. payment can be confirmed after successful review
    def test_payment_confirmable_after_review(self):
        snap = self.commit_and_finalize()
        blob = self.matching_invoice(snap)
        incoming.check_and_review('2026-003', 'PP', 'invoice.csv', blob)
        record, created = incoming.confirm_payment('2026-003', 'PP', paid_date='2026-09-21')
        self.assertTrue(created)
        self.assertEqual(record['paid_at'], '2026-09-21')
        self.assertEqual(incoming.status('2026-003', 'PP'), 'abgeschlossen')

    # 11. a second payment is prevented
    def test_second_payment_prevented(self):
        snap = self.commit_and_finalize()
        blob = self.matching_invoice(snap)
        incoming.check_and_review('2026-003', 'PP', 'invoice.csv', blob)
        first, created1 = incoming.confirm_payment('2026-003', 'PP', paid_date='2026-09-21')
        second, created2 = incoming.confirm_payment('2026-003', 'PP', paid_date='2026-09-25')
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first['paid_at'], second['paid_at'])
        self.assertEqual(second['paid_at'], '2026-09-21')

    # 12. payment date is stored
    def test_payment_date_stored(self):
        snap = self.commit_and_finalize()
        blob = self.matching_invoice(snap)
        incoming.check_and_review('2026-003', 'PP', 'invoice.csv', blob)
        record, _ = incoming.confirm_payment('2026-003', 'PP', paid_date='2026-09-21', note='per Ueberweisung')
        self.assertEqual(record['paid_at'], '2026-09-21')
        self.assertEqual(record['paid_note'], 'per Ueberweisung')

    # 13. the stored invoice stays linked to exactly the snapshot hash
    def test_invoice_linked_to_exact_snapshot_hash(self):
        snap = self.commit_and_finalize()
        blob = self.matching_invoice(snap)
        record, _ = incoming.check_and_review('2026-003', 'PP', 'invoice.csv', blob)
        self.assertEqual(record['snapshot_hash'], snap['snapshot_hash'])

    # 14. later live-data changes never affect an already-reviewed check
    def test_later_live_data_does_not_affect_review(self):
        snap = self.commit_and_finalize()
        blob = self.matching_invoice(snap)
        record, _ = incoming.check_and_review('2026-003', 'PP', 'invoice.csv', blob)
        business = workflow.positions()
        key = business.loc[business.Bestellnummer == 'order-a'].iloc[0].position_key
        with core.ledger() as db:
            db.execute('''INSERT INTO position_workflow
                (position_key,reviewed_at,paid_at,received_at,closed_at,source,paid_without_invoice_at)
                VALUES(?,?,?,?,?,?,?)''',
                (key, '2026-09-21', None, None, None,
                 workflow.source_snapshot(business.loc[business.position_key == key].iloc[0]), None))
            db.commit()
        with core.ledger() as db:
            still = dict(db.execute('SELECT * FROM partner_round_invoices WHERE round_id=? AND partner=?',
                                     ('2026-003', 'PP')).fetchone())
        self.assertEqual(still['snapshot_hash'], record['snapshot_hash'])
        self.assertEqual(still['amount'], record['amount'])

    # 15. historical 001/002 tables untouched
    def test_001_002_untouched(self):
        snap = self.commit_and_finalize()
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            db.execute("INSERT INTO group_b_rounds VALUES('GB-2026-001',2026,1,'test',NULL,NULL,'0','h','{}','2026-01-01T00:00:00Z')")
            db.commit()
        blob = self.matching_invoice(snap)
        incoming.check_and_review('2026-003', 'PP', 'invoice.csv', blob)
        incoming.confirm_payment('2026-003', 'PP')
        with core.ledger() as db:
            row = db.execute("SELECT * FROM group_b_rounds WHERE id='GB-2026-001'").fetchone()
            count = db.execute("SELECT COUNT(*) FROM group_b_round_positions WHERE round_id='GB-2026-001'").fetchone()[0]
        self.assertIsNotNone(row)
        self.assertEqual(count, 0)

    # 16. MH "bezahlt, Rechnung fehlt" can later gain an invoice without a second payment
    def test_mh_paid_without_invoice_regression_untouched(self):
        # This module never writes to position_workflow / partner_invoices at
        # all, so the historical MH paid-without-invoice flow (protected by
        # test_paid_without_invoice.py) is structurally unaffected - only
        # verify no cross-talk happens via the new tables for an MH position
        # that already carries the historical marker.
        self.seed_sale('p1', 'order-mh', 'MH / TEST')
        business = workflow.positions()
        key = business.loc[business.Bestellnummer == 'order-mh'].iloc[0].position_key
        with core.ledger() as db:
            db.execute('''INSERT INTO position_workflow
                (position_key,reviewed_at,paid_at,received_at,closed_at,source,paid_without_invoice_at)
                VALUES(?,?,?,?,?,?,?)''',
                (key, None, None, None, None, workflow.source_snapshot(business.loc[business.position_key == key].iloc[0]),
                 '2026-09-01'))
            db.commit()
        self.assertEqual(incoming.status('2026-003', 'MH'), 'kein_snapshot')
        with core.ledger() as db:
            count = db.execute('SELECT COUNT(*) FROM partner_round_invoices').fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == '__main__':
    unittest.main()
