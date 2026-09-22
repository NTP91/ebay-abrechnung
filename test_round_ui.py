import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import core
import partner_invoices
import partner_round_invoices as incoming
import partner_snapshot
import position_workflow as workflow
import round_planner as planner
import round_ui
from test_invoice_support import invoice_csv as legacy_invoice_csv
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

    def test_historical_matrix_ignores_hold_reserve_role_position(self):
        # A production regression: bootstrap() also parks hold_reserve
        # positions (never an active claim) in the same historical round as
        # the genuinely paid-without-invoice ones - they must never drag
        # Zahlung back to ❌ for the real historical case.
        self.seed_sale('p1', 'order-mh', 'MH / TEST')
        self.seed_sale('p2', 'order-mh-hold', 'MH / TEST')
        rows = workflow.positions()
        paid_row = rows[rows.Bestellnummer == 'order-mh'].iloc[0]
        held_row = rows[rows.Bestellnummer == 'order-mh-hold'].iloc[0]
        self.assign_historical_round(paid_row.position_key, 'GB-2026-001')
        workflow.mark_paid_without_invoice([paid_row.position_key], date.today(), 'MH', {'GB-2026-001'}, {'p1'},
                                            'tester', 'historischer Sammelfall')
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            db.execute('INSERT OR REPLACE INTO group_b_round_positions VALUES(?,?,?,?)',
                       (held_row.position_key, 'GB-2026-001', 'hold_reserve', 'test'))
            db.commit()
        business = workflow.positions()
        frame, blockers, header_icon = round_ui._historical_matrix(business, 'GB-2026-001')
        self.assertEqual(frame.loc['Zahlung', 'MH'], '✅')

    def test_historical_matrix_mh_paid_without_invoice(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST')
        row = workflow.positions().iloc[0]
        self.assign_historical_round(row.position_key, 'GB-2026-001')
        workflow.mark_paid_without_invoice([row.position_key], date.today(), 'MH', {'GB-2026-001'}, {'p1'},
                                            'tester', 'historischer Sammelfall')
        business = workflow.positions()
        frame, blockers, header_icon = round_ui._historical_matrix(business, 'GB-2026-001')
        self.assertIsNotNone(frame)
        self.assertEqual(frame.loc['Einzelabrechnung', 'MH'], '➖')
        self.assertEqual(frame.loc['Rechnung', 'MH'], '❌')
        self.assertEqual(frame.loc['Zahlung', 'MH'], '✅')
        self.assertEqual(frame.loc['Status', 'MH'], '❌')
        self.assertEqual(header_icon, '❌')
        self.assertIn('MH · Rechnung fehlt', blockers)
        self.assertNotIn('MH · Zahlung offen', blockers)

    def test_historical_matrix_fully_settled_partner_shows_ok(self):
        self.seed_sale('p1', 'order-ba', 'BA / TEST', payout_date='01.09.2026')
        row = workflow.positions().iloc[0]
        self.assign_historical_round(row.position_key, 'GB-2026-002', sequence=2)
        expected = partner_invoices.expected_statement(workflow.positions())
        record, _ = partner_invoices.upload('BA', 'invoice.csv', legacy_invoice_csv(expected))
        self.assertEqual(record['report']['status'], 'matched', record['report'])
        partner_invoices.approve(record['id'], 'tester')
        workflow.confirm([row.position_key], 'partner_paid', date.today())
        business = workflow.positions()
        frame, blockers, header_icon = round_ui._historical_matrix(business, 'GB-2026-002')
        self.assertEqual(frame.loc['Rechnung', 'BA'], '✅')
        self.assertEqual(frame.loc['Zahlung', 'BA'], '✅')
        self.assertEqual(frame.loc['Status', 'BA'], '✅')
        self.assertEqual(header_icon, '✅')
        self.assertEqual(blockers, [])

    def test_historical_matrix_no_assigned_positions_is_neutral(self):
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            db.execute("INSERT INTO group_b_rounds VALUES('GB-2026-001',2026,1,'test',NULL,NULL,'0','h','{}','2026-01-01T00:00:00Z')")
            db.commit()
        frame, blockers, header_icon = round_ui._historical_matrix(workflow.positions(), 'GB-2026-001')
        self.assertIsNone(frame)
        self.assertEqual(blockers, [])
        self.assertEqual(header_icon, '')

    def test_historical_header_icon_and_matrix_render_in_app(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST')
        row = workflow.positions().iloc[0]
        self.assign_historical_round(row.position_key, 'GB-2026-001')
        workflow.mark_paid_without_invoice([row.position_key], date.today(), 'MH', {'GB-2026-001'}, {'p1'},
                                            'tester', 'historischer Sammelfall')
        app = self.run_app()
        self.assertFalse(list(app.exception))
        expander_labels = [exp.label for exp in app.expander]
        self.assertTrue(any(label.startswith('❌ GB-2026-001') for label in expander_labels))
        matrix = next(el.value for el in app.dataframe
                      if list(el.value.index) == MATRIX_ROWS and 'MH' in el.value.columns
                      and el.value.loc['Rechnung', 'MH'] == '❌' and el.value.loc['Zahlung', 'MH'] == '✅')
        self.assertEqual(matrix.loc['Status', 'MH'], '❌')
        body = self.all_text(app)
        self.assertIn('MH · Rechnung fehlt', body)

    def test_historical_document_access_still_works(self):
        self.seed_sale('p1', 'order-ba', 'BA / TEST', payout_date='01.09.2026')
        row = workflow.positions().iloc[0]
        self.assign_historical_round(row.position_key, 'GB-2026-002', sequence=2)
        expected = partner_invoices.expected_statement(workflow.positions())
        record, _ = partner_invoices.upload('BA', 'invoice.csv', legacy_invoice_csv(expected, 'BA0001'))
        partner_invoices.approve(record['id'], 'tester')
        with core.ledger() as db:
            db.execute('INSERT OR IGNORE INTO partner_invoice_rounds VALUES(?,?)', (record['id'], 'GB-2026-002'))
            db.commit()
        app = self.run_app()
        self.assertFalse(list(app.exception))
        body = self.all_text(app)
        self.assertIn('Verknüpfte Partnerbelege', body)
        self.assertIn('BA0001', body)

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

    # Fail-soft: a technically broken broker-commission status must not take
    # down the 'Abrechnungsrunden' block (or the rest of the page) - only
    # its own line degrades to a visible warning.
    def test_broker_status_failure_shows_warning_not_a_crash(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        self.seed_sale('p1m', 'order-mh', 'MH / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            db.execute("INSERT INTO group_b_rounds VALUES('GB-2026-001',2026,1,'test',NULL,NULL,'0','h','{}','2026-01-01T00:00:00Z')")
            db.commit()
        import broker_commission
        with patch.object(broker_commission, 'status', side_effect=RuntimeError('DB nicht erreichbar')):
            app = self.run_app()
        self.assertFalse(list(app.exception))
        body = self.all_text(app)
        # The degraded warning is shown instead of the normal broker line...
        self.assertIn('Vermittlungsprovision-Status derzeit nicht verfügbar', body)
        # ...the rest of the round overview keeps rendering (round id, matrix,
        # partner cards)...
        self.assertIn('Abrechnungsrunden', body)
        self.assertIn('2026-003', body)
        self.round_matrix(app)  # raises if the matrix table is missing
        # ...and the pre-existing historical GB-2026-001 view is untouched.
        self.assertIn('GB-2026-001', body)
        # No internal error text/traceback fragments leak into the UI.
        self.assertNotIn('RuntimeError', body)
        self.assertNotIn('Traceback', body)


if __name__ == '__main__':
    unittest.main()
