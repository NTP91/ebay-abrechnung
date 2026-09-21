import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import core
import partner_round_invoices as incoming
import partner_snapshot
import round_planner as planner
from test_recovery import payout

BERLIN = ZoneInfo('Europe/Berlin')
# Fixed anchor ending 2026-003 in the past relative to real wall-clock "now"
# (this suite is meant to run on/after 2026-09-21), so round_status.py's own
# real-time cut_passed check is exercised without mocking time.
BASE_CUT = datetime(2026, 9, 20, 23, 59, tzinfo=BERLIN)
MATRIX_ROWS = ['Einzelabrechnung', 'Rechnung', 'Zahlung', 'Gutschrift', 'Status']


def berlin(*args):
    return datetime(*args, tzinfo=BERLIN)


class RoundUiSmokeTests(unittest.TestCase):
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

    def run_app(self):
        from streamlit.testing.v1 import AppTest
        return AppTest.from_file(str(Path(__file__).with_name('app.py'))).run(timeout=30)

    def all_text(self, app):
        parts = []
        for kind in ('markdown', 'caption', 'text', 'success', 'warning', 'error', 'info', 'header', 'subheader'):
            parts.extend(str(element.value) for element in getattr(app, kind))
        for metric in app.metric:
            parts.append(f'{metric.label} {metric.value}')
        return '\n'.join(parts)

    def round_matrix(self, app):
        return next(el.value for el in app.dataframe if list(el.value.index) == MATRIX_ROWS)

    def test_old_round_tab_removed_no_exception(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        app = self.run_app()
        self.assertFalse(list(app.exception))
        labels = [tab.label for tab in app.tabs]
        self.assertNotIn('Runde 2026-003+', labels)
        for label in ['Übersicht', 'Gruppe A', 'Gruppe B', 'Offene Positionen', 'Historie']:
            self.assertIn(label, labels)

    def test_abrechnungsrunden_block_in_uebersicht_with_concrete_blocker(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        app = self.run_app()
        self.assertFalse(list(app.exception))
        body = self.all_text(app)
        self.assertIn('Abrechnungsrunden', body)
        expander_labels = [exp.label for exp in app.expander]
        self.assertTrue(any('2026-003' in label and 'in Abwicklung' in label for label in expander_labels))
        self.assertIn('PP · Finale Einzelabrechnung fehlt', body)

    def test_matrix_is_compact_icon_only(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        app = self.run_app()
        self.assertFalse(list(app.exception))
        matrix = self.round_matrix(app)
        self.assertEqual(list(matrix.index), MATRIX_ROWS)
        for value in matrix.values.flatten():
            self.assertNotIn('Finale Einzelabrechnung fehlt', value)
            self.assertLessEqual(len(value), 2)  # a single emoji, no long label

    def test_no_banned_symbols_in_matrix_or_body(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        app = self.run_app()
        self.assertFalse(list(app.exception))
        matrix = self.round_matrix(app)
        allowed = {'✅', '❌', '➖'}
        for value in matrix.values.flatten():
            self.assertIn(value, allowed)
        body = self.all_text(app)
        self.assertNotIn('⏳', body)
        self.assertNotIn('🟠', body)

    def test_2026_003_labeled_as_first_shared_round(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        app = self.run_app()
        expander_labels = [exp.label for exp in app.expander]
        self.assertTrue(any('2026-003' in label and 'erste gemeinsame Runde' in label for label in expander_labels))

    def test_partner_001_is_a_column_not_confused_with_a_round(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        app = self.run_app()
        matrix = self.round_matrix(app)
        self.assertIn('001', matrix.columns)

    def test_zero_position_partner_shows_all_dash_never_a_fake_checkmark(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        app = self.run_app()
        matrix = self.round_matrix(app)
        self.assertIn('MK', matrix.columns)  # confirmed Gruppe-A partner with 0 positions in 2026-003
        self.assertEqual(matrix.loc['Einzelabrechnung', 'MK'], '➖')
        self.assertEqual(matrix.loc['Rechnung', 'MK'], '➖')
        self.assertEqual(matrix.loc['Zahlung', 'MK'], '➖')
        self.assertEqual(matrix.loc['Gutschrift', 'MK'], '➖')
        self.assertEqual(matrix.loc['Status', 'MK'], '➖')

    def test_finalized_and_paid_partner_shows_ok_icons_and_round_completed(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        snap, created = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        self.assertTrue(created)
        import json
        from test_invoice_support import invoice_csv
        line_items = json.loads(snap['line_items'])
        blob = invoice_csv(dict(items=line_items, total=snap['final_amount']), 'INV-1')
        record, report = incoming.check_and_review('2026-003', 'PP', 'invoice.csv', blob)
        self.assertEqual(report['status'], 'matched', report)
        incoming.confirm_payment('2026-003', 'PP')
        app = self.run_app()
        self.assertFalse(list(app.exception))
        matrix = self.round_matrix(app)
        self.assertEqual(matrix.loc['Einzelabrechnung', 'PP'], '✅')
        self.assertEqual(matrix.loc['Rechnung', 'PP'], '✅')
        self.assertEqual(matrix.loc['Zahlung', 'PP'], '✅')
        self.assertIn('abgeschlossen', self.all_text(app))

    def test_historical_rounds_shown_separately_and_expandable(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            db.execute("INSERT INTO group_b_rounds VALUES('GB-2026-001',2026,1,'test',NULL,NULL,'0','h','{}','2026-01-01T00:00:00Z')")
            db.commit()
        app = self.run_app()
        self.assertFalse(list(app.exception))
        body = self.all_text(app)
        self.assertIn('Historische Runden (altes Modell)', body)
        self.assertIn('GB-2026-001', body)
        expander_labels = [exp.label for exp in app.expander]
        self.assertTrue(any('GB-2026-001' in label for label in expander_labels))

    def test_legacy_group_b_panel_never_shows_a_neutral_round(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        app = self.run_app()
        self.assertFalse(list(app.exception))
        # No dataframe/table anywhere should list the neutral round id under
        # the legacy per-partner "Gesamtsicht je Partner" round-id column.
        for element in app.dataframe:
            if 'Partner' in getattr(element.value, 'columns', []) and 'round_id' in str(element.value.columns).lower():
                self.assertNotIn('2026-003', element.value.astype(str).values)

    def test_no_debug_payload_leaks(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        app = self.run_app()
        self.assertFalse(list(app.exception))
        self.assertEqual(len(app.json), 0)


if __name__ == '__main__':
    unittest.main()
