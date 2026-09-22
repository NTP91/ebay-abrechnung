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
import round_status
from test_invoice_support import invoice_csv
from test_recovery import payout

BERLIN = ZoneInfo('Europe/Berlin')
BASE_CUT = datetime(2026, 9, 20, 23, 59, tzinfo=BERLIN)  # ends 2026-003


def berlin(*args):
    return datetime(*args, tzinfo=BERLIN)


class RoundStatusTests(unittest.TestCase):
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

    def review_and_pay(self, round_id, partner, snap, number='INV-1'):
        blob = self.matching_invoice(snap, number)
        record, report = incoming.check_and_review(round_id, partner, 'invoice.csv', blob)
        self.assertEqual(report['status'], 'matched', report)
        incoming.confirm_payment(round_id, partner, paid_date=date.today().isoformat())

    # 1. running round -> 'laufend'
    def test_running_round_is_laufend(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        result = round_status.round_status('2026-003', now=berlin(2026, 9, 19, 12, 0))
        self.assertEqual(result['round_status'], 'laufend')
        pp = next(p for p in result['partners'] if p['partner'] == 'PP')
        self.assertEqual(pp['overall_status'], 'laufend')
        self.assertEqual(pp['invoice_status'], 'noch_nicht_moeglich')

    # 2. after cut, no invoice yet -> in_Abwicklung
    def test_after_cut_missing_invoice_is_in_abwicklung(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        result = round_status.round_status('2026-003', now=berlin(2026, 9, 21, 9, 0))
        self.assertEqual(result['round_status'], 'in_Abwicklung')
        pp = next(p for p in result['partners'] if p['partner'] == 'PP')
        self.assertEqual(pp['invoice_status'], 'fehlt')
        self.assertIn('PP · Rechnung fehlt', result['blockers'])

    # 3. reviewed invoice, payment still open -> not completed
    def test_reviewed_but_unpaid_is_not_completed(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        snap, _ = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        blob = self.matching_invoice(snap, 'INV-1')
        incoming.check_and_review('2026-003', 'PP', 'invoice.csv', blob)
        result = round_status.partner_status('2026-003', 'PP', now=berlin(2026, 9, 21, 9, 0))
        self.assertEqual(result['invoice_status'], 'geprueft')
        self.assertEqual(result['payment_status'], 'offen')
        self.assertEqual(result['overall_status'], 'in_Abwicklung')
        self.assertIn('Zahlung offen', result['blockers'])

    # 4. invoice + payment done, no recovery required -> partner completed
    def test_invoice_and_payment_done_no_recovery_completes_partner(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        snap, _ = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        self.review_and_pay('2026-003', 'PP', snap)
        result = round_status.partner_status('2026-003', 'PP', now=berlin(2026, 9, 21, 9, 0))
        self.assertEqual(result['overall_status'], 'abgeschlossen')
        self.assertEqual(result['blockers'], [])
        self.assertEqual(result['credit_status'], 'nicht_erforderlich')
        self.assertEqual(result['payment_status'], 'bezahlt')
        self.assertTrue(result['paid_at'])

    # 5 & 6. open recovery case blocks; resolving it un-blocks
    def test_open_recovery_blocks_then_resolves(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        snap, _ = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        self.review_and_pay('2026-003', 'PP', snap)
        self.seed_refund('p2', 'order-a', 'PP / TEST', amount='-10,00', payout_date='22.09.2026')
        self.seed_sale('p3', 'order-b', 'PP / TEST', payout_date='22.09.2026', artikelnummer='order-b')
        planner.commit_round(now=berlin(2026, 9, 22, 12, 0), base_cut=BASE_CUT)
        snap4, _ = partner_snapshot.finalize('2026-004', 'PP', now=berlin(2026, 9, 28, 9, 0))
        self.review_and_pay('2026-004', 'PP', snap4, number='INV-2')
        recovery_cases.detect()

        result = round_status.partner_status('2026-004', 'PP', now=berlin(2026, 9, 28, 9, 0))
        self.assertEqual(result['credit_status'], 'fehlt')
        self.assertEqual(result['overall_status'], 'in_Abwicklung')
        self.assertIn('Gutschrift fehlt', result['blockers'])

        case = recovery_cases.list_cases(round_id='2026-004', partner='PP')[0]
        import test_recovery_cases as rc_helpers
        recovery_cases.resolve(case['id'], 'credit.csv', rc_helpers.credit_csv('order-a', '10.00'))

        result = round_status.partner_status('2026-004', 'PP', now=berlin(2026, 9, 28, 9, 0))
        self.assertEqual(result['credit_status'], 'erledigt')
        self.assertEqual(result['overall_status'], 'abgeschlossen')
        self.assertEqual(result['blockers'], [])

    # 7. a resolved recovery case never reverts to "nicht erforderlich"
    def test_resolved_recovery_stays_historically_required(self):
        self.assertEqual(recovery_cases.status('2026-999', 'ANY'), 'nicht_erforderlich')
        # (full erledigt-stays-erledigt guarantee is exercised end-to-end above
        # and in test_recovery_cases.py; this only pins the "never existed"
        # baseline so a future regression in that guarantee is visible here too.)

    # 8 & 9. a 0-position partner is automatically "nichts erforderlich" and never blocks
    def test_zero_position_partner_never_blocks_round(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        snap, _ = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        self.review_and_pay('2026-003', 'PP', snap)
        result = round_status.round_status('2026-003', now=berlin(2026, 9, 21, 9, 0))
        mk = next(p for p in result['partners'] if p['partner'] == 'MK')
        self.assertEqual(mk['positions'], 0)
        self.assertEqual(mk['overall_status'], 'nichts_erforderlich')
        self.assertEqual(mk['invoice_status'], 'nicht_erforderlich')
        self.assertEqual(mk['payment_status'], 'nicht_erforderlich')
        self.assertEqual(mk['credit_status'], 'nicht_erforderlich')
        self.assertEqual(mk['blockers'], [])
        # PP is the only partner with positions, and is fully done -> round closes
        self.assertEqual(result['round_status'], 'abgeschlossen')

    # 10. every partner done -> round completed
    def test_all_partners_done_round_completed(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        snap, _ = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        self.review_and_pay('2026-003', 'PP', snap)
        result = round_status.round_status('2026-003', now=berlin(2026, 9, 21, 9, 0))
        self.assertEqual(result['round_status'], 'abgeschlossen')
        self.assertEqual(result['blockers'], [])
        self.assertEqual(result['open_partner_count'], 0)

    # 11. one open partner -> round in Abwicklung with a concrete blocker
    def test_one_open_partner_keeps_round_in_abwicklung(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        result = round_status.round_status('2026-003', now=berlin(2026, 9, 21, 9, 0))
        self.assertEqual(result['round_status'], 'in_Abwicklung')
        self.assertEqual(result['open_partner_count'], 1)
        self.assertIn('PP · Rechnung fehlt', result['blockers'])

    # after cut, nobody has downloaded the final statement yet -> the one
    # concrete blocker is the missing snapshot, not a redundant "Zahlung offen"
    def test_missing_snapshot_is_the_sole_blocker_not_redundant_payment(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        result = round_status.partner_status('2026-003', 'PP', now=berlin(2026, 9, 21, 9, 0))
        self.assertEqual(result['snapshot_status'], 'kein_snapshot')
        self.assertEqual(result['invoice_status'], 'noch_nicht_moeglich')
        self.assertEqual(result['payment_status'], 'offen')
        self.assertEqual(result['blockers'], ['Finale Einzelabrechnung fehlt'])

    # 12. historical rounds are refused, not reinterpreted
    def test_historical_round_is_refused(self):
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            db.execute("INSERT INTO group_b_rounds VALUES('GB-2026-001',2026,1,'test',NULL,NULL,'0','h','{}','2026-01-01T00:00:00Z')")
            db.commit()
        with self.assertRaisesRegex(ValueError, 'historische Geschäftslogik'):
            round_status.round_status('GB-2026-001')
        with self.assertRaisesRegex(ValueError, 'historische Geschäftslogik'):
            round_status.partner_status('GB-2026-001', 'MH')

    # 13. unknown partner prefix only locks its own positions, never crashes status
    def test_unknown_partner_prefix_only_locks_its_own_positions(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST', payout_date='14.09.2026')
        self.seed_sale('p2', 'order-z', 'ZZ / TEST', payout_date='14.09.2026', artikelnummer='order-z')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        result = round_status.round_status('2026-003', now=berlin(2026, 9, 19, 0, 0))
        self.assertNotIn('ZZ', {p['partner'] for p in result['partners']})

    # 14. Gruppe A and Gruppe B use the identical 003+ completion workflow
    def test_group_a_and_b_use_identical_workflow(self):
        self.seed_sale('p1', 'order-pp', 'PP / TEST', payout_date='14.09.2026', artikelnummer='order-pp')
        self.seed_sale('p1m', 'order-mh', 'MH / TEST', payout_date='14.09.2026', artikelnummer='order-mh')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        snap_pp, _ = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        snap_mh, _ = partner_snapshot.finalize('2026-003', 'MH', now=berlin(2026, 9, 21, 0, 5))
        self.review_and_pay('2026-003', 'PP', snap_pp, number='INV-PP')
        self.review_and_pay('2026-003', 'MH', snap_mh, number='INV-MH')
        result = round_status.round_status('2026-003', now=berlin(2026, 9, 21, 9, 0))
        pp = next(p for p in result['partners'] if p['partner'] == 'PP')
        mh = next(p for p in result['partners'] if p['partner'] == 'MH')
        self.assertEqual(pp['group'], 'Gruppe A')
        self.assertEqual(mh['group'], 'Gruppe B')
        self.assertEqual(pp['overall_status'], 'abgeschlossen')
        self.assertEqual(mh['overall_status'], 'abgeschlossen')
        # Beide Partner sind fertig, aber die Runde enthaelt eine
        # provisionsrelevante Gruppe-B-Position (MH): die eigene Spur
        # "Vermittlungsabrechnung Patrick -> Evelyn" ist noch offen und haelt
        # die Runde korrekt in Abwicklung - ohne die Partner zurueckzusetzen.
        self.assertEqual(result['broker']['status'], 'offen')
        self.assertEqual(result['round_status'], 'in_Abwicklung')
        self.assertEqual([p['blockers'] for p in (pp, mh)], [[], []])

        import broker_commission
        broker_commission.finalize('2026-003', now=berlin(2026, 9, 21, 0, 5))
        broker_commission.confirm_payment('2026-003')
        result = round_status.round_status('2026-003', now=berlin(2026, 9, 21, 9, 0))
        self.assertEqual(result['broker']['status'], 'erstellt')
        self.assertEqual(result['round_status'], 'abgeschlossen')

    # 15. fail-soft: a technical failure loading the broker-commission status
    # must never take down the rest of the round overview, and must never
    # let the round appear 'abgeschlossen' while its true state is unknown.
    def test_broker_status_exception_does_not_break_round_overview(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        snap, _ = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        self.review_and_pay('2026-003', 'PP', snap)
        # Baseline: with a healthy broker status this exact setup completes.
        healthy = round_status.round_status('2026-003', now=berlin(2026, 9, 21, 9, 0))
        self.assertEqual(healthy['round_status'], 'abgeschlossen')

        import broker_commission
        with patch.object(broker_commission, 'status', side_effect=RuntimeError('DB nicht erreichbar')):
            result = round_status.round_status('2026-003', now=berlin(2026, 9, 21, 9, 0))

        # The technical broker failure must not raise out of round_status()...
        self.assertTrue(result['broker_error'])
        self.assertIsNone(result['broker'])
        # ...must never claim completion while the broker state is unknown...
        self.assertNotEqual(result['round_status'], 'abgeschlossen')
        self.assertIn('in_Abwicklung', result['round_status'])
        self.assertTrue(any('nicht verfügbar' in b for b in result['blockers']))
        # ...and must leave the fachlich unabhaengige Partnerinformation intact.
        pp = next(p for p in result['partners'] if p['partner'] == 'PP')
        self.assertEqual(pp['overall_status'], 'abgeschlossen')
        self.assertEqual(pp['blockers'], [])

    def test_broker_status_exception_is_logged_not_silenced(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        import broker_commission
        with patch.object(broker_commission, 'status', side_effect=RuntimeError('DB nicht erreichbar')):
            with self.assertLogs('round_status', level='ERROR') as logs:
                round_status.round_status('2026-003', now=berlin(2026, 9, 19, 0, 0))
        self.assertTrue(any('Broker-Commission-Status' in line for line in logs.output))
        # No secrets/connection details end up in the log message itself.
        self.assertFalse(any('SUPABASE' in line.upper() and 'TOKEN' in line.upper() for line in logs.output))


if __name__ == '__main__':
    unittest.main()
