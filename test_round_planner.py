import sqlite3
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

import core
import position_workflow as workflow
import round_planner as planner
from test_recovery import payout

BERLIN = ZoneInfo('Europe/Berlin')

# A fixed base_cut for every test: 2026-09-20 23:59 Europe/Berlin ends 2026-003.
BASE_CUT = datetime(2026, 9, 20, 23, 59, tzinfo=BERLIN)


def berlin(*args):
    return datetime(*args, tzinfo=BERLIN)


def row(position_key, order='o1', partner='NB', group='Gruppe B', payout='p1',
        amount='50.00', art='Bestellung', pruefhinweis='', closed_at='', paid_at='',
        paid_without_invoice_at='', quellenpruefung='', api_hold=False):
    return dict(position_key=position_key, Bestellnummer=order, Partner=partner, Gruppe=group,
                **{'Auszahlung Nr.': payout}, Erlös_Brutto=Decimal(amount), Art=art,
                Prüfhinweis=pruefhinweis, closed_at=closed_at, paid_at=paid_at,
                paid_without_invoice_at=paid_without_invoice_at,
                Quellenpruefung=quellenpruefung, API_Hold=api_hold)


def business_frame(rows):
    return pd.DataFrame(rows)


def payouts_frame(pairs):
    """pairs: {payout_id: 'dd.mm.yyyy'}. Always carries the columns
    core.read_master()/canonicalize() would guarantee even when empty."""
    rows = [{'Auszahlung Nr.': pid, 'Auszahlungsdatum': date, 'Bestellnummer': '', 'Typ': 'Bestellung'}
            for pid, date in pairs.items()]
    return pd.DataFrame(rows, columns=['Auszahlung Nr.', 'Auszahlungsdatum', 'Bestellnummer', 'Typ'])


def empty_db(assignments=()):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE group_b_round_positions (position_key TEXT PRIMARY KEY, round_id TEXT)')
    for key, rid in assignments:
        conn.execute('INSERT INTO group_b_round_positions VALUES (?,?)', (key, rid))
    conn.commit()
    return conn


class WeekWindowTests(unittest.TestCase):
    def test_sunday_23_58_round_still_running(self):
        result = planner.plan_round(now=berlin(2026, 9, 20, 23, 58), base_cut=BASE_CUT,
                                     business=business_frame([]), payouts=payouts_frame({}), db=empty_db())
        self.assertEqual(result['round_id'], '2026-003')
        self.assertFalse(result['cut_passed'])
        self.assertEqual(result['derived_status'], 'laufend')

    def test_sunday_23_59_next_round_required(self):
        result = planner.plan_round(now=berlin(2026, 9, 20, 23, 59), base_cut=BASE_CUT,
                                     business=business_frame([]), payouts=payouts_frame({}), db=empty_db())
        self.assertEqual(result['round_id'], '2026-004')
        self.assertTrue(result['cut_passed'])
        self.assertTrue(result['next_round_required'])

    def test_monday_after_cut_new_round_running(self):
        result = planner.plan_round(now=berlin(2026, 9, 21, 8, 0), base_cut=BASE_CUT,
                                     business=business_frame([]), payouts=payouts_frame({}), db=empty_db())
        self.assertEqual(result['round_id'], '2026-004')
        self.assertFalse(result['cut_passed'])
        self.assertEqual(result['derived_status'], 'laufend')
        self.assertEqual(result['start'], berlin(2026, 9, 20, 23, 59))
        self.assertEqual(result['end'], berlin(2026, 9, 27, 23, 59))


class EligibilityTests(unittest.TestCase):
    NOW = berlin(2026, 9, 18, 12, 0)  # Friday within round 2026-003, before the cut

    def test_historical_position_never_proposed(self):
        rows = [row('key-hist', partner='NB', payout='p-old', amount='30.00')]
        db = empty_db(assignments=[('key-hist', 'GB-2026-002')])
        result = planner.plan_round(now=self.NOW, base_cut=BASE_CUT, business=business_frame(rows),
                                     payouts=payouts_frame({'p-old': '01.09.2026'}), db=db)
        self.assertEqual(result['total_positions'], 0)
        self.assertEqual([e['position_key'] for e in result['excluded']['historisch_zugeordnet']], ['key-hist'])

    def test_mh_paid_without_invoice_never_proposed(self):
        rows = [row('key-mh', partner='MH', payout='p-mh', amount='100.00', paid_without_invoice_at='2026-09-20')]
        result = planner.plan_round(now=self.NOW, base_cut=BASE_CUT, business=business_frame(rows),
                                     payouts=payouts_frame({'p-mh': '18.09.2026'}), db=empty_db())
        self.assertEqual(result['total_positions'], 0)
        self.assertEqual(len(result['excluded']['bereits_bezahlt_abgeschlossen']), 1)
        self.assertTrue(result['excluded']['bereits_bezahlt_abgeschlossen'][0]['paid_without_invoice'])

    def test_order_without_payout_not_proposed(self):
        rows = [row('key-open', partner='NB', payout='', amount='40.00')]
        result = planner.plan_round(now=self.NOW, base_cut=BASE_CUT, business=business_frame(rows),
                                     payouts=payouts_frame({}), db=empty_db())
        self.assertEqual(result['total_positions'], 0)
        self.assertEqual(len(result['orders_without_payout']), 1)
        self.assertEqual(result['orders_without_payout'][0]['Bestellnummer'], 'o1')

    def test_confirmed_partner_with_zero_positions_shown_as_nothing_required(self):
        result = planner.plan_round(now=self.NOW, base_cut=BASE_CUT, business=business_frame([]),
                                     payouts=payouts_frame({}), db=empty_db())
        with patch.object(core, 'known_group_b_partners', return_value={'NB'}):
            result = planner.plan_round(now=self.NOW, base_cut=BASE_CUT, business=business_frame([]),
                                         payouts=payouts_frame({}), db=empty_db())
        nb = next(p for p in result['partners'] if p['partner'] == 'NB')
        self.assertEqual(nb['positions'], 0)
        self.assertEqual(nb['label'], '0 Positionen · nichts erforderlich')

    def test_unknown_prefix_flagged_but_not_billable(self):
        rows = [row('key-unk', partner='ZZ', payout='p-unk', amount='20.00',
                     pruefhinweis='Zuordnung fehlt: unbekannter Partner ZZ')]
        with patch.object(core, 'known_group_b_partners', return_value={'NB'}):
            result = planner.plan_round(now=self.NOW, base_cut=BASE_CUT, business=business_frame(rows),
                                         payouts=payouts_frame({'p-unk': '18.09.2026'}), db=empty_db())
        self.assertEqual(result['total_positions'], 0)
        self.assertIn('ZZ', result['unknown_prefixes'])
        self.assertEqual(result['unknown_prefixes']['ZZ'], 1)
        self.assertFalse(any(p['partner'] == 'ZZ' for p in result['partners']))  # never auto-confirmed

    def test_late_payout_still_belongs_to_not_yet_final_older_round(self):
        # Planning 2026-004; payout dated into 2026-003's own week (14.-20.09.),
        # with 2026-003 explicitly still open (not yet finalized).
        now_in_round_4 = berlin(2026, 9, 25, 12, 0)
        rows = [row('key-late', partner='NB', payout='p-late', amount='60.00')]
        result = planner.plan_round(now=now_in_round_4, base_cut=BASE_CUT, business=business_frame(rows),
                                     payouts=payouts_frame({'p-late': '15.09.2026'}),
                                     db=empty_db(), open_round_ids={'2026-003'})
        self.assertEqual(result['round_id'], '2026-004')
        self.assertEqual(result['total_positions'], 0)
        hits = result['excluded']['aeltere_offene_runde']
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['gehoert_zu'], '2026-003')

    def test_late_payout_falls_forward_when_older_round_already_final(self):
        now_in_round_4 = berlin(2026, 9, 25, 12, 0)
        rows = [row('key-late2', partner='NB', payout='p-late2', amount='60.00')]
        result = planner.plan_round(now=now_in_round_4, base_cut=BASE_CUT, business=business_frame(rows),
                                     payouts=payouts_frame({'p-late2': '15.09.2026'}),
                                     db=empty_db(), open_round_ids=set())  # 2026-003 not open -> final
        self.assertEqual(result['round_id'], '2026-004')
        self.assertEqual(result['total_positions'], 1)
        self.assertEqual(result['included'][0]['position_key'], 'key-late2')

    def test_no_dependency_on_old_group_b_lexware_model(self):
        """The whole module must never call into the historical
        RE0090/Patrick->Evelyn machinery (mentioning it in a comment explaining
        the deliberate independence is fine; importing or calling it is not)."""
        import inspect
        source = inspect.getsource(planner)
        # commit_round() deliberately DOES import group_b_rounds - to reuse its
        # hash-locked _insert_round(), not to touch its RE0090/Lexware-specific
        # bootstrap()/statement_type='group_b_evelyn' machinery.
        self.assertNotIn('import partner_export', source)
        self.assertNotIn('bootstrap(', source)
        self.assertNotIn('group_b_evelyn', source)
        self.assertNotIn('RE0090', source.replace('no RE0090', ''))  # ignore the one doc mention

    def test_eligible_position_is_included_with_correct_claim(self):
        rows = [row('key-ok', partner='NB', payout='p-ok', amount='75.50')]
        with patch.object(core, 'known_group_b_partners', return_value={'NB'}):
            result = planner.plan_round(now=self.NOW, base_cut=BASE_CUT, business=business_frame(rows),
                                         payouts=payouts_frame({'p-ok': '18.09.2026'}), db=empty_db())
        self.assertEqual(result['total_positions'], 1)
        self.assertEqual(result['total_claim'], Decimal('75.50'))
        nb = next(p for p in result['partners'] if p['partner'] == 'NB')
        self.assertEqual(nb['positions'], 1)
        self.assertEqual(nb['claim'], Decimal('75.50'))


class CommitRoundTests(unittest.TestCase):
    """commit_round() against a real local ledger (no Supabase) - the same
    core.ledger() machinery group_b_rounds itself uses."""

    NOW = berlin(2026, 9, 18, 12, 0)  # Friday within round 2026-003, before the cut

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        paths = patch.multiple(core, PAYOUTS_DB_PATH=str(self.root / 'Master_Payouts.csv'),
                                ORDERS_DB_PATH=str(self.root / 'Master_Orders.csv'))
        paths.start(); self.addCleanup(paths.stop)

    def seed(self, payout_id, order, sku, amount='50,00', payout_date='18.09.2026'):
        order_frame = payout(payout_id, order, order, sku=sku, amount=amount)
        core.import_reports([order_frame], core.ORDERS_DB_PATH, 'orders')
        sale = payout(payout_id, order, order, sku=sku, amount=amount)
        sale['Auszahlungsdatum'] = payout_date
        sale['Auszahlungsstatus'] = 'Betrag überwiesen'
        core.import_reports([sale], core.PAYOUTS_DB_PATH, 'payout')

    def seed_no_payout(self, order, sku, amount='50,00'):
        order_frame = payout('', order, order, sku=sku, amount=amount)
        core.import_reports([order_frame], core.ORDERS_DB_PATH, 'orders')
        open_row = payout('', order, order, sku=sku, amount=amount)
        core.import_reports([open_row], core.PAYOUTS_DB_PATH, 'payout')

    def fetch_round_positions(self, round_id=None):
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            if round_id:
                return list(db.execute('SELECT position_key, round_id FROM group_b_round_positions WHERE round_id=?', (round_id,)))
            return list(db.execute('SELECT position_key, round_id FROM group_b_round_positions'))

    def test_round_created_exactly_once(self):
        self.seed('p1', 'order-a', 'PP / TEST')
        round_id, created, plan = planner.commit_round(now=self.NOW, base_cut=BASE_CUT)
        self.assertEqual(round_id, '2026-003')
        self.assertTrue(created)
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            rows = list(db.execute("SELECT id FROM group_b_rounds WHERE id='2026-003'"))
        self.assertEqual(len(rows), 1)

    def test_repeat_call_is_a_true_noop(self):
        self.seed('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=self.NOW, base_cut=BASE_CUT)
        round_id, created, plan = planner.commit_round(now=self.NOW, base_cut=BASE_CUT)
        self.assertEqual(round_id, '2026-003')
        self.assertFalse(created)
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            rows = list(db.execute("SELECT id FROM group_b_rounds WHERE id='2026-003'"))
        self.assertEqual(len(rows), 1)

    def test_same_position_never_assigned_twice(self):
        self.seed('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=self.NOW, base_cut=BASE_CUT)
        rows = self.fetch_round_positions()
        keys = [r['position_key'] for r in rows]
        self.assertEqual(len(keys), len(set(keys)))
        # Directly attempting a second assignment for the same key must fail
        # (PRIMARY KEY on position_key), never silently move/duplicate it.
        with core.ledger() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('INSERT INTO group_b_round_positions VALUES(?,?,?,?)', (keys[0], 'some-other-round', 'evelyn_invoice', ''))
                db.commit()

    def test_historical_001_002_untouched(self):
        self.seed('p1', 'order-a', 'PP / TEST')
        business = workflow.positions()
        historical_key = business.iloc[0].position_key
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            db.execute("INSERT INTO group_b_rounds VALUES('GB-2026-001',2026,1,'test',NULL,NULL,'0','h','{}','2026-01-01T00:00:00Z')")
            db.execute("INSERT INTO group_b_round_positions VALUES(?,?,?,?)", (historical_key, 'GB-2026-001', 'evelyn_invoice', ''))
            db.commit()
        self.seed('p2', 'order-b', 'PP / TEST', payout_date='18.09.2026')
        planner.commit_round(now=self.NOW, base_cut=BASE_CUT)
        rows = self.fetch_round_positions('GB-2026-001')
        self.assertEqual([r['position_key'] for r in rows], [historical_key])
        rows_003 = self.fetch_round_positions('2026-003')
        self.assertNotIn(historical_key, [r['position_key'] for r in rows_003])

    def test_mh_protection_stays_effective(self):
        self.seed('p1', 'order-mh', 'MH / TEST')
        business = workflow.positions()
        key = business.iloc[0].position_key
        with core.ledger() as db:
            db.execute("UPDATE position_workflow SET paid_without_invoice_at='2026-09-01' WHERE position_key=?", (key,)) \
                if db.execute("SELECT 1 FROM position_workflow WHERE position_key=?", (key,)).fetchone() else \
                db.execute("INSERT INTO position_workflow(position_key,reviewed_at,paid_at,received_at,closed_at,source,paid_without_invoice_at) VALUES(?,?,?,?,?,?,?)",
                           (key, None, None, None, None, workflow.source_snapshot(business.iloc[0]), '2026-09-01'))
            db.commit()
        round_id, created, plan = planner.commit_round(now=self.NOW, base_cut=BASE_CUT)
        rows = self.fetch_round_positions('2026-003')
        self.assertNotIn(key, [r['position_key'] for r in rows])

    def test_locked_and_ambiguous_positions_stay_out(self):
        self.seed('p1', 'order-a', 'PP / TEST')
        self.seed('p2', 'order-b', 'PP / TEST')
        business = workflow.positions()
        # Simulate an unresolved hold by monkeypatching plan_round's business input directly instead
        # (API_Hold is computed from api_holds evidence, out of scope for this local fixture) - use
        # Prüfhinweis instead, which plan_round treats identically as an unresolved lock.
        business.loc[business.Bestellnummer == 'order-b', 'Prüfhinweis'] = 'Zuordnung fehlt: Mehrdeutige Bestellzuordnung'
        payouts = core.read_master(core.PAYOUTS_DB_PATH)
        plan = planner.plan_round(now=self.NOW, base_cut=BASE_CUT, business=business, payouts=payouts)
        self.assertEqual(plan['total_positions'], 1)
        self.assertEqual(len(plan['excluded']['ungeklaerte_sperre']), 1)

    def test_group_a_position_included_with_real_payout(self):
        self.seed('p1', 'order-a', 'PP / TEST')
        round_id, created, plan = planner.commit_round(now=self.NOW, base_cut=BASE_CUT)
        self.assertEqual(len(plan['included']), 1)
        self.assertEqual(plan['included'][0]['Gruppe'], 'Gruppe A')
        self.assertTrue(plan['included'][0]['Payout'])

    def test_group_a_position_without_payout_excluded(self):
        self.seed_no_payout('order-open', 'PP / TEST')
        round_id, created, plan = planner.commit_round(now=self.NOW, base_cut=BASE_CUT)
        self.assertEqual(plan['total_positions'], 0)
        self.assertEqual(len(plan['orders_without_payout']), 1)

    def test_group_b_position_without_payout_excluded(self):
        self.seed_no_payout('order-open-b', 'MH / TEST')
        round_id, created, plan = planner.commit_round(now=self.NOW, base_cut=BASE_CUT)
        self.assertEqual(plan['total_positions'], 0)
        self.assertEqual(len(plan['orders_without_payout']), 1)

    def test_no_re0090_or_lexware_dependency_in_created_round(self):
        self.seed('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=self.NOW, base_cut=BASE_CUT)
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            row = db.execute("SELECT evelyn_invoice_id, evelyn_document_number, source_kind FROM group_b_rounds WHERE id='2026-003'").fetchone()
        self.assertIsNone(row['evelyn_invoice_id'])
        self.assertIsNone(row['evelyn_document_number'])
        self.assertEqual(row['source_kind'], 'neutral_weekly')


if __name__ == '__main__':
    unittest.main()
