"""Globale Suche (Übersicht): read-only navigation across current 2026-003+
and historical GB-2026-001/002 cases - no status/payment/round/snapshot/
recovery/assignment change, ever."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import api_holds
import core
import group_b_rounds as rounds
import position_workflow
import round_planner as planner
import studio_view
from test_api_holds import movement, snapshot
from test_recovery import payout

import global_search

BERLIN = ZoneInfo('Europe/Berlin')
BASE_CUT = datetime(2026, 9, 20, 23, 59, tzinfo=BERLIN)


def berlin(*args):
    return datetime(*args, tzinfo=BERLIN)


class GlobalSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        paths = patch.multiple(core, PAYOUTS_DB_PATH=str(root / 'Master_Payouts.csv'),
                               ORDERS_DB_PATH=str(root / 'Master_Orders.csv'))
        paths.start(); self.addCleanup(paths.stop)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('HTTP forbidden'))
        network.start(); self.addCleanup(network.stop)
        # studio_view.invoice_history() resolves a real Belegnummer only for
        # already-known invoice_ids via this fixed lookup table; register
        # this test's synthetic RE0090 invoice_id the same way.
        document_numbers = patch.dict(studio_view.LEXWARE_DOCUMENT_NUMBERS, {'invoice-re0090': 'RE0090'})
        document_numbers.start(); self.addCleanup(document_numbers.stop)

        frames = [
            payout('p1', 'sale-r1-mh', 'r1-mh', sku='MH / BÜR / A', amount='100,00'),
            payout('p1', 'sale-r1-nb', 'r1-nb', sku='NB / A', amount='200,00'),
            payout('p1', 'sale-r1-hold', 'r1-hold', sku='MH / H', amount='20,00'),
            payout('p2', 'sale-r2', 'r2-mh', sku='MH / B', amount='50,00'),
            payout('p2', 'sale-r2-nb', 'r2-nb', sku='NB / B', amount='40,00'),
            payout('p2', 'sale-zero', 'r2-zero', sku='MH / C', amount='30,00'),
            payout('p2', 'refund-zero', 'r2-zero', sku='MH / C', amount='-30,00', kind='Rückerstattung'),
            payout('p2', 'refund-late', 'r1-nb', sku='NB / A', amount='-10,00', kind='Rückerstattung'),
        ]
        for frame in frames:
            frame['Artikelnummer'] = 'item-' + frame.iloc[0]['Bestellnummer']
            frame['Transaktionsbetrag (inkl. Kosten)'] = frame['Betrag abzügl. Kosten']
            frame['Auszahlungsdatum'] = '08.09.2026'; frame['Auszahlungsstatus'] = 'Betrag überwiesen'
        core.import_reports(frames, core.ORDERS_DB_PATH, 'orders')
        core.import_reports(frames, core.PAYOUTS_DB_PATH, 'payout')
        api_holds.ingest(root, snapshot([movement(order='r1-hold', identifier='DISPUTE_HOLD-1')]))
        master = core.load_master_data(); core.sync_status(master)
        payload = core.build_invoice_payload(master, 'p1', 'contact', True)
        self.invoice_id = 'invoice-re0090'
        with core.ledger() as db:
            db.execute("UPDATE payouts SET invoice_id=?,attempt='created',snapshot=? WHERE id='p1'",
                       (self.invoice_id, json.dumps(payload)))
            db.commit()
        self.business = position_workflow.positions()
        self.current = self.business[self.business.Bestellnummer.isin(['r2-mh', 'r2-nb']) & (self.business.Art == 'Bestellung')]
        self.amount = studio_view._snapshot_total(payload)
        self.invoices = {self.invoice_id: {'Belegnummer': 'RE0090', 'Betrag': self.amount,
                                            'Payouts': ['p1'], 'discarded': False}}
        rounds.bootstrap(self.business, self.current, self.invoices)

        # NB0576-style: an approved, paid historical partner invoice for NB's
        # r1-nb position (GB-2026-001's own closed slice).
        nb_row = self.business[(self.business.Bestellnummer == 'r1-nb') & (self.business.Art == 'Bestellung')]
        with core.ledger() as db:
            db.execute('BEGIN IMMEDIATE')
            items = [{'key': row.position_key, 'gross': str(row['Erlös_Brutto'])} for _, row in nb_row.iterrows()]
            record = dict(id='invoice-nb0576', partner='NB', invoice_number='NB0576',
                          approved_at='2026-09-09T00:00:00Z', expected={'items': items})
            db.execute('INSERT INTO partner_invoices VALUES(?,?,?,?,?)',
                       ('invoice-nb0576', 'hash-nb0576', 'NB', None, json.dumps(record)))
            for item in items:
                db.execute('INSERT INTO partner_invoice_positions VALUES(?,?)', (item['key'], 'invoice-nb0576'))
            for _, row in nb_row.iterrows():
                db.execute('INSERT INTO position_workflow(position_key,reviewed_at,paid_at,received_at,closed_at,source) '
                           'VALUES(?,?,?,?,?,?)',
                           (row.position_key, '2026-09-08', '2026-09-09', None, None,
                            position_workflow.source_snapshot(row)))
            db.commit()

        # RE0089-style: a discarded/verworfen Evelyn draft, unrelated to any round.
        with core.ledger() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''INSERT INTO discarded_invoices VALUES(?,?,?,?)''',
                       ('invoice-re0089', 'RE0089 · verworfener Testbeleg', '2026-09-04T12:00:00Z',
                        json.dumps([{'id': 'p0-old', 'snapshot': json.dumps({
                            'voucherDate': '2026-09-01T00:00:00Z',
                            'lineItems': [{'description': 'eBay-Bestellnummer: old-order\nSKU: NB / OLD'}]})}])))
            db.commit()

        # A current 2026-003+ round with a reviewed, paid, invoiced partner
        # (Gruppe A · PP) so a Partnerrechnungsnummer search has a real hit.
        self.seed_sale('p3', 'order-pp', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self.business = position_workflow.positions()
        import partner_snapshot
        partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 8, 0), business=self.business)
        with core.ledger() as db:
            partner_snapshot.initialize(db)
            import partner_round_invoices
            partner_round_invoices.initialize(db)
            snap = db.execute("SELECT final_amount,line_items,snapshot_hash FROM partner_round_snapshots "
                              "WHERE round_id='2026-003' AND partner='PP'").fetchone()
            db.execute('''INSERT INTO partner_round_invoices VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                '2026-003', 'PP', 'PP-RECHNUNG-1', '2026-09-21', 'hash', 'pp.pdf', b'bytes',
                '2026-09-21T00:00:00Z', snap['final_amount'], snap['snapshot_hash'],
                '2026-09-21T00:00:00Z', None, None, None, None))
            db.commit()
        self.business = position_workflow.positions()

    def seed_sale(self, payout_id, order, sku, amount='60,00', payout_date='19.09.2026'):
        order_frame = payout(payout_id, order, order, sku=sku, amount=amount)
        core.import_reports([order_frame], core.ORDERS_DB_PATH, 'orders')
        sale = payout(payout_id, order, order, sku=sku, amount=amount)
        sale['Auszahlungsdatum'] = payout_date
        sale['Auszahlungsstatus'] = 'Betrag überwiesen'
        sale['Transaktionsbetrag (inkl. Kosten)'] = amount
        core.import_reports([sale], core.PAYOUTS_DB_PATH, 'payout')

    def search(self, query):
        return global_search.search(query, business=self.business)

    def test_bestellnummer_exact(self):
        results = self.search('r1-mh')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['partner'], 'MH')
        self.assertIn('Bestellnummer', results[0]['match_types'])

    def test_sku_exact(self):
        results = self.search('NB / A')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['partner'], 'NB')
        self.assertIn('SKU', results[0]['match_types'])

    def test_sku_partial(self):
        results = self.search('BÜR')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['partner'], 'MH')
        self.assertIn('SKU', results[0]['match_types'])

    def test_sku_partial_too_short_finds_nothing(self):
        self.assertEqual(self.search('B'), [])

    def test_partnerrechnungsnummer_neutral_round(self):
        results = self.search('PP-RECHNUNG-1')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['partner'], 'PP')
        self.assertEqual(results[0]['round_id'], '2026-003')
        self.assertIn('Partnerrechnung', results[0]['match_types'])

    def test_historical_partnerrechnungsnummer_nb0576(self):
        results = self.search('NB0576')
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result['kind'], 'historisch')
        self.assertEqual(result['partner'], 'NB')
        self.assertEqual(result['round_label'], rounds.ROUND_ONE)
        self.assertEqual(result['invoice_icon'], '✅')
        self.assertEqual(result['payment_icon'], '✅')

    def test_evelyn_beleg_re0090(self):
        results = self.search('RE0090')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['kind'], 'evelyn_beleg')
        self.assertEqual(results[0]['round_id'], rounds.ROUND_ONE)

    def test_discarded_re0089_marked_and_not_navigable(self):
        results = self.search('RE0089')
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result['kind'], 'verworfen')
        self.assertIn('verworfen', result['invoice_text'])
        self.assertIsNone(result['nav'])

    def test_payout_with_multiple_positions_grouped_by_partner(self):
        results = self.search('p1')
        partners = sorted(r['partner'] for r in results if r['partner'])
        self.assertEqual(partners, ['MH', 'NB'])
        # Grouped, never merged into one artificial combined case.
        self.assertEqual(len(results), len(set((r['kind'], r['round_id'], r['partner']) for r in results)))

    def test_historical_case_found(self):
        results = self.search('r1-mh')
        self.assertEqual(results[0]['kind'], 'historisch')

    def test_current_2026_003_case_found(self):
        results = self.search('order-pp')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['kind'], 'aktuelle_runde')
        self.assertEqual(results[0]['round_id'], '2026-003')

    def test_no_hit_returns_empty_list_no_error(self):
        self.assertEqual(self.search('does-not-exist-anywhere-xyz'), [])
        self.assertEqual(self.search(''), [])
        self.assertEqual(self.search('   '), [])

    def test_search_never_changes_any_data(self):
        before = position_workflow.positions().to_dict('records')
        for query in ('r1-mh', 'NB0576', 'RE0090', 'RE0089', 'p1', '2026-003', 'order-pp', 'nothing'):
            self.search(query)
        after = position_workflow.positions().to_dict('records')
        self.assertEqual(before, after)

    def test_multiple_hits_not_falsely_merged(self):
        """r1-mh and r2-mh both belong to MH but different rounds/cases -
        searching a payout spanning both must never collapse them into one
        artificial combined row that hides which round each came from."""
        results = self.search('p2')
        mh_results = [r for r in results if r['partner'] == 'MH']
        # r2-mh/r2-zero -> GB-2026-002 (part of MH's still-open combined case);
        # exactly one MH entry, not silently duplicated per matched row.
        self.assertEqual(len(mh_results), 1)
        nb_results = [r for r in results if r['partner'] == 'NB']
        self.assertEqual(len(nb_results), 1)
        self.assertNotEqual(mh_results[0]['round_id'], nb_results[0]['round_id'])


if __name__ == '__main__':
    unittest.main()
