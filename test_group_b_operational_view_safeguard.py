"""Regression for the Group-B operational view (studio_view.partner_rows()):
a historically paid-without-invoice position must never resurface as a new,
payable claim. MH is the verified real-world regression fixture, but the
fix itself (studio_view.py's valid_b/committed_b filters) is generic -
another partner is checked too, to prove nothing MH-specific was hardcoded.
"""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import core
import position_workflow as workflow
import studio_view
from test_recovery import payout


class GroupBOperationalViewSafeguardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        paths = patch.multiple(core, PAYOUTS_DB_PATH=str(self.root / 'Master_Payouts.csv'),
                                ORDERS_DB_PATH=str(self.root / 'Master_Orders.csv'))
        paths.start(); self.addCleanup(paths.stop)

    def seed(self, sku, payout_id, order, transaction):
        frame = payout(payout_id, transaction=transaction, order=order, sku=sku)
        frame['Auszahlungsstatus'] = 'Betrag überwiesen'
        core.import_reports([frame], core.ORDERS_DB_PATH, 'orders')
        core.import_reports([frame], core.PAYOUTS_DB_PATH, 'payout')

    def assign_round(self, position_key, round_id):
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            db.execute('INSERT OR IGNORE INTO group_b_rounds VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (round_id, 2026, 1, 'test', None, None, '0', 'hash-' + round_id, '{}', '2026-01-01T00:00:00Z'))
            db.execute('INSERT OR REPLACE INTO group_b_round_positions VALUES(?,?,?,?)',
                       (position_key, round_id, 'evelyn_invoice', 'test'))
            db.commit()

    def mark_paid_historical(self, partner, positions, round_id, payout_ids):
        keys = [row.position_key for row in positions]
        for key in keys:
            self.assign_round(key, round_id)
        workflow.mark_paid_without_invoice(keys, date.today(), partner, {round_id}, set(payout_ids),
                                            'tester', 'historischer Sammelfall')

    def seed_historical_mh(self, count=3, round_id='GB-2026-001', payout_prefix='hist'):
        for index in range(count):
            self.seed('MH / TEST', f'{payout_prefix}{index}', f'order-hist-{index}', f'tx-hist-{index}')
        rows = workflow.positions()
        mh_rows = list(rows[rows.Partner == 'MH'].itertuples())
        self.mark_paid_historical('MH', mh_rows, round_id, [f'{payout_prefix}{i}' for i in range(count)])
        return [row.position_key for row in mh_rows]

    # 1. historical positions never appear as "new for next invoice"
    def test_historical_paid_without_invoice_positions_excluded_from_operational_view(self):
        self.seed_historical_mh(count=3)
        rows = workflow.positions()
        operational = studio_view.partner_rows(rows)
        self.assertTrue(operational[operational.Partner == 'MH'].empty)

    # 2. current new claim is exactly 0 while no new payout exists
    def test_current_new_claim_is_zero_without_new_payout(self):
        self.seed_historical_mh(count=3)
        rows = workflow.positions()
        operational = studio_view.partner_rows(rows)
        mh_new = operational[(operational.Partner == 'MH') & (operational.Art == 'Bestellung')
                              & ~operational.reviewed_at.astype(bool)]
        self.assertEqual(len(mh_new), 0)
        self.assertEqual(mh_new['Erlös_Brutto'].sum(), 0)

    # 3. the historical amount stays visible/traceable via position_workflow itself
    def test_historical_amount_stays_visible_in_position_workflow(self):
        keys = self.seed_historical_mh(count=3)
        rows = workflow.positions()
        historical = rows[rows.position_key.isin(keys)]
        self.assertEqual(len(historical), 3)
        self.assertTrue((historical[workflow.PAID_WITHOUT_INVOICE].astype(bool)).all())
        self.assertEqual(historical.Bearbeitungsstatus.unique().tolist(), ['bezahlt · Rechnung fehlt'])
        self.assertGreater(historical['Erlös_Brutto'].sum(), 0)

    # 4. no payment button path: none of the historical positions are ever
    # "awaiting payment" (reviewed_at true, paid_at false) in the operational view
    def test_no_payment_action_surface_for_historical_positions(self):
        self.seed_historical_mh(count=3)
        rows = workflow.positions()
        operational = studio_view.partner_rows(rows)
        awaiting_payment = operational[operational.reviewed_at.astype(bool) & ~operational.paid_at.astype(bool)]
        self.assertTrue(awaiting_payment[awaiting_payment.Partner == 'MH'].empty)

    # 5 & 6. a genuinely new MH payout shows up alone - historical 59-like
    # positions are never added to it, and never become payable again
    def test_new_payout_shows_only_new_positions_not_added_to_historical(self):
        self.seed_historical_mh(count=3)
        self.seed('MH / NEW', 'new1', 'order-new-1', 'tx-new-1')
        self.seed('MH / NEW', 'new2', 'order-new-2', 'tx-new-2')
        rows = workflow.positions()
        operational = studio_view.partner_rows(rows)
        mh_new = operational[(operational.Partner == 'MH') & (operational.Art == 'Bestellung')]
        self.assertEqual(len(mh_new), 2)
        self.assertEqual(set(mh_new.Bestellnummer), {'order-new-1', 'order-new-2'})
        self.assertGreater(mh_new['Erlös_Brutto'].sum(), 0)
        # the 3 historical ones still never reappear, still not payable
        historical = rows[rows.Bestellnummer.str.startswith('order-hist-')]
        self.assertTrue((historical[workflow.PAID_WITHOUT_INVOICE].astype(bool)).all())
        self.assertFalse(historical.partner_ready.any())

    # paid_without_invoice_at / partner_ready are both actually respected
    def test_paid_without_invoice_and_partner_ready_are_respected(self):
        keys = self.seed_historical_mh(count=1)
        rows = workflow.positions()
        row = rows[rows.position_key.isin(keys)].iloc[0]
        self.assertTrue(row[workflow.PAID_WITHOUT_INVOICE])
        self.assertFalse(row.partner_ready)

    # other partners remain unaffected by the Group-B fix
    def test_other_partners_unaffected(self):
        self.seed_historical_mh(count=2)
        self.seed('NB / TEST', 'nb1', 'order-nb-1', 'tx-nb-1')
        rows = workflow.positions()
        operational = studio_view.partner_rows(rows)
        nb_rows = operational[operational.Partner == 'NB']
        self.assertEqual(len(nb_rows), 1)
        self.assertEqual(nb_rows.iloc[0].Bestellnummer, 'order-nb-1')

    # historical round data (GB-2026-001/002) is never rewritten by this fix
    def test_historical_round_data_untouched(self):
        keys = self.seed_historical_mh(count=2, round_id='GB-2026-001')
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            before = sorted((dict(r) for r in db.execute(
                'SELECT position_key, round_id FROM group_b_round_positions WHERE round_id=?', ('GB-2026-001',))),
                key=lambda r: r['position_key'])
        studio_view.partner_rows(workflow.positions())
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            after = sorted((dict(r) for r in db.execute(
                'SELECT position_key, round_id FROM group_b_round_positions WHERE round_id=?', ('GB-2026-001',))),
                key=lambda r: r['position_key'])
        self.assertEqual(before, after)
        self.assertEqual({row['position_key'] for row in before}, set(keys))


if __name__ == '__main__':
    unittest.main()
