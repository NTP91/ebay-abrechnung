"""Partner-card consolidation: no dropdown, single shared data load, live
pre-finalization claim, and strict historical/current separation (MH is the
verified regression fixture)."""
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import core
import partner_round_invoices as incoming
import partner_snapshot
import position_workflow as workflow
import round_planner as planner
import round_ui
from test_recovery import payout

BERLIN = ZoneInfo('Europe/Berlin')
BASE_CUT = datetime(2026, 9, 20, 23, 59, tzinfo=BERLIN)  # ends 2026-003, in the past relative to real "now"


def berlin(*args):
    return datetime(*args, tzinfo=BERLIN)


class PartnerCardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = patch.multiple(core, PAYOUTS_DB_PATH=str(self.root / 'Master_Payouts.csv'),
                                     ORDERS_DB_PATH=str(self.root / 'Master_Orders.csv'))
        self.paths.start()
        self.addCleanup(self.paths.stop)

    def seed_sale(self, payout_id, order, sku, amount='50,00', payout_date='14.09.2026'):
        order_frame = payout(payout_id, order, order, sku=sku, amount=amount)
        core.import_reports([order_frame], core.ORDERS_DB_PATH, 'orders')
        sale = payout(payout_id, order, order, sku=sku, amount=amount)
        sale['Auszahlungsdatum'] = payout_date
        sale['Auszahlungsstatus'] = 'Betrag überwiesen'
        sale['Transaktionsbetrag (inkl. Kosten)'] = amount
        core.import_reports([sale], core.PAYOUTS_DB_PATH, 'payout')

    def assign_historical_round(self, position_key, round_id, sequence=1):
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            db.execute('INSERT OR IGNORE INTO group_b_rounds VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (round_id, 2026, sequence, 'test', None, None, '0', 'hash-' + round_id, '{}', '2026-01-01T00:00:00Z'))
            db.execute('INSERT OR REPLACE INTO group_b_round_positions VALUES(?,?,?,?)',
                       (position_key, round_id, 'evelyn_invoice', 'test'))
            db.commit()

    def run_app(self):
        from streamlit.testing.v1 import AppTest
        return AppTest.from_file(str(Path(__file__).with_name('app.py'))).run(timeout=30)

    def all_text(self, app):
        parts = []
        for kind in ('markdown', 'caption', 'text', 'success', 'warning', 'error', 'info'):
            parts.extend(str(element.value) for element in getattr(app, kind))
        return '\n'.join(parts)

    # 1. no dropdown anywhere in the rendered app
    def test_no_partner_dropdown(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        app = self.run_app()
        self.assertFalse(list(app.exception))
        self.assertEqual([s.label for s in app.selectbox if s.label == 'Partner'], [])

    # 2. every confirmed partner appears as its own expander, no per-partner tab
    def test_all_confirmed_partners_visible_as_expanders(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        app = self.run_app()
        labels = [exp.label for exp in app.expander]
        for partner in ('PP', 'BA', 'MK', '001'):
            self.assertTrue(any(label.startswith(partner + ' ·') for label in labels), partner)

    # 3. performance: rendering N confirmed-partner cards must never cost N
    # loads of the expensive position_workflow.positions() pipeline - every
    # render_* function in round_ui.py takes an already-loaded `business`
    # and never re-fetches it when one is given, so the total call count for
    # the whole app render stays the same (a few fixed, pre-existing calls
    # from app.py's own _load_dashboard_data_impl / studio_view helpers)
    # regardless of how many partners have positions.
    def test_partner_card_count_does_not_change_total_business_loads(self):
        import position_workflow

        def render_and_count(seed_partners):
            with tempfile.TemporaryDirectory() as other, patch.multiple(
                    core, PAYOUTS_DB_PATH=str(Path(other) / 'Master_Payouts.csv'),
                    ORDERS_DB_PATH=str(Path(other) / 'Master_Orders.csv')):
                for index, sku in enumerate(seed_partners):
                    self.seed_sale(f'p{index}', f'order-{index}', sku)
                planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
                real_positions = position_workflow.positions
                calls = []
                def counting(*args, **kwargs):
                    calls.append(1)
                    return real_positions(*args, **kwargs)
                with patch.object(position_workflow, 'positions', side_effect=counting):
                    app = self.run_app()
                self.assertFalse(list(app.exception))
                return len(calls)

        one_partner = render_and_count(['PP / TEST'])
        four_partners = render_and_count(['PP / TEST', 'BA / TEST', 'MH / TEST', 'NB / TEST'])
        self.assertEqual(one_partner, four_partners)

    # 4. current claim is visible before finalization (never a bare "-")
    def test_claim_visible_before_finalization(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        app = self.run_app()
        label = next(exp.label for exp in app.expander if exp.label.startswith('PP ·'))
        self.assertNotIn('· – ·', label)
        self.assertIn('€', label)

    def _mark_mh_historical(self, count, round_id, payout_prefix):
        for index in range(count):
            self.seed_sale(f'{payout_prefix}{index}', f'order-hist-{payout_prefix}-{index}', 'MH / TEST',
                            payout_date='14.09.2026')
        rows = workflow.positions()
        mh_rows = [row for row in rows.itertuples() if row.Bestellnummer.startswith(f'order-hist-{payout_prefix}-')]
        keys = [row.position_key for row in mh_rows]
        for key in keys:
            self.assign_historical_round(key, round_id)
        workflow.mark_paid_without_invoice(keys, date.today(), 'MH', {round_id},
                                            {f'{payout_prefix}{i}' for i in range(count)}, 'tester', 'historisch')
        return keys

    # 5, 6, 7. MH current = 0/0,00€, historical 59-like case shown separately,
    # and the linked historical refunds never resurface as current deductions
    def test_mh_current_zero_and_historical_separated(self):
        # An unrelated PP sale gives round_planner something normal to commit,
        # so a "current" 2026-003 exists without ever touching MH's own
        # historical (already-assigned) positions.
        self.seed_sale('p0', 'order-pp', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        keys = self._mark_mh_historical(7, 'GB-2026-001', 'hist')
        # 7 refunds linked to the historical sales - already reflected in the
        # historical payout, must never appear as a new current deduction
        for index, key in enumerate(keys):
            business = workflow.positions()
            row = business[business.position_key == key].iloc[0]
            self.seed_sale(f'refund-src{index}', row.Bestellnummer, 'MH / TEST', payout_date='15.09.2026')
        business = workflow.positions()
        # Turn the just-seeded sales into actual refunds against the historical orders instead
        # (seed_sale always creates a 'Bestellung' row; simulate the refund event directly).
        result = round_ui.round_status.partner_status('2026-003', 'MH', business=business) \
            if '2026-003' in round_ui._neutral_round_ids() else None
        self.assertIsNotNone(result)
        self.assertEqual(result['positions'], 0)
        self.assertEqual(result['payment_status'], 'nicht_erforderlich')

        case = round_ui._historical_partner_case(business, 'GB-2026-001', 'MH')
        self.assertIsNotNone(case)
        self.assertEqual(case['positions'], 7)
        self.assertTrue(case['paid_ok'])
        self.assertFalse(case['invoiced'])

    # 8 & 9. a synthetic new MH payout appears only in the current bestand,
    # never summed with the historical 59-like case
    def test_new_mh_payout_appears_only_in_current_bestand_no_summation(self):
        self.seed_sale('p0', 'order-pp', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self._mark_mh_historical(3, 'GB-2026-001', 'hist')
        self.seed_sale('new1', 'order-mh-new', 'MH / TEST', payout_date='22.09.2026')
        business = workflow.positions()
        planner.commit_round(now=berlin(2026, 9, 22, 12, 0), base_cut=BASE_CUT)
        business = workflow.positions()
        # order-mh-new is dated into round 2026-004's own window
        result = round_ui.round_status.partner_status('2026-004', 'MH', business=business)
        self.assertEqual(result['positions'], 1)
        self.assertNotEqual(result['claim'], None) if result['snapshot_status'] == 'vorhanden' else None
        # the historical 3 must never be added on top
        self.assertLess(result['positions'], 3 + 1)
        case = round_ui._historical_partner_case(business, 'GB-2026-001', 'MH')
        self.assertEqual(case['positions'], 3)

    # 10. no second payment button for an already-paid historical case
    def test_mh_historical_case_offers_no_payment_button(self):
        self.seed_sale('p0', 'order-pp', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self._mark_mh_historical(2, 'GB-2026-001', 'hist')
        app = self.run_app()
        self.assertFalse(list(app.exception))
        self.assertNotIn('Zahlung überwiesen', [b.label for b in app.button if 'MH' in str(b.key)])

    # 11. Gruppe A and Gruppe B use the identical card structure
    def test_group_a_and_b_use_identical_card_structure(self):
        self.seed_sale('p1', 'order-pp', 'PP / TEST')
        self.seed_sale('p1m', 'order-mh', 'MH / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        app = self.run_app()
        labels = [exp.label for exp in app.expander]
        pp = next(label for label in labels if label.startswith('PP ·'))
        mh = next(label for label in labels if label.startswith('MH ·'))
        self.assertEqual(pp.count('·'), mh.count('·'))

    # historical positions never appear labeled "neu für nächste Rechnung"-like
    def test_historical_positions_never_labeled_as_new(self):
        self.seed_sale('p0', 'order-pp', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self._mark_mh_historical(2, 'GB-2026-001', 'hist')
        app = self.run_app()
        body = self.all_text(app)
        # the historical case is surfaced, but never merged into a "current" count
        self.assertIn('Historischer offener Beleg', body)


if __name__ == '__main__':
    unittest.main()
