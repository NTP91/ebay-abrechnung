import csv
import io
import json
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import core
import partner_round_invoices as incoming
import partner_snapshot
import position_workflow as workflow
import recovery_cases
import round_planner as planner
from test_invoice_support import invoice_csv
from test_recovery import payout

BERLIN = ZoneInfo('Europe/Berlin')
BASE_CUT = datetime(2026, 9, 20, 23, 59, tzinfo=BERLIN)  # ends 2026-003


def berlin(*args):
    return datetime(*args, tzinfo=BERLIN)


def credit_csv(order, amount):
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(['Bestellnummer', 'Positionsbetrag brutto', 'Gesamtbetrag brutto'])
    writer.writerow([order, str(amount), str(amount)])
    return output.getvalue().encode('utf-8-sig')


class RecoveryCaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        paths = patch.multiple(core, PAYOUTS_DB_PATH=str(self.root / 'Master_Payouts.csv'),
                                ORDERS_DB_PATH=str(self.root / 'Master_Orders.csv'))
        paths.start(); self.addCleanup(paths.stop)

    def seed_sale(self, payout_id, order, sku, amount='50,00', payout_date='14.09.2026', artikelnummer=None):
        artikelnummer = artikelnummer or order
        order_frame = payout(payout_id, order, order, sku=sku, amount=amount)
        order_frame['Artikelnummer'] = artikelnummer
        core.import_reports([order_frame], core.ORDERS_DB_PATH, 'orders')
        sale = payout(payout_id, order, order, sku=sku, amount=amount)
        sale['Artikelnummer'] = artikelnummer
        sale['Auszahlungsdatum'] = payout_date
        sale['Auszahlungsstatus'] = 'Betrag überwiesen'
        sale['Transaktionsbetrag (inkl. Kosten)'] = amount
        core.import_reports([sale], core.PAYOUTS_DB_PATH, 'payout')

    def seed_refund(self, payout_id, order, sku, amount, payout_date, artikelnummer=None, transaction='refund1'):
        artikelnummer = artikelnummer or order
        credit = payout(payout_id, transaction, order, sku=sku, amount=amount, kind='Rückerstattung')
        credit['Artikelnummer'] = artikelnummer
        credit['Auszahlungsdatum'] = payout_date
        credit['Transaktionsbetrag (inkl. Kosten)'] = amount
        core.import_reports([credit], core.PAYOUTS_DB_PATH, 'payout')

    def matching_invoice(self, snap, number):
        line_items = json.loads(snap['line_items'])
        expected = dict(items=[dict(item) for item in line_items], total=snap['final_amount'])
        return invoice_csv(expected, number)

    def pay_partner(self, round_id, order, sku, partner, commit_now, finalize_now, payout_date,
                     payout_id='p1', invoice_number='INV-1', amount='50,00', artikelnummer=None, commit=True):
        self.seed_sale(payout_id, order, sku, amount=amount, payout_date=payout_date, artikelnummer=artikelnummer)
        if commit:
            planner.commit_round(now=commit_now, base_cut=BASE_CUT)
        snap, created = partner_snapshot.finalize(round_id, partner, now=finalize_now)
        self.assertTrue(created, f'expected a fresh snapshot for {round_id}/{partner}')
        blob = self.matching_invoice(snap, invoice_number)
        record, report = incoming.check_and_review(round_id, partner, 'invoice.csv', blob)
        self.assertEqual(report['status'], 'matched', report)
        incoming.confirm_payment(round_id, partner, paid_date=date.today().isoformat())
        return snap

    # 1 & 5. refund known before finalization -> Tab 2, no recovery case
    def test_refund_before_finalization_no_case(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST', payout_date='14.09.2026')
        self.seed_refund('p1-r', 'order-a', 'PP / TEST', amount='-10,00', payout_date='15.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        snap, created = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        self.assertTrue(created)
        self.assertNotEqual(Decimal(snap['refunds_total']), 0)
        cases = recovery_cases.detect()
        self.assertEqual(cases, [])
        self.assertEqual(recovery_cases.list_cases(), [])

    # 2. refund after partner payment -> new recovery case in the current round
    def test_refund_after_payment_creates_case_in_current_round(self):
        self.pay_partner('2026-003', 'order-a', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 18, 12, 0), finalize_now=berlin(2026, 9, 21, 0, 5),
                          payout_date='14.09.2026')
        self.seed_refund('p2', 'order-a', 'PP / TEST', amount='-10,00', payout_date='22.09.2026')
        # a second, later round becomes "current"
        self.pay_partner('2026-004', 'order-b', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 22, 12, 0), finalize_now=berlin(2026, 9, 28, 9, 0),
                          payout_date='22.09.2026', payout_id='p3', invoice_number='INV-2')
        created = recovery_cases.detect()
        self.assertEqual(len(created), 1)
        case = recovery_cases.list_cases()[0]
        self.assertEqual(case['partner'], 'PP')
        self.assertEqual(case['origin_round_id'], '2026-003')
        self.assertEqual(case['current_round_id'], '2026-004')
        self.assertEqual(case['status'], 'offen')
        self.assertEqual(Decimal(case['refund_amount']), Decimal('-10.00'))

    # 3. origin round stays unchanged
    def test_origin_round_untouched(self):
        self.pay_partner('2026-003', 'order-a', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 18, 12, 0), finalize_now=berlin(2026, 9, 21, 0, 5),
                          payout_date='14.09.2026')
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            before = [dict(r) for r in db.execute(
                "SELECT position_key, round_id FROM group_b_round_positions WHERE round_id='2026-003'")]
        self.seed_refund('p2', 'order-a', 'PP / TEST', amount='-10,00', payout_date='22.09.2026')
        self.pay_partner('2026-004', 'order-b', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 22, 12, 0), finalize_now=berlin(2026, 9, 28, 9, 0),
                          payout_date='22.09.2026', payout_id='p3', invoice_number='INV-2')
        recovery_cases.detect()
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            after = [dict(r) for r in db.execute(
                "SELECT position_key, round_id FROM group_b_round_positions WHERE round_id='2026-003'")]
        self.assertEqual(before, after)

    # 4. old final snapshot stays byte-identical
    def test_old_snapshot_stays_byte_identical(self):
        before = self.pay_partner('2026-003', 'order-a', 'PP / TEST', 'PP',
                                   commit_now=berlin(2026, 9, 18, 12, 0), finalize_now=berlin(2026, 9, 21, 0, 5),
                                   payout_date='14.09.2026')
        self.seed_refund('p2', 'order-a', 'PP / TEST', amount='-10,00', payout_date='22.09.2026')
        self.pay_partner('2026-004', 'order-b', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 22, 12, 0), finalize_now=berlin(2026, 9, 28, 9, 0),
                          payout_date='22.09.2026', payout_id='p3', invoice_number='INV-2')
        recovery_cases.detect()
        self.assertEqual(partner_snapshot.final_file('2026-003', 'PP'), before['file_bytes'])

    # 6. same refund transaction on next sync -> no duplicate
    def test_repeated_detect_is_idempotent(self):
        self.pay_partner('2026-003', 'order-a', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 18, 12, 0), finalize_now=berlin(2026, 9, 21, 0, 5),
                          payout_date='14.09.2026')
        self.seed_refund('p2', 'order-a', 'PP / TEST', amount='-10,00', payout_date='22.09.2026')
        self.pay_partner('2026-004', 'order-b', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 22, 12, 0), finalize_now=berlin(2026, 9, 28, 9, 0),
                          payout_date='22.09.2026', payout_id='p3', invoice_number='INV-2')
        first = recovery_cases.detect()
        second = recovery_cases.detect()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(len(recovery_cases.list_cases()), 1)

    # 7. ambiguous / unmatched refund -> no automatic charge
    def test_unmatched_refund_not_guessed(self):
        self.pay_partner('2026-003', 'order-a', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 18, 12, 0), finalize_now=berlin(2026, 9, 21, 0, 5),
                          payout_date='14.09.2026')
        # different Artikelnummer -> core.refund_links() cannot pair it to any sale
        self.seed_refund('p2', 'order-a', 'PP / TEST', amount='-10,00', payout_date='22.09.2026',
                          artikelnummer='unrelated-item')
        self.pay_partner('2026-004', 'order-b', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 22, 12, 0), finalize_now=berlin(2026, 9, 28, 9, 0),
                          payout_date='22.09.2026', payout_id='p3', invoice_number='INV-2')
        created = recovery_cases.detect()
        self.assertEqual(created, [])

    # 8. a partnerless fee never becomes a recovery case
    def test_partnerless_fee_no_case(self):
        self.pay_partner('2026-003', 'order-a', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 18, 12, 0), finalize_now=berlin(2026, 9, 21, 0, 5),
                          payout_date='14.09.2026')
        fee = payout('p-fee', 'fee1', '', sku='', amount='-5,00', kind='Gebühr')
        fee['Auszahlungsdatum'] = '22.09.2026'
        core.import_reports([fee], core.PAYOUTS_DB_PATH, 'payout')
        created = recovery_cases.detect()
        self.assertEqual(created, [])

    # 9. a hold is not a refund
    def test_hold_not_treated_as_refund(self):
        self.pay_partner('2026-003', 'order-a', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 18, 12, 0), finalize_now=berlin(2026, 9, 21, 0, 5),
                          payout_date='14.09.2026')
        self.seed_refund('p2', 'order-a', 'PP / TEST', amount='-10,00', payout_date='22.09.2026')
        self.pay_partner('2026-004', 'order-b', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 22, 12, 0), finalize_now=berlin(2026, 9, 28, 9, 0),
                          payout_date='22.09.2026', payout_id='p3', invoice_number='INV-2')
        business = workflow.positions()
        business.loc[business.Bestellnummer == 'order-a', 'API_Hold'] = True
        created = recovery_cases.detect(business=business)
        self.assertEqual(created, [])

    # 10. an open recovery case blocks partner completion
    def test_open_case_blocks_partner_completion(self):
        self.pay_partner('2026-003', 'order-a', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 18, 12, 0), finalize_now=berlin(2026, 9, 21, 0, 5),
                          payout_date='14.09.2026')
        self.seed_refund('p2', 'order-a', 'PP / TEST', amount='-10,00', payout_date='22.09.2026')
        self.pay_partner('2026-004', 'order-b', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 22, 12, 0), finalize_now=berlin(2026, 9, 28, 9, 0),
                          payout_date='22.09.2026', payout_id='p3', invoice_number='INV-2')
        self.assertEqual(recovery_cases.partner_round_status('2026-004', 'PP'), 'abgeschlossen')
        recovery_cases.detect()
        self.assertEqual(recovery_cases.status('2026-004', 'PP'), 'fehlt')
        self.assertEqual(recovery_cases.partner_round_status('2026-004', 'PP'), 'gutschrift_fehlt')

    # 11 & 12. a correct credit resolves the case, and it stays resolved (never reverts)
    def test_correct_credit_resolves_case_and_stays_resolved(self):
        self.pay_partner('2026-003', 'order-a', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 18, 12, 0), finalize_now=berlin(2026, 9, 21, 0, 5),
                          payout_date='14.09.2026')
        self.seed_refund('p2', 'order-a', 'PP / TEST', amount='-10,00', payout_date='22.09.2026')
        self.pay_partner('2026-004', 'order-b', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 22, 12, 0), finalize_now=berlin(2026, 9, 28, 9, 0),
                          payout_date='22.09.2026', payout_id='p3', invoice_number='INV-2')
        recovery_cases.detect()
        case = recovery_cases.list_cases()[0]
        record, report = recovery_cases.resolve(case['id'], 'credit.csv', credit_csv('order-a', '10.00'))
        self.assertEqual(report['status'], 'matched')
        self.assertEqual(record['status'], 'erledigt')
        self.assertTrue(record['resolved_at'])
        self.assertEqual(recovery_cases.status('2026-004', 'PP'), 'erledigt')
        self.assertEqual(recovery_cases.partner_round_status('2026-004', 'PP'), 'abgeschlossen')
        with self.assertRaises(ValueError):
            recovery_cases.resolve(case['id'], 'credit.csv', credit_csv('order-a', '10.00'))

    # 13. wrong credit amount -> not approved
    def test_wrong_credit_amount_not_approved(self):
        self.pay_partner('2026-003', 'order-a', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 18, 12, 0), finalize_now=berlin(2026, 9, 21, 0, 5),
                          payout_date='14.09.2026')
        self.seed_refund('p2', 'order-a', 'PP / TEST', amount='-10,00', payout_date='22.09.2026')
        self.pay_partner('2026-004', 'order-b', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 22, 12, 0), finalize_now=berlin(2026, 9, 28, 9, 0),
                          payout_date='22.09.2026', payout_id='p3', invoice_number='INV-2')
        recovery_cases.detect()
        case = recovery_cases.list_cases()[0]
        record, report = recovery_cases.resolve(case['id'], 'credit.csv', credit_csv('order-a', '999.00'))
        self.assertIsNone(record)
        self.assertEqual(report['status'], 'deviation')
        self.assertEqual(recovery_cases.status('2026-004', 'PP'), 'fehlt')

    # 14. Gruppe A and B work identically
    def test_group_a_and_b_identical(self):
        self.seed_sale('p1', 'order-ba', 'BA / TEST', payout_date='14.09.2026', artikelnummer='order-ba')
        self.seed_sale('p1m', 'order-mh', 'MH / TEST', payout_date='14.09.2026', artikelnummer='order-mh')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        for partner, order, number in (('BA', 'order-ba', 'INV-BA'), ('MH', 'order-mh', 'INV-MH')):
            snap, created = partner_snapshot.finalize('2026-003', partner, now=berlin(2026, 9, 21, 0, 5))
            self.assertTrue(created)
            blob = self.matching_invoice(snap, number)
            record, report = incoming.check_and_review('2026-003', partner, 'invoice.csv', blob)
            self.assertEqual(report['status'], 'matched', report)
            incoming.confirm_payment('2026-003', partner, paid_date=date.today().isoformat())
        self.seed_refund('p2', 'order-ba', 'BA / TEST', amount='-10,00', payout_date='22.09.2026', artikelnummer='order-ba')
        self.seed_refund('p2m', 'order-mh', 'MH / TEST', amount='-10,00', payout_date='22.09.2026', artikelnummer='order-mh', transaction='refund-mh')
        self.seed_sale('p3', 'order-ba2', 'BA / TEST', payout_date='22.09.2026', artikelnummer='order-ba2')
        self.seed_sale('p3m', 'order-mh2', 'MH / TEST', payout_date='22.09.2026', artikelnummer='order-mh2')
        planner.commit_round(now=berlin(2026, 9, 22, 12, 0), base_cut=BASE_CUT)
        for partner, order, number in (('BA', 'order-ba2', 'INV-BA2'), ('MH', 'order-mh2', 'INV-MH2')):
            snap, created = partner_snapshot.finalize('2026-004', partner, now=berlin(2026, 9, 28, 9, 0))
            self.assertTrue(created)
            blob = self.matching_invoice(snap, number)
            record, report = incoming.check_and_review('2026-004', partner, 'invoice.csv', blob)
            self.assertEqual(report['status'], 'matched', report)
            incoming.confirm_payment('2026-004', partner, paid_date=date.today().isoformat())
        created = recovery_cases.detect()
        self.assertEqual(len(created), 2)
        partners = {c['partner'] for c in recovery_cases.list_cases()}
        self.assertEqual(partners, {'BA', 'MH'})

    # 15. the historical MH/RE0090 model (7 already-verrechnete refunds) never spawns a new case
    def test_historical_mh_model_position_never_spawns_case(self):
        self.seed_sale('p1', 'order-mh-hist', 'MH / TEST', payout_date='14.09.2026', artikelnummer='mh-hist')
        business = workflow.positions()
        historical_key = business.loc[business.Bestellnummer == 'order-mh-hist'].iloc[0].position_key
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            db.execute("INSERT INTO group_b_rounds VALUES('GB-2026-001',2026,1,'test',NULL,NULL,'0','h','{}','2026-01-01T00:00:00Z')")
            db.execute("INSERT INTO group_b_round_positions VALUES(?,?,?,?)", (historical_key, 'GB-2026-001', 'evelyn_invoice', ''))
            db.commit()
        # An unrelated position gives round_planner something normal to commit,
        # so a "current" 2026-0xx round exists - order-mh-hist itself is already
        # historically assigned above and is therefore never swept into it.
        self.seed_sale('p-other', 'order-other', 'PP / TEST', payout_date='14.09.2026', artikelnummer='other')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            in_neutral_round = db.execute(
                "SELECT 1 FROM group_b_round_positions gbp JOIN group_b_rounds gr ON gr.id=gbp.round_id "
                "WHERE gbp.position_key=? AND gr.source_kind='neutral_weekly'", (historical_key,)).fetchone()
        self.assertIsNone(in_neutral_round)
        for index in range(7):
            self.seed_refund(f'p1-r{index}', 'order-mh-hist', 'MH / TEST', amount='-1,00',
                              payout_date='15.09.2026', artikelnummer='mh-hist', transaction=f'hist-refund-{index}')
        created = recovery_cases.detect()
        self.assertEqual(created, [])
        self.assertEqual(recovery_cases.list_cases(), [])

    # 16. a second detect() run with the same data is fully idempotent
    def test_second_run_fully_idempotent(self):
        self.pay_partner('2026-003', 'order-a', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 18, 12, 0), finalize_now=berlin(2026, 9, 21, 0, 5),
                          payout_date='14.09.2026')
        self.seed_refund('p2', 'order-a', 'PP / TEST', amount='-10,00', payout_date='22.09.2026')
        self.pay_partner('2026-004', 'order-b', 'PP / TEST', 'PP',
                          commit_now=berlin(2026, 9, 22, 12, 0), finalize_now=berlin(2026, 9, 28, 9, 0),
                          payout_date='22.09.2026', payout_id='p3', invoice_number='INV-2')
        recovery_cases.detect()
        before = recovery_cases.list_cases()
        recovery_cases.detect()
        after = recovery_cases.list_cases()
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
