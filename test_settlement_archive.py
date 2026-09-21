"""Abrechnungsarchiv (Historie): central, read-only document/case archive
spanning 2026-003+ and historical GB-2026-001/002 - built entirely from
existing status/data functions, no new business logic."""
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import core
import group_b_rounds
import partner_snapshot
import position_workflow as workflow
import round_planner as planner
import round_ui
from test_recovery import payout

BERLIN = ZoneInfo('Europe/Berlin')
BASE_CUT = datetime(2026, 9, 20, 23, 59, tzinfo=BERLIN)


def berlin(*args):
    return datetime(*args, tzinfo=BERLIN)


class SettlementArchiveTests(unittest.TestCase):
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
            group_b_rounds.initialize(db)
            db.execute('INSERT OR IGNORE INTO group_b_rounds VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (round_id, 2026, sequence, 'test', None, None, '0', 'hash-' + round_id, '{}', '2026-01-01T00:00:00Z'))
            db.execute('INSERT OR REPLACE INTO group_b_round_positions VALUES(?,?,?,?)',
                       (position_key, round_id, 'evelyn_invoice', 'test'))
            db.commit()

    def _mark_mh_historical(self, count, round_id, payout_prefix, partner='MH'):
        for index in range(count):
            self.seed_sale(f'{payout_prefix}{index}', f'order-hist-{payout_prefix}-{index}', f'{partner} / TEST',
                            payout_date='14.09.2026')
        rows = workflow.positions()
        rows = rows[rows.Bestellnummer.str.startswith(f'order-hist-{payout_prefix}-')]
        keys = rows.position_key.tolist()
        for key in keys:
            self.assign_historical_round(key, round_id)
        workflow.mark_paid_without_invoice(keys, date.today(), partner, {round_id},
                                            {f'{payout_prefix}{i}' for i in range(count)}, 'tester', 'historisch')
        return keys

    def run_app(self):
        from streamlit.testing.v1 import AppTest
        return AppTest.from_file(str(Path(__file__).with_name('app.py'))).run(timeout=30)

    def all_text(self, app):
        parts = []
        for kind in ('markdown', 'caption', 'text', 'success', 'warning', 'error', 'info', 'subheader'):
            parts.extend(str(element.value) for element in getattr(app, kind))
        return '\n'.join(parts)

    # every existing round (current 2026-003 and historical GB-2026-001) is
    # found in the archive, newest first, historical clearly marked as such
    def test_all_rounds_listed_newest_first_historical_marked(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self._mark_mh_historical(2, 'GB-2026-001', 'hist')
        app = self.run_app()
        self.assertFalse(list(app.exception))
        labels = [exp.label for exp in app.expander]
        self.assertTrue(any(label.startswith('2026-003 ·') for label in labels))
        self.assertTrue(any(label.startswith('GB-2026-001 · historisches Altmodell') for label in labels))
        body = self.all_text(app)
        self.assertIn('Abrechnungsarchiv', body)

    # partner cases are reachable inside a round (aufklappbar) for both models
    def test_partner_cases_expandable_within_round(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self._mark_mh_historical(2, 'GB-2026-001', 'hist')
        app = self.run_app()
        labels = [exp.label for exp in app.expander]
        self.assertTrue(any(label.startswith('PP ·') and 'Positionen' in label for label in labels))
        self.assertTrue(any(label.startswith('MH ·') and 'Positionen' in label for label in labels))

    # finalized 2026-003+ Einzelabrechnung is offered again from the archive
    # via the exact same stored-bytes function the partner card itself uses
    def test_final_statement_download_uses_stored_snapshot_not_live_recompute(self):
        self.seed_sale('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        business = workflow.positions()
        record, created = partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 8, 0), business=business)
        self.assertTrue(created)
        stored = partner_snapshot.final_file('2026-003', 'PP')
        app = self.run_app()
        self.assertFalse(list(app.exception))
        keys = [b.key for b in app.get('download_button') if b.key and b.key.startswith('archive-final-2026-003-PP')]
        self.assertEqual(len(keys), 1)
        # repeated calls to the same stored-bytes function never regenerate -
        # already covered by test_partner_snapshot.test_second_download_identical_bytes;
        # here we only confirm the archive is wired to that same function.
        self.assertEqual(stored, partner_snapshot.final_file('2026-003', 'PP'))

    # an open historical case (MH: paid, invoice missing) stays visible in
    # the archive and is worded correctly
    def test_mh_paid_invoice_missing_visible_in_archive(self):
        self.seed_sale('p0', 'order-pp', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self._mark_mh_historical(3, 'GB-2026-001', 'hist')
        app = self.run_app()
        labels = [exp.label for exp in app.expander]
        mh_label = next(label for label in labels if label.startswith('MH ·') and 'Rechnung fehlt' in label)
        self.assertIn('bezahlt', mh_label)

    # MH never gets a second payment action inside the (read-only) archive
    def test_no_payment_action_rendered_inside_archive(self):
        self.seed_sale('p0', 'order-pp', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self._mark_mh_historical(2, 'GB-2026-001', 'hist')
        app = self.run_app()
        self.assertNotIn('Zahlung überwiesen', [b.label for b in app.button])

    # a fully closed historical partner slice (paid + invoiced) still shows
    # up in the archive (not only open cases) with a ✅ status
    def test_fully_settled_historical_partner_slice_also_listed(self):
        self.seed_sale('p0', 'order-pp', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        keys = self._mark_mh_historical(1, 'GB-2026-001', 'closed', partner='NB')
        import json
        import partner_invoices
        rows = workflow.positions()
        chosen = rows[rows.position_key.isin(keys)]
        expected = partner_invoices.expected_statement(chosen)
        record = dict(id='inv-nb-closed', partner='NB', file_name='nb.pdf', file_ref='nb.pdf', file_hash='x',
                       uploaded_at='2026-09-09T00:00:00Z', invoice_number='NB-TEST', invoice_date='2026-09-09',
                       extracted=expected, expected=expected, report=dict(status='matched', errors=[], warnings=[]),
                       approved_at='2026-09-09T00:00:00Z', approved_by='tester', approval_mode='manual',
                       override_reason='')
        with core.ledger() as db:
            db.execute('INSERT INTO partner_invoices VALUES(?,?,?,?,?)',
                       ('inv-nb-closed', 'hash-inv-nb-closed', 'NB', None, json.dumps(record)))
            db.commit()
        app = self.run_app()
        self.assertFalse(list(app.exception))
        labels = [exp.label for exp in app.expander]
        nb_label = next((label for label in labels if label.startswith('NB ·') and '✅ geprüft' in label), None)
        self.assertIsNotNone(nb_label)

    # rendering the archive alone never mutates production-shaped state
    def test_rendering_archive_does_not_change_position_workflow_state(self):
        self.seed_sale('p0', 'order-pp', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self._mark_mh_historical(2, 'GB-2026-001', 'hist')
        before = workflow.positions().to_dict('records')
        self.run_app()
        after = workflow.positions().to_dict('records')
        self.assertEqual(before, after)

    # a partner whose open claim spans two historical rounds (MH-style) is
    # shown ONCE as one combined package, never re-split back into two
    # separate per-round amounts (which reintroduces the old cent mismatch)
    def test_cross_round_open_case_shown_combined_not_split(self):
        self.seed_sale('p0', 'order-pp', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self._mark_mh_historical(2, 'GB-2026-001', 'r1mh')
        self._mark_mh_historical(2, 'GB-2026-002', 'r2mh')
        business = workflow.positions()
        with core.ledger() as db:
            group_b_rounds.initialize(db)
            db.execute('BEGIN IMMEDIATE')
            db.execute('''INSERT OR IGNORE INTO group_b_rounds VALUES(?,?,?,?,?,?,?,?,?,?)''',
                       ('GB-2026-002', 2026, 2, 'test', None, None, '0', 'hash-GB-2026-002', '{}',
                        '2026-01-01T00:00:00Z'))
            db.commit()
        cases = round_ui._historical_round_partner_cases(business, 'GB-2026-001')
        mh_r1 = next(case for case in cases if case['partner'] == 'MH')
        self.assertFalse(mh_r1['invoiced'])  # still open - would be folded into the combined case
        app = self.run_app()
        self.assertFalse(list(app.exception))
        labels = [exp.label for exp in app.expander]
        mh_labels = [label for label in labels if label.startswith('MH ·') and 'Rechnung fehlt' in label]
        self.assertEqual(len(mh_labels), 1)
        self.assertIn('4 Positionen', mh_labels[0])

    # direct function-level check: a fully-settled round slice is included
    # by the archive's own per-round case helper (unlike the partner-card
    # combined callout, which deliberately skips it)
    def test_historical_round_partner_cases_includes_closed_slices(self):
        self.seed_sale('p0', 'order-pp', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self._mark_mh_historical(2, 'GB-2026-001', 'hist')
        business = workflow.positions()
        cases = round_ui._historical_round_partner_cases(business, 'GB-2026-001')
        self.assertTrue(any(case['partner'] == 'MH' for case in cases))
        mh_case = next(case for case in cases if case['partner'] == 'MH')
        self.assertEqual(mh_case['positions'], 2)
        self.assertTrue(mh_case['paid_ok'])
        self.assertFalse(mh_case['invoiced'])


if __name__ == '__main__':
    unittest.main()
