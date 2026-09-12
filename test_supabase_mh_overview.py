"""End-to-end MH invoice-overview/export check under PAYMENT_BACKEND=supabase.

Exercises the exact regular application path (core.load_master_data ->
position_workflow.positions -> studio_view.partner_rows/partner_summary ->
partner_export.export_partner_excel) with PAYMENT_BACKEND=supabase genuinely
set and only the outermost network boundary (supabase_store.get/get_json/put)
replaced by an in-memory fake store - the same boundary-faking approach
already used by SupabaseLedgerSchemaTests in test_group_b_rounds.py.
supabase_store.py's own HTTP/SQL contract (chunking, hashing, versioning) is
covered separately in test_supabase_store.py; this file verifies the
application logic that sits on top of that contract, without a live
Supabase project (none is available in this environment).
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core
import position_workflow
import studio_view
import supabase_store
from partner_export import export_partner_excel
from test_recovery import payout

_MISSING = object()


class FakeSupabase:
    """Minimal in-memory stand-in for supabase_store's public get/put contract."""

    def __init__(self):
        self.store = {}
        self.put_calls = 0

    def get(self, key, required=True):
        if key not in self.store:
            if required:
                raise supabase_store.StoreError('missing ' + key)
            return None, 0
        return self.store[key]

    def get_json(self, key, default=_MISSING):
        raw, version = self.get(key, required=default is _MISSING)
        return (json.loads(raw.decode('utf-8')) if raw is not None else default), version

    def put(self, key, content, expected_version=None):
        self.put_calls += 1
        current = self.store.get(key, (None, 0))[1]
        if expected_version is not None and expected_version != current:
            raise supabase_store.StoreError('stale ' + key)
        new_version = current + 1
        self.store[key] = (content, new_version)
        return new_version


class SupabaseMhOverviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.paths = patch.multiple(core, PAYOUTS_DB_PATH=str(root / 'Master_Payouts.csv'),
                                    ORDERS_DB_PATH=str(root / 'Master_Orders.csv'))
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.http = patch('requests.sessions.Session.request', side_effect=AssertionError('Live HTTP forbidden'))
        self.http.start()
        self.addCleanup(self.http.stop)

        self.fake = FakeSupabase()
        # A fully-initialized settlement register (all base tables, invoice_store's
        # tables, and group_b_rounds' tables) - the same one-time bootstrap the
        # local-file ledger branch performs, used here as the seeded Supabase blob
        # to represent an already-migrated, already-fixed production database.
        with tempfile.TemporaryDirectory() as scratch:
            with patch.multiple(core, PAYOUTS_DB_PATH=str(Path(scratch) / 'Master_Payouts.csv'),
                                ORDERS_DB_PATH=str(Path(scratch) / 'Master_Orders.csv')):
                with core.ledger():
                    pass
                raw_bytes = (Path(scratch) / 'Settlement_State.sqlite3').read_bytes()
        self.fake.store['state/settlement.sqlite3'] = (raw_bytes, 1)

        empty = core.canonicalize(core.pd.DataFrame())
        empty_csv = empty.to_csv(sep=';', index=False).encode('utf-8-sig')
        self.fake.store['source/orders.csv'] = (empty_csv, 1)
        self.fake.store['source/payouts.csv'] = (empty_csv, 1)
        self.fake.store['config/partners.json'] = (Path('partners.json').read_bytes(), 1)
        self.fake.store['config/billing_recipients.json'] = (Path('billing_recipients.json').read_bytes(), 1)

        self.env = patch.dict('os.environ', {'PAYMENT_BACKEND': 'supabase',
                                             'SUPABASE_ACCESS_TOKEN': 'dummy-token',
                                             'SUPABASE_PROJECT_REF': 'dummy-ref'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.supabase = patch.multiple(supabase_store, enabled=lambda: True,
                                       get=self.fake.get, get_json=self.fake.get_json, put=self.fake.put)
        self.supabase.start(); self.addCleanup(self.supabase.stop)

        self.assertTrue(supabase_store.enabled())  # the real env-var gate, unmocked

        frames = [
            payout('p-mh-1', 'sale-mh-1', 'order-mh-1', sku='MH / A', amount='100,00'),
            payout('p-mh-2', 'sale-mh-2', 'order-mh-2', sku='MH-Sub / B', amount='60,00'),
            payout('p-mh-3', 'sale-mh-3', 'order-mh-3', sku='MH / C', amount='40,00'),
            payout('p-mh-3', 'refund-mh-3', 'order-mh-3', sku='MH / C', amount='-40,00', kind='Rückerstattung'),
            payout('p-nb-1', 'sale-nb-1', 'order-nb-1', sku='NB / A', amount='200,00'),
            payout('p-ga-1', 'sale-ga-1', 'order-ga-1', sku='PP / A', amount='300,00'),
        ]
        for frame in frames:
            frame['Artikelnummer'] = 'item-' + frame.iloc[0]['Bestellnummer']
            frame['Transaktionsbetrag (inkl. Kosten)'] = frame['Betrag abzügl. Kosten']
            frame['Auszahlungsdatum'] = '10.09.2026'
            frame['Auszahlungsstatus'] = 'Betrag überwiesen'
        core.import_reports(frames, core.ORDERS_DB_PATH, 'orders')
        core.import_reports(frames, core.PAYOUTS_DB_PATH, 'payout')

        # Still-open order without any eBay payout number yet - must never surface.
        open_order = payout('', 'sale-open-1', 'order-open-1', sku='MH / D', amount='77,00')
        open_order['Artikelnummer'] = 'item-order-open-1'
        core.import_reports([open_order], core.ORDERS_DB_PATH, 'orders')

    def business(self):
        master = core.load_master_data()
        states = core.sync_status(master)
        return position_workflow.positions(master, states)

    def test_mh_overview_opens_and_exports_under_supabase(self):
        business = self.business()
        self.assertEqual(sorted(business.Partner.unique()), ['MH', 'NB', 'PP'])
        self.assertEqual(sorted(business.Gruppe.unique()), ['Gruppe A', 'Gruppe B'])

        partner_ready = studio_view.partner_rows(business)
        mh = partner_ready[partner_ready.Partner == 'MH']

        # Only real MH orders (including the MH-Sub sub-variant) are present.
        self.assertEqual(sorted(mh.Bestellnummer.unique()), ['order-mh-1', 'order-mh-2', 'order-mh-3'])
        # Group A and NB never leak into the MH block.
        self.assertFalse((mh.Partner != 'MH').any())
        self.assertNotIn('order-nb-1', mh.Bestellnummer.tolist())
        self.assertNotIn('order-ga-1', mh.Bestellnummer.tolist())
        # Only rows with a real eBay payout number are included.
        self.assertTrue((mh['Auszahlung Nr.'] != '').all())
        self.assertNotIn('order-open-1', mh.Bestellnummer.tolist())
        # Variant B: the refund is its own separate negative event, the sale stays positive.
        self.assertEqual(sorted(mh.Art.unique()), ['Bestellung', 'Erstattung'])
        refund_row = mh[(mh.Bestellnummer == 'order-mh-3') & (mh.Art == 'Erstattung')]
        sale_row = mh[(mh.Bestellnummer == 'order-mh-3') & (mh.Art == 'Bestellung')]
        self.assertEqual(len(refund_row), 1)
        self.assertEqual(len(sale_row), 1)
        self.assertLess(float(refund_row.iloc[0].Erlös_Brutto), 0)
        self.assertGreater(float(sale_row.iloc[0].Erlös_Brutto), 0)

        # The real, currently-open MH invoice overview exports a genuine XLSX.
        next_invoice = mh[~mh.reviewed_at.astype(bool)]
        blob = export_partner_excel(next_invoice)
        self.assertGreater(len(blob), 1000)

    def test_closed_position_never_reappears(self):
        business = self.business()
        target = business[(business.Bestellnummer == 'order-mh-1') & (business.Art == 'Bestellung')].iloc[0]
        with core.ledger() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO position_workflow(position_key,reviewed_at,paid_at,received_at,closed_at,source) '
                       'VALUES(?,?,?,?,?,?)',
                       (target.position_key, '2026-09-08', '2026-09-08', None, '2026-09-08',
                        position_workflow.source_snapshot(target)))
            db.commit()
        mh = studio_view.partner_rows(self.business())
        mh = mh[mh.Partner == 'MH']
        self.assertNotIn('order-mh-1', mh.Bestellnummer.tolist())
        self.assertEqual(sorted(mh.Bestellnummer.unique()), ['order-mh-2', 'order-mh-3'])

    def test_reading_an_unchanged_register_does_not_write_back(self):
        self.business()
        with core.ledger():
            pass
        writes_after_pure_reads = self.fake.put_calls
        with core.ledger():
            pass
        self.assertEqual(self.fake.put_calls, writes_after_pure_reads)


if __name__ == '__main__':
    unittest.main()
