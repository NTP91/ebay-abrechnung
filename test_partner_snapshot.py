import json
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import core
import partner_snapshot
import position_workflow as workflow
import round_planner as planner
from test_recovery import payout

BERLIN = ZoneInfo('Europe/Berlin')
# Same fixed anchor as test_round_planner.py: 2026-09-20 23:59 Europe/Berlin ends 2026-003.
BASE_CUT = datetime(2026, 9, 20, 23, 59, tzinfo=BERLIN)


def berlin(*args):
    return datetime(*args, tzinfo=BERLIN)


class PartnerSnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        paths = patch.multiple(core, PAYOUTS_DB_PATH=str(self.root / 'Master_Payouts.csv'),
                                ORDERS_DB_PATH=str(self.root / 'Master_Orders.csv'))
        paths.start(); self.addCleanup(paths.stop)

    def seed_sale(self, payout_id, order, sku, amount='50,00', payout_date='18.09.2026', artikelnummer='item-1'):
        order_frame = payout(payout_id, order, order, sku=sku, amount=amount)
        order_frame['Artikelnummer'] = artikelnummer
        core.import_reports([order_frame], core.ORDERS_DB_PATH, 'orders')
        sale = payout(payout_id, order, order, sku=sku, amount=amount)
        sale['Artikelnummer'] = artikelnummer
        sale['Auszahlungsdatum'] = payout_date
        sale['Auszahlungsstatus'] = 'Betrag überwiesen'
        sale['Transaktionsbetrag (inkl. Kosten)'] = amount
        core.import_reports([sale], core.PAYOUTS_DB_PATH, 'payout')

    def seed_refund(self, payout_id, order, sku, amount, payout_date, artikelnummer='item-1', transaction='refund1'):
        credit = payout(payout_id, transaction, order, sku=sku, amount=amount, kind='Rückerstattung')
        credit['Artikelnummer'] = artikelnummer
        credit['Auszahlungsdatum'] = payout_date
        credit['Transaktionsbetrag (inkl. Kosten)'] = amount
        core.import_reports([credit], core.PAYOUTS_DB_PATH, 'payout')

    def commit_003(self, order='order-a', sku='PP / TEST'):
        self.seed_sale('p1', order, sku, payout_date='14.09.2026')
        return planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)

    def current(self):
        return workflow.positions(), core.read_master(core.PAYOUTS_DB_PATH)

    # 1. still running -> interim only, no final snapshot allowed
    def test_running_round_only_interim_no_final(self):
        self.commit_003()
        preview = partner_snapshot.interim_export('2026-003', 'PP')
        self.assertIsNotNone(preview)
        with self.assertRaises(ValueError):
            partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 19, 12, 0))

    # 2. after cut -> first final download creates the snapshot
    def test_after_cut_first_download_creates_snapshot(self):
        self.commit_003()
        record, created = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        self.assertTrue(created)
        self.assertEqual(record['round_id'], '2026-003')
        self.assertEqual(record['partner'], 'PP')
        self.assertTrue(record['file_bytes'])
        self.assertTrue(record['finalized_at'])

    # 3. second download -> exact same bytes/hash, no recompute
    def test_second_download_identical_bytes(self):
        self.commit_003()
        first, created1 = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        second, created2 = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 22, 9, 0))
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first['file_hash'], second['file_hash'])
        self.assertEqual(first['file_bytes'], second['file_bytes'])
        self.assertEqual(first['snapshot_hash'], second['snapshot_hash'])
        self.assertEqual(partner_snapshot.final_file('2026-003', 'PP'), first['file_bytes'])

    # 4. changed live workflow state after finalization never changes the stored export
    def test_changed_live_data_does_not_affect_stored_export(self):
        self.commit_003()
        first, _ = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        business = workflow.positions()
        key = business.loc[business.Bestellnummer == 'order-a'].iloc[0].position_key
        with core.ledger() as db:
            db.execute('''INSERT INTO position_workflow
                (position_key,reviewed_at,paid_at,received_at,closed_at,source,paid_without_invoice_at)
                VALUES(?,?,?,?,?,?,?)''',
                (key, '2026-09-22', None, None, None,
                 workflow.source_snapshot(business.loc[business.position_key == key].iloc[0]), None))
            db.commit()
        self.assertEqual(partner_snapshot.final_file('2026-003', 'PP'), first['file_bytes'])

    # 5. a genuine new-week payout always lands in the new round, download timing irrelevant
    def test_download_timing_does_not_change_week_membership(self):
        self.commit_003()
        planner.rollover(now=berlin(2026, 9, 21, 0, 5))
        self.seed_sale('p2', 'order-mon', 'PP / TEST', payout_date='21.09.2026', artikelnummer='item-mon')
        business, payouts = self.current()
        planner.assign_late_payouts(business=business, payouts=payouts)
        mon_key = business.loc[business.Bestellnummer == 'order-mon'].iloc[0].position_key
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            row = db.execute('SELECT round_id FROM group_b_round_positions WHERE position_key=?', (mon_key,)).fetchone()
        self.assertEqual(row[0], '2026-004')
        # Finalizing 003 only now (Monday, delayed download) must not pull the Monday position in.
        record, created = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 9, 0))
        self.assertTrue(created)
        self.assertNotIn(mon_key, json.loads(record['position_keys']))

    # 6. late payout dated into old week, before that partner is finalized -> old round allowed
    def test_late_payout_before_partner_finalization_allowed_in_old_round(self):
        self.commit_003()
        planner.rollover(now=berlin(2026, 9, 21, 0, 5))
        self.seed_sale('p2', 'order-late', 'PP / TEST', payout_date='15.09.2026', artikelnummer='item-late')
        business, payouts = self.current()
        assigned = planner.assign_late_payouts(business=business, payouts=payouts)
        key = business.loc[business.Bestellnummer == 'order-late'].iloc[0].position_key
        self.assertEqual([a['round_id'] for a in assigned if a['position_key'] == key], ['2026-003'])

    # 7. late payout dated into old week, after that partner is finalized -> next open round
    def test_late_payout_after_partner_finalization_goes_to_next_open_round(self):
        self.commit_003()
        partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        planner.rollover(now=berlin(2026, 9, 21, 0, 5))
        self.seed_sale('p2', 'order-late', 'PP / TEST', payout_date='15.09.2026', artikelnummer='item-late')
        business, payouts = self.current()
        assigned = planner.assign_late_payouts(business=business, payouts=payouts)
        key = business.loc[business.Bestellnummer == 'order-late'].iloc[0].position_key
        self.assertEqual([a['round_id'] for a in assigned if a['position_key'] == key], ['2026-004'])

    # 8. a finalized partner never locks other partners in the same round
    def test_finalized_partner_does_not_lock_other_partners(self):
        self.seed_sale('p1', 'order-pp', 'PP / TEST', payout_date='14.09.2026', artikelnummer='item-pp')
        self.seed_sale('p1b', 'order-ba', 'BA / TEST', payout_date='14.09.2026', artikelnummer='item-ba')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        record, created = partner_snapshot.finalize('2026-003', 'BA', now=berlin(2026, 9, 21, 0, 5))
        self.assertTrue(created)
        self.assertEqual(record['partner'], 'BA')

    # 9. 0-position partner -> nothing required, no snapshot needed/possible
    def test_zero_position_partner_no_snapshot_needed(self):
        self.commit_003()
        self.assertIsNone(partner_snapshot.interim_export('2026-003', 'MK'))
        with self.assertRaises(ValueError):
            partner_snapshot.finalize('2026-003', 'MK', now=berlin(2026, 9, 21, 0, 5))

    # 10. historical 001/002 untouched
    def test_001_002_untouched(self):
        self.commit_003()
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            db.execute("INSERT INTO group_b_rounds VALUES('GB-2026-001',2026,1,'test',NULL,NULL,'0','h','{}','2026-01-01T00:00:00Z')")
            db.commit()
        partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            row = db.execute("SELECT * FROM group_b_rounds WHERE id='GB-2026-001'").fetchone()
            count = db.execute("SELECT COUNT(*) FROM group_b_round_positions WHERE round_id='GB-2026-001'").fetchone()[0]
        self.assertIsNotNone(row)
        self.assertEqual(count, 0)

    # 11. MH finalizes like any other partner, no legacy interference
    def test_mh_partner_finalizes_normally(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', payout_date='14.09.2026', artikelnummer='item-mh')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        record, created = partner_snapshot.finalize('2026-003', 'MH', now=berlin(2026, 9, 21, 0, 5))
        self.assertTrue(created)
        self.assertEqual(record['partner'], 'MH')

    # 12. Tab-2 refund is counted exactly once in the final amount
    def test_tab2_refund_counted_once_in_final_amount(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST', amount='50,00', payout_date='14.09.2026', artikelnummer='item-x')
        self.seed_refund('p1-refund', 'order-a', 'PP / TEST', amount='-10,00', payout_date='15.09.2026', artikelnummer='item-x')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        record, created = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        self.assertTrue(created)
        regular = Decimal(record['regular_claim'])
        refunds = Decimal(record['refunds_total'])
        final = Decimal(record['final_amount'])
        self.assertLess(refunds, 0)
        self.assertEqual(final, regular + refunds)

    # 13. Tab-3 repayments arriving after finalization never change the frozen historical amount
    def test_tab3_refund_after_finalization_does_not_change_frozen_amount(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST', amount='50,00', payout_date='14.09.2026', artikelnummer='item-x')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        first, created1 = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        self.assertTrue(created1)
        business = workflow.positions()
        key = business.loc[business.Bestellnummer == 'order-a'].iloc[0].position_key
        with core.ledger() as db:
            db.execute('''INSERT INTO position_workflow
                (position_key,reviewed_at,paid_at,received_at,closed_at,source,paid_without_invoice_at)
                VALUES(?,?,?,?,?,?,?)''',
                (key, '2026-09-19', '2026-09-19', None, None,
                 workflow.source_snapshot(business.loc[business.position_key == key].iloc[0]), None))
            db.commit()
        self.seed_refund('p2', 'order-a', 'PP / TEST', amount='-10,00', payout_date='29.09.2026', artikelnummer='item-x', transaction='refund-late')
        second, created2 = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 29, 12, 0))
        self.assertFalse(created2)
        self.assertEqual(first['final_amount'], second['final_amount'])
        self.assertEqual(first['file_bytes'], second['file_bytes'])

    # 14. repeated finalization never creates a second stored version
    def test_repeated_finalize_no_second_version(self):
        self.commit_003()
        partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 22, 0, 5))
        with core.ledger() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM partner_round_snapshots WHERE round_id='2026-003' AND partner='PP'").fetchone()[0]
        self.assertEqual(count, 1)

    # 15. the stored snapshot is directly usable as the later partner-invoice check basis
    def test_snapshot_usable_for_later_invoice_check(self):
        self.commit_003()
        record, _ = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        keys = json.loads(record['position_keys'])
        self.assertEqual(len(keys), record['position_count'])
        self.assertTrue(all(isinstance(k, str) for k in keys))


if __name__ == '__main__':
    unittest.main()
