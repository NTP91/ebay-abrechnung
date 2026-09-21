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

    def test_app_loads_with_new_tab_and_no_exception(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        app = self.run_app()
        self.assertFalse(list(app.exception))
        labels = [tab.label for tab in app.tabs]
        self.assertIn('Runde 2026-003+', labels)

    def test_round_003_shown_in_abwicklung_with_concrete_blocker(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        app = self.run_app()
        self.assertFalse(list(app.exception))
        body = self.all_text(app)
        self.assertIn('2026-003', body)
        self.assertIn('in Abwicklung', body)
        self.assertIn('Finale Einzelabrechnung fehlt', body)

    def test_zero_position_partner_shows_nichts_erforderlich(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        app = self.run_app()
        self.assertFalse(list(app.exception))
        matrix = next(el.value for el in app.dataframe if list(el.value.index) == ['Rechnung', 'Zahlung', 'Gutschrift', 'Status'])
        self.assertIn('MK', matrix.columns)  # confirmed Gruppe-A partner with 0 positions in 2026-003
        self.assertEqual(matrix.loc['Status', 'MK'], 'nichts erforderlich')

    def test_finalized_and_paid_partner_shows_completed_status(self):
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
        body = self.all_text(app)
        self.assertIn('abgeschlossen', body)
        self.assertIn('bezahlt am', body)

    def test_no_debug_payload_leaks_in_new_tab(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        app = self.run_app()
        self.assertFalse(list(app.exception))
        self.assertEqual(len(app.json), 0)


if __name__ == '__main__':
    unittest.main()
