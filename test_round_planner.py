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


class RolloverTests(unittest.TestCase):
    """rollover() against a real local ledger - same setup as CommitRoundTests."""

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

    def all_round_ids(self):
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            return [r[0] for r in db.execute(
                "SELECT id FROM group_b_rounds WHERE source_kind='neutral_weekly' ORDER BY sequence")]

    def test_before_cut_creates_nothing_new(self):
        # 003 already exists (created Friday, before its own cut); rolling
        # over again on the same Friday must not create 004.
        self.seed('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        results = planner.rollover(now=berlin(2026, 9, 19, 9, 0))
        self.assertEqual([rid for rid, created in results if created], [])
        self.assertEqual(self.all_round_ids(), ['2026-003'])

    def test_right_after_cut_creates_exactly_one_new_round(self):
        self.seed('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        results = planner.rollover(now=berlin(2026, 9, 21, 0, 5))
        created = [rid for rid, was_created in results if was_created]
        self.assertEqual(created, ['2026-004'])
        self.assertEqual(self.all_round_ids(), ['2026-003', '2026-004'])

    def test_repeat_run_is_a_true_noop(self):
        self.seed('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        planner.rollover(now=berlin(2026, 9, 21, 0, 5))
        results = planner.rollover(now=berlin(2026, 9, 22, 10, 0))
        self.assertEqual([created for _, created in results], [False, False])
        self.assertEqual(self.all_round_ids(), ['2026-003', '2026-004'])

    def test_missed_scheduler_catches_up_multiple_rounds(self):
        self.seed('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        # Scheduler didn't run for three weeks straight; next run is well
        # into what should be 2026-006.
        results = planner.rollover(now=berlin(2026, 10, 9, 10, 0))
        created = [rid for rid, was_created in results if was_created]
        self.assertEqual(created, ['2026-004', '2026-005', '2026-006'])
        self.assertEqual(self.all_round_ids(), ['2026-003', '2026-004', '2026-005', '2026-006'])

    def test_new_round_may_have_zero_positions(self):
        self.seed('p1', 'order-a', 'PP / TEST')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        planner.rollover(now=berlin(2026, 9, 21, 0, 5))
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            positions = list(db.execute(
                "SELECT position_key FROM group_b_round_positions WHERE round_id='2026-004'"))
        self.assertEqual(positions, [])

    def test_late_payout_protected_from_open_older_round_when_rolling_forward(self):
        # order-zero is part of 003 at creation time; order-a's payout dates
        # into 003's own week too but only shows up afterwards (a late
        # arrival). rollover() creating 004 must pass 003 as still-open, so
        # plan_round() excludes order-a from 004 instead of misassigning it -
        # matching the manuscript rule that a late payout may only ever
        # (re-)land in its own not-yet-final round, never get swept forward.
        self.seed('p0', 'order-zero', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self.seed('p1', 'order-a', 'PP / TEST', payout_date='18.09.2026')
        planner.rollover(now=berlin(2026, 9, 21, 0, 5))
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            round_004 = [r[0] for r in db.execute(
                "SELECT position_key FROM group_b_round_positions WHERE round_id='2026-004'")]
            round_003 = [r[0] for r in db.execute(
                "SELECT position_key FROM group_b_round_positions WHERE round_id='2026-003'")]
        business = workflow.positions()
        late_key = business.loc[business.Bestellnummer == 'order-a'].iloc[0].position_key
        self.assertNotIn(late_key, round_004)
        self.assertNotIn(late_key, round_003)  # not auto-inserted either - out of scope here

    def test_001_002_and_mh_untouched(self):
        self.seed('p1', 'order-a', 'PP / TEST')
        self.seed('p2', 'order-mh', 'MH / TEST', payout_date='19.09.2026')
        business = workflow.positions()
        mh_key = business.loc[business.Bestellnummer == 'order-mh'].iloc[0].position_key
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            db.execute("INSERT INTO group_b_rounds VALUES('GB-2026-001',2026,1,'test',NULL,NULL,'0','h','{}','2026-01-01T00:00:00Z')")
            db.execute("INSERT INTO group_b_round_positions VALUES(?,?,?,?)", (mh_key, 'GB-2026-001', 'evelyn_invoice', ''))
            db.commit()
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        planner.rollover(now=berlin(2026, 10, 2, 10, 0))
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            gb1 = [r[0] for r in db.execute(
                "SELECT position_key FROM group_b_round_positions WHERE round_id='GB-2026-001'")]
        self.assertEqual(gb1, [mh_key])


class LatePayoutAssignmentTests(unittest.TestCase):
    """assign_late_payouts() against a real local ledger - same setup as CommitRoundTests."""

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
        sale['Transaktionsbetrag (inkl. Kosten)'] = amount  # needed by partner_export.prepare_partner_export
        core.import_reports([sale], core.PAYOUTS_DB_PATH, 'payout')

    def seed_no_payout(self, order, sku, amount='50,00'):
        order_frame = payout('', order, order, sku=sku, amount=amount)
        core.import_reports([order_frame], core.ORDERS_DB_PATH, 'orders')
        open_row = payout('', order, order, sku=sku, amount=amount)
        core.import_reports([open_row], core.PAYOUTS_DB_PATH, 'payout')

    def current(self):
        return workflow.positions(), core.read_master(core.PAYOUTS_DB_PATH)

    def key_for(self, business, order):
        return business.loc[business.Bestellnummer == order].iloc[0].position_key

    def round_of(self, position_key):
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            row = db.execute('SELECT round_id FROM group_b_round_positions WHERE position_key=?',
                              (position_key,)).fetchone()
            return row[0] if row else None

    def test_late_payout_assigned_to_open_older_round(self):
        self.seed('p0', 'order-zero', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        planner.rollover(now=berlin(2026, 9, 21, 0, 5))
        self.seed('p1', 'order-late', 'PP / TEST', payout_date='15.09.2026')
        business, payouts = self.current()
        assigned = planner.assign_late_payouts(business=business, payouts=payouts)
        key = self.key_for(business, 'order-late')
        self.assertEqual([a['round_id'] for a in assigned if a['position_key'] == key], ['2026-003'])
        self.assertEqual(self.round_of(key), '2026-003')

    def test_late_payout_for_finalized_partner_falls_to_next_open_round(self):
        import partner_snapshot
        self.seed('p0', 'order-zero', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        partner_snapshot.finalize('2026-003', 'PP', now=berlin(2026, 9, 21, 0, 5))
        planner.rollover(now=berlin(2026, 9, 21, 0, 5))
        self.seed('p1', 'order-late', 'PP / TEST', payout_date='15.09.2026')
        business, payouts = self.current()
        assigned = planner.assign_late_payouts(business=business, payouts=payouts)
        key = self.key_for(business, 'order-late')
        self.assertEqual([a['round_id'] for a in assigned if a['position_key'] == key], ['2026-004'])
        self.assertEqual(self.round_of(key), '2026-004')
        # the finalized round/partner itself gained nothing
        self.assertNotEqual(self.round_of(key), '2026-003')

    def test_already_assigned_position_is_a_noop(self):
        self.seed('p0', 'order-zero', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        planner.rollover(now=berlin(2026, 9, 21, 0, 5))
        self.seed('p1', 'order-late', 'PP / TEST', payout_date='15.09.2026')
        business, payouts = self.current()
        first = planner.assign_late_payouts(business=business, payouts=payouts)
        self.assertEqual(len(first), 1)
        business2, payouts2 = self.current()
        second = planner.assign_late_payouts(business=business2, payouts=payouts2)
        self.assertEqual(second, [])
        key = self.key_for(business, 'order-late')
        self.assertEqual(self.round_of(key), '2026-003')

    def test_position_without_real_payout_not_assigned(self):
        # core.load_master_data() already drops any row without an
        # 'Auszahlung Nr.' before business ever reaches assign_late_payouts -
        # so an unpaid order structurally can never be assigned, for Gruppe A
        # or B alike (mirrors round_planner's own plan_round() guarantee).
        self.seed('p0', 'order-zero', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        planner.rollover(now=berlin(2026, 9, 21, 0, 5))
        self.seed_no_payout('order-open', 'PP / TEST')
        business, payouts = self.current()
        self.assertNotIn('order-open', set(business.Bestellnummer))
        assigned = planner.assign_late_payouts(business=business, payouts=payouts)
        self.assertEqual(assigned, [])

    def test_locked_position_not_assigned(self):
        self.seed('p0', 'order-zero', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        planner.rollover(now=berlin(2026, 9, 21, 0, 5))
        self.seed('p1', 'order-late', 'PP / TEST', payout_date='15.09.2026')
        business, payouts = self.current()
        business.loc[business.Bestellnummer == 'order-late', 'Prüfhinweis'] = 'Zuordnung fehlt: Mehrdeutige Bestellzuordnung'
        assigned = planner.assign_late_payouts(business=business, payouts=payouts)
        key = self.key_for(business, 'order-late')
        self.assertNotIn(key, [a['position_key'] for a in assigned])
        self.assertIsNone(self.round_of(key))

    def test_mh_paid_without_invoice_not_assigned(self):
        self.seed('p0', 'order-zero', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        planner.rollover(now=berlin(2026, 9, 21, 0, 5))
        self.seed('p1', 'order-mh-late', 'MH / TEST', payout_date='15.09.2026')
        business, _ = self.current()
        mh_key = self.key_for(business, 'order-mh-late')
        with core.ledger() as db:
            db.execute('''INSERT INTO position_workflow
                (position_key,reviewed_at,paid_at,received_at,closed_at,source,paid_without_invoice_at)
                VALUES(?,?,?,?,?,?,?)''',
                (mh_key, None, None, None, None,
                 workflow.source_snapshot(business.loc[business.position_key == mh_key].iloc[0]), '2026-09-19'))
            db.commit()
        business, payouts = self.current()
        assigned = planner.assign_late_payouts(business=business, payouts=payouts)
        self.assertNotIn(mh_key, [a['position_key'] for a in assigned])
        self.assertIsNone(self.round_of(mh_key))

    def test_group_a_and_group_b_treated_identically(self):
        self.seed('p0', 'order-zero', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        planner.rollover(now=berlin(2026, 9, 21, 0, 5))
        self.seed('p1', 'order-late-a', 'PP / TEST', payout_date='15.09.2026')
        self.seed('p2', 'order-late-b', 'MH / TEST', payout_date='16.09.2026')
        business, payouts = self.current()
        assigned = planner.assign_late_payouts(business=business, payouts=payouts)
        a_key = self.key_for(business, 'order-late-a')
        b_key = self.key_for(business, 'order-late-b')
        self.assertEqual(self.round_of(a_key), '2026-003')
        self.assertEqual(self.round_of(b_key), '2026-003')
        self.assertEqual(len(assigned), 2)

    def test_001_002_untouched(self):
        self.seed('p0', 'order-zero', 'PP / TEST', payout_date='14.09.2026')
        business = workflow.positions()
        historical_key = business.iloc[0].position_key
        with core.ledger() as db:
            import group_b_rounds
            group_b_rounds.initialize(db)
            db.execute("INSERT INTO group_b_rounds VALUES('GB-2026-001',2026,1,'test',NULL,NULL,'0','h','{}','2026-01-01T00:00:00Z')")
            db.execute("INSERT INTO group_b_round_positions VALUES(?,?,?,?)", (historical_key, 'GB-2026-001', 'evelyn_invoice', ''))
            db.commit()
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        planner.rollover(now=berlin(2026, 9, 21, 0, 5))
        business, payouts = self.current()
        planner.assign_late_payouts(business=business, payouts=payouts)
        self.assertEqual(self.round_of(historical_key), 'GB-2026-001')

    def test_dry_run_writes_nothing(self):
        self.seed('p0', 'order-zero', 'PP / TEST', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        planner.rollover(now=berlin(2026, 9, 21, 0, 5))
        self.seed('p1', 'order-late', 'PP / TEST', payout_date='15.09.2026')
        business, payouts = self.current()
        preview = planner.assign_late_payouts(business=business, payouts=payouts, dry_run=True)
        key = self.key_for(business, 'order-late')
        self.assertEqual([a['round_id'] for a in preview if a['position_key'] == key], ['2026-003'])
        self.assertIsNone(self.round_of(key))  # nothing actually written
        real = planner.assign_late_payouts(business=business, payouts=payouts)
        self.assertEqual(real, preview)
        self.assertEqual(self.round_of(key), '2026-003')


if __name__ == '__main__':
    unittest.main()
