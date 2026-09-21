import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import api_holds
import core
import group_b_rounds as rounds
import position_workflow
import studio_view
import supabase_store
from test_api_holds import movement, snapshot
from test_recovery import payout


class GroupBRoundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        paths = patch.multiple(core, PAYOUTS_DB_PATH=str(root/'Master_Payouts.csv'),
                               ORDERS_DB_PATH=str(root/'Master_Orders.csv'))
        paths.start(); self.addCleanup(paths.stop)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('HTTP forbidden'))
        network.start(); self.addCleanup(network.stop)

        frames = [
            payout('p1','sale-r1-mh','r1-mh',sku='MH / A',amount='100,00'),
            payout('p1','sale-r1-nb','r1-nb',sku='NB / A',amount='200,00'),
            payout('p1','sale-r1-hold','r1-hold',sku='MH / H',amount='20,00'),
            payout('p2','sale-r2','r2-mh',sku='MH / B',amount='50,00'),
            payout('p2','sale-r2-nb','r2-nb',sku='NB / B',amount='40,00'),
            payout('p2','sale-zero','r2-zero',sku='MH / C',amount='30,00'),
            payout('p2','refund-zero','r2-zero',sku='MH / C',amount='-30,00',kind='Rückerstattung'),
            payout('p2','refund-late','r1-nb',sku='NB / A',amount='-10,00',kind='Rückerstattung'),
        ]
        for frame in frames:
            frame['Artikelnummer']='item-'+frame.iloc[0]['Bestellnummer']
            frame['Transaktionsbetrag (inkl. Kosten)']=frame['Betrag abzügl. Kosten']
            frame['Auszahlungsdatum']='08.09.2026'; frame['Auszahlungsstatus']='Betrag überwiesen'
        core.import_reports(frames,core.ORDERS_DB_PATH,'orders')
        core.import_reports(frames,core.PAYOUTS_DB_PATH,'payout')
        # A hold present from the very start (before any invoice is built) so
        # it is automatically excluded from the RE0090 snapshot and swept up as
        # its round's separate hold_reserve, exactly like a hold that surfaces
        # after the round was first created.
        api_holds.ingest(root, snapshot([movement(order='r1-hold', identifier='DISPUTE_HOLD-1')]))
        master=core.load_master_data(); core.sync_status(master)
        payload=core.build_invoice_payload(master,'p1','contact',True)
        self.invoice_id='invoice-re0090'
        with core.ledger() as db:
            db.execute("UPDATE payouts SET invoice_id=?,attempt='created',snapshot=? WHERE id='p1'",
                       (self.invoice_id,json.dumps(payload)))
            db.commit()
        self.business=position_workflow.positions()
        self.current=self.business[self.business.Bestellnummer.isin(['r2-mh','r2-nb'])&(self.business.Art=='Bestellung')]
        self.amount=studio_view._snapshot_total(payload)
        self.invoices={self.invoice_id:{'Belegnummer':'RE0090','Betrag':self.amount,
                                       'Payouts':['p1'],'discarded':False}}

    def _record_paid_invoice(self, db, invoice_id, partner, rows):
        """Directly persist an approved, paid partner invoice for one or more positions.

        Mirrors the shape partner_invoices.approve()/authorize_review() leave
        behind (an approved record plus partner_invoice_positions rows) and
        marks each row paid via position_workflow, without exercising the
        full upload/reconcile/approve UI flow that is already covered by
        test_partner_invoices.py.
        """
        items = [{'key': row.position_key, 'gross': str(row['Erlös_Brutto'])} for _, row in rows.iterrows()]
        record = dict(id=invoice_id, partner=partner, approved_at='2026-09-09T00:00:00Z',
                      expected={'items': items})
        db.execute('INSERT INTO partner_invoices VALUES(?,?,?,?,?)',
                   (invoice_id, 'hash-'+invoice_id, partner, None, json.dumps(record)))
        for item in items:
            db.execute('INSERT INTO partner_invoice_positions VALUES(?,?)', (item['key'], invoice_id))
        for _, row in rows.iterrows():
            db.execute('INSERT INTO position_workflow(position_key,reviewed_at,paid_at,received_at,closed_at,source) '
                       'VALUES(?,?,?,?,?,?)',
                       (row.position_key, '2026-09-08', '2026-09-09', None, None,
                        position_workflow.source_snapshot(row)))

    def test_bootstrap_is_immutable_and_maps_refunds_once(self):
        rounds.bootstrap(self.business,self.current,self.invoices)
        rounds.bootstrap(self.business,self.current,self.invoices)
        with core.ledger() as db:
            saved=[dict(row) for row in db.execute('SELECT * FROM group_b_rounds ORDER BY id')]
            positions=[dict(row) for row in db.execute('SELECT * FROM group_b_round_positions')]
            refunds=[dict(row) for row in db.execute('SELECT * FROM group_b_round_refunds')]
        self.assertEqual([row['id'] for row in saved],[rounds.ROUND_ONE,rounds.ROUND_TWO])
        self.assertEqual(sum(row['role']=='evelyn_invoice' for row in positions),4)
        self.assertEqual(sum(row['role']=='zero_pair' for row in positions),1)
        self.assertEqual(sum(row['role']=='hold_reserve' for row in positions),1)
        self.assertEqual(len(refunds),2)
        late=next(row for row in refunds if self.business.loc[self.business.position_key==row['refund_key'],'Bestellnummer'].iloc[0]=='r1-nb')
        self.assertEqual(late['origin_round_id'],rounds.ROUND_ONE)
        self.assertEqual(late['settlement_round_id'],rounds.ROUND_TWO)
        with core.ledger() as db:
            self.assertEqual(rounds.next_round_id(db,2026),'GB-2026-003')

        extra=self.business[(self.business.Bestellnummer=='r2-zero')&(self.business.Art=='Bestellung')]
        with core.ledger() as db:
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('INSERT INTO group_b_round_positions VALUES(?,?,?,?)',
                           (extra.iloc[0].position_key,rounds.ROUND_ONE,'zero_pair','duplicate'))
            db.rollback()

    def test_round_two_never_grows_and_partner_invoice_can_span_rounds(self):
        rounds.bootstrap(self.business,self.current,self.invoices)
        before=rounds.overview(self.business)
        expanded=self.business[(self.business.Art=='Bestellung') & self.business.Bestellnummer.isin(['r2-mh','r2-zero'])]
        rounds.bootstrap(self.business,expanded,self.invoices)
        after=rounds.overview(self.business)
        self.assertEqual([(r['round_id'],r['evelyn_invoiced']) for r in before['rounds']],
                         [(r['round_id'],r['evelyn_invoiced']) for r in after['rounds']])

        chosen=self.business[(self.business.Art=='Bestellung') & self.business.Bestellnummer.isin(['r1-mh','r2-mh'])]
        with core.ledger() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT INTO partner_invoices VALUES('multi','hash-multi','MH',NULL,'{}')")
            rounds.link_partner_invoice(db,'multi',chosen)
            related={row[0] for row in db.execute("SELECT round_id FROM partner_invoice_rounds WHERE invoice_id='multi'")}
            db.rollback()
        self.assertEqual(related,{rounds.ROUND_ONE,rounds.ROUND_TWO})

    def test_evelyn_link_precheck_allows_exactly_one_creation_then_blocks_repeat(self):
        rounds.bootstrap(self.business,self.current,self.invoices)
        with core.ledger() as db:
            self.assertTrue(rounds.evelyn_link_precheck(db,rounds.ROUND_TWO,self.business,self.invoices,self.current))
        with core.ledger() as db:
            self.assertTrue(rounds.record_evelyn_invoice(db,rounds.ROUND_TWO,'invoice-re0091','RE0091'))
        with core.ledger() as db:
            saved=db.execute('SELECT evelyn_invoice_id,evelyn_document_number FROM group_b_rounds WHERE id=?',
                             (rounds.ROUND_TWO,)).fetchone()
        self.assertEqual((saved['evelyn_invoice_id'],saved['evelyn_document_number']),('invoice-re0091','RE0091'))

        # Same result reported twice (e.g. a retried write) is a harmless no-op...
        with core.ledger() as db:
            self.assertFalse(rounds.record_evelyn_invoice(db,rounds.ROUND_TWO,'invoice-re0091','RE0091'))
        # ...but a second real submission is cleanly blocked before any network call.
        with core.ledger() as db:
            with self.assertRaises(ValueError):
                rounds.evelyn_link_precheck(db,rounds.ROUND_TWO,self.business,self.invoices,self.current)
        # ...and record_evelyn_invoice itself refuses to overwrite with a different invoice.
        with core.ledger() as db:
            with self.assertRaises(ValueError):
                rounds.record_evelyn_invoice(db,rounds.ROUND_TWO,'invoice-different','RE0099')

    def test_evelyn_link_precheck_blocks_on_mismatched_position_set(self):
        rounds.bootstrap(self.business,self.current,self.invoices)
        subset=self.current[self.current.Bestellnummer=='r2-mh']
        with core.ledger() as db:
            with self.assertRaises(ValueError):
                rounds.evelyn_link_precheck(db,rounds.ROUND_TWO,self.business,self.invoices,subset)
        # RE0090's own round is untouched by a check aimed at ROUND_TWO.
        with core.ledger() as db:
            unaffected=db.execute('SELECT evelyn_invoice_id FROM group_b_rounds WHERE id=?',
                                  (rounds.ROUND_ONE,)).fetchone()
        self.assertEqual(unaffected['evelyn_invoice_id'],self.invoice_id)

    def test_evelyn_link_precheck_blocks_when_another_invoice_already_covers_the_same_payout(self):
        rounds.bootstrap(self.business,self.current,self.invoices)
        conflicting=dict(self.invoices)
        conflicting['other-invoice']={'Belegnummer':'RE0099','Betrag':Decimal('1.00'),
                                       'Payouts':['p2'],'discarded':False}
        with core.ledger() as db:
            with self.assertRaises(ValueError):
                rounds.evelyn_link_precheck(db,rounds.ROUND_TWO,self.business,conflicting,self.current)
        # A discarded/test document with the same payout never blocks (mirrors RE0089).
        discarded=dict(self.invoices)
        discarded['discarded-invoice']={'Belegnummer':'RE0088 · verworfen','Betrag':Decimal('1.00'),
                                        'Payouts':['p2'],'discarded':True}
        with core.ledger() as db:
            self.assertTrue(rounds.evelyn_link_precheck(db,rounds.ROUND_TWO,self.business,discarded,self.current))

    def test_evelyn_link_precheck_refuses_neutral_weekly_rounds(self):
        with core.ledger() as db:
            rounds.initialize(db)
            db.execute("INSERT INTO group_b_rounds VALUES('2026-003',2026,3,'neutral_weekly',NULL,NULL,'0',"
                       "'hash-2026-003','{}','2026-01-01T00:00:00Z')")
            db.commit()
        with core.ledger() as db:
            with self.assertRaises(ValueError):
                rounds.evelyn_link_precheck(db,'2026-003',self.business,self.invoices,self.current)

    def test_variant_b_round_totals_keep_full_pair_at_zero(self):
        rounds.bootstrap(self.business,self.current,self.invoices)
        view=rounds.overview(self.business)
        r2_mh=next(row for row in view['partners'] if row['round_id']==rounds.ROUND_TWO and row['partner']=='MH')
        regular=self.business[(self.business.Bestellnummer=='r2-mh')&(self.business.Art=='Bestellung')]
        from partner_export import prepare_partner_export
        expected=prepare_partner_export(regular)['totals']['Rechnung']['gross']
        pair=self.business[self.business.Bestellnummer=='r2-zero']
        pair_model=prepare_partner_export(pair)
        self.assertEqual(r2_mh['current'],expected)
        self.assertEqual(pair_model['totals']['Rechnung']['gross']+
                         pair_model['totals']['Gutschriften']['gross'],Decimal('0.00'))
        self.assertEqual(r2_mh['corrections'],pair_model['totals']['Gutschriften']['gross'])
        self.assertEqual(r2_mh['positive'],expected+pair_model['totals']['Rechnung']['gross'])

    def test_hold_stays_a_reserve_never_a_partner_claim_or_patrick_margin(self):
        rounds.bootstrap(self.business,self.current,self.invoices)
        view=rounds.overview(self.business)
        round_one=next(r for r in view['rounds'] if r['round_id']==rounds.ROUND_ONE)
        mh_one=next(row for row in view['partners'] if row['round_id']==rounds.ROUND_ONE and row['partner']=='MH')
        from partner_export import prepare_partner_export
        r1_mh_only=self.business[(self.business.Bestellnummer=='r1-mh')&(self.business.Art=='Bestellung')]
        expected_mh=prepare_partner_export(r1_mh_only)['totals']['Rechnung']['gross']
        r1_nb_only=self.business[(self.business.Bestellnummer=='r1-nb')&(self.business.Art=='Bestellung')]
        expected_nb=prepare_partner_export(r1_nb_only)['totals']['Rechnung']['gross']
        r1_hold=self.business[(self.business.Bestellnummer=='r1-hold')&(self.business.Art=='Bestellung')]
        hold_amount=prepare_partner_export(r1_hold)['totals']['Rechnung']['gross']

        # The held sale never inflates the MH claim for this round...
        self.assertEqual(mh_one['positive'],expected_mh)
        self.assertEqual(mh_one['current'],expected_mh)
        # ...it is excluded from Evelyn's actual invoiced amount and therefore
        # never becomes Patrick's margin either...
        self.assertEqual(round_one['patrick_margin'],round_one['evelyn_invoiced']-expected_mh-expected_nb)
        # ...but it is not silently dropped: it is visible as its own reserve.
        self.assertEqual(round_one['holds'],1)
        self.assertGreater(hold_amount,Decimal('0.00'))
        self.assertEqual(round_one['unfunded_hold_reserve'],hold_amount)
        self.assertEqual(mh_one['unfunded_hold_reserve'],hold_amount)
        self.assertEqual(mh_one['funded_hold_reserve'],Decimal('0.00'))
        self.assertTrue(view['unassigned_holds'].empty)

    def test_historical_round_snapshot_cannot_be_changed(self):
        rounds.bootstrap(self.business,self.current,self.invoices)
        with core.ledger() as db:
            saved_before=dict(db.execute('SELECT * FROM group_b_rounds WHERE id=?',(rounds.ROUND_ONE,)).fetchone())
        with core.ledger() as db:
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaises(ValueError):
                rounds._insert_round(db,rounds.ROUND_ONE,1,'tampered',None,None,
                                     Decimal('999.99'),{'different':'snapshot'},self.current,{})
            db.rollback()
        with core.ledger() as db:
            saved_after=dict(db.execute('SELECT * FROM group_b_rounds WHERE id=?',(rounds.ROUND_ONE,)).fetchone())
        self.assertEqual(saved_before,saved_after)

    def test_hold_arriving_after_the_round_was_already_invoiced_becomes_a_funded_reserve(self):
        """A hold surfacing on a position Evelyn already paid for (Codex's
        'nachträglicher Hold aus Runde 1') must freeze that partner's share as
        a reserve, not as a claim the partner can still be paid nor as margin
        that quietly keeps the money - and it must not rewrite the already
        -persisted, immutable round.
        """
        rounds.bootstrap(self.business,self.current,self.invoices)
        before=rounds.overview(self.business)
        round_one_before=next(r for r in before['rounds'] if r['round_id']==rounds.ROUND_ONE)
        with core.ledger() as db:
            saved_before=dict(db.execute('SELECT * FROM group_b_rounds WHERE id=?',(rounds.ROUND_ONE,)).fetchone())

        directory=Path(core.PAYOUTS_DB_PATH).parent
        api_holds.ingest(directory, snapshot([movement(order='r1-mh', identifier='DISPUTE_HOLD-2')],
                                             at='2026-09-10T10:00:00Z'))
        business=position_workflow.positions()
        rounds.bootstrap(business,self.current,self.invoices)
        after=rounds.overview(business)
        round_one_after=next(r for r in after['rounds'] if r['round_id']==rounds.ROUND_ONE)
        mh_one=next(row for row in after['partners'] if row['round_id']==rounds.ROUND_ONE and row['partner']=='MH')

        from partner_export import prepare_partner_export
        r1_mh_only=business[(business.Bestellnummer=='r1-mh')&(business.Art=='Bestellung')]
        expected_mh=prepare_partner_export(r1_mh_only)['totals']['Rechnung']['gross']

        with core.ledger() as db:
            saved_after=dict(db.execute('SELECT * FROM group_b_rounds WHERE id=?',(rounds.ROUND_ONE,)).fetchone())
        # The persisted RE0090 round itself never changes because of a later hold.
        self.assertEqual(saved_before,saved_after)
        self.assertEqual(round_one_after['evelyn_invoiced'],round_one_before['evelyn_invoiced'])
        self.assertEqual(round_one_after['patrick_margin'],round_one_before['patrick_margin'])
        # MH no longer has an open claim for the now-held position...
        self.assertEqual(mh_one['positive'],Decimal('0.00'))
        # ...but the money Evelyn already paid for it is kept visible as a
        # funded reserve, not silently merged into margin or into a payable claim.
        self.assertEqual(round_one_after['holds'],2)
        self.assertEqual(mh_one['funded_hold_reserve'],expected_mh)
        self.assertGreater(round_one_after['funded_hold_reserve'],round_one_before['funded_hold_reserve'])

    def test_nb_round_one_is_paid_while_round_two_stays_open(self):
        rounds.bootstrap(self.business,self.current,self.invoices)
        r1_nb=self.business[(self.business.Bestellnummer=='r1-nb')&(self.business.Art=='Bestellung')]
        with core.ledger() as db:
            db.execute('BEGIN IMMEDIATE')
            self._record_paid_invoice(db,'nb-round-one-invoice','NB',r1_nb)
            db.commit()
        business=position_workflow.positions()
        view=rounds.overview(business)
        nb_one=next(row for row in view['partners'] if row['round_id']==rounds.ROUND_ONE and row['partner']=='NB')
        nb_two=next(row for row in view['partners'] if row['round_id']==rounds.ROUND_TWO and row['partner']=='NB')
        self.assertGreater(nb_one['paid'],Decimal('0.00'))
        self.assertEqual(nb_one['paid'],nb_one['positive'])
        self.assertLessEqual(nb_one['open'],Decimal('0.00'))
        self.assertEqual(nb_two['paid'],Decimal('0.00'))
        self.assertGreater(nb_two['open'],Decimal('0.00'))

    def test_mh_combined_round_total_matches_the_actual_download_button(self):
        """The number Patrick actually downloads must equal the sum of the
        per-round figures shown in the Gruppe-B-Abrechnungsrunden panel.

        This cross-checks two independently implemented views of the same
        open MH claim: group_b_rounds.overview() (round-scoped) and
        studio_view.partner_rows()/partner_summary() (the exact pipeline
        behind app.py's 'Einzelabrechnung herunterladen' button). MH here
        has one open sale in GB-2026-001 and a second sale plus a full
        refund pair in GB-2026-002 - none of it reviewed or paid yet, i.e.
        the ordinary 'nothing invoiced so far' state.
        """
        rounds.bootstrap(self.business,self.current,self.invoices)
        view=rounds.overview(self.business)
        mh_rows=[row for row in view['partners'] if row['partner']=='MH']
        self.assertEqual({row['round_id'] for row in mh_rows},{rounds.ROUND_ONE,rounds.ROUND_TWO})
        round_total=sum((row['open'] for row in mh_rows),Decimal('0.00'))

        partner_ready=studio_view.partner_rows(self.business)
        mh_ready=partner_ready[partner_ready.Partner=='MH']
        next_invoice=mh_ready[~mh_ready.reviewed_at.astype(bool)]
        summary=studio_view.partner_summary(next_invoice).iloc[0]
        download_total=Decimal(str(summary['Verbleibender Anspruch'])).quantize(Decimal('0.01'))

        self.assertEqual(round_total,download_total)
        self.assertGreater(round_total,Decimal('0.00'))

    def test_bootstrap_initializes_missing_round_tables_on_its_own_connection(self):
        """bootstrap() must not rely on its caller (core.ledger()) having
        already created the schema - it has to safeguard itself on whatever
        connection it actually receives, exactly like link_partner_invoice()
        already does. This bypasses core.ledger() entirely and hands
        bootstrap() a bare connection that only has the tables that predate
        group_b_rounds (payouts, partner_invoice_positions), reproducing an
        old database without the round tables regardless of which ledger()
        branch (local file or Supabase) would normally supply it.
        """
        bare = sqlite3.connect(':memory:')
        bare.row_factory = sqlite3.Row
        bare.execute('CREATE TABLE payouts (id TEXT PRIMARY KEY, status TEXT NOT NULL, '
                     'fingerprint TEXT, invoice_id TEXT, attempt TEXT, snapshot TEXT)')
        bare.execute('CREATE TABLE partner_invoice_positions (position_key TEXT PRIMARY KEY, invoice_id TEXT)')
        with core.ledger() as real_db:
            for row in real_db.execute('SELECT * FROM payouts'):
                bare.execute('INSERT INTO payouts VALUES(?,?,?,?,?,?)', tuple(row))
        bare.commit()
        existing = {row[0] for row in bare.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('group_b_rounds', existing)

        @contextmanager
        def bare_ledger():
            try:
                yield bare
            except Exception:
                bare.rollback(); raise

        with patch.object(core, 'ledger', bare_ledger):
            rounds.bootstrap(self.business, self.current, self.invoices)  # must not raise
            rounds.bootstrap(self.business, self.current, self.invoices)  # second call: no duplicates

        tables = {row[0] for row in bare.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in ('group_b_rounds', 'group_b_round_positions', 'group_b_round_refunds', 'partner_invoice_rounds'):
            self.assertIn(name, tables)
        saved = [row['id'] for row in bare.execute('SELECT * FROM group_b_rounds ORDER BY id')]
        self.assertEqual(saved, [rounds.ROUND_ONE, rounds.ROUND_TWO])
        positions = [dict(row) for row in bare.execute('SELECT * FROM group_b_round_positions')]
        self.assertEqual(len(positions), len({row['position_key'] for row in positions}))
        refund_rows = [dict(row) for row in bare.execute('SELECT * FROM group_b_round_refunds')]
        self.assertEqual(len(refund_rows), len({row['refund_key'] for row in refund_rows}))
        bare.close()


class SupabaseLedgerSchemaTests(unittest.TestCase):
    """core.ledger()'s Supabase branch loads whatever schema is already inside
    the stored settlement.sqlite3 blob. A blob migrated before group_b_rounds
    existed has no such tables, so bootstrap()/overview() must not be handed
    a connection missing them - exactly the reported 'no such table:
    group_b_rounds' failure in the running Streamlit-Cloud/Supabase app.
    """

    def test_ledger_creates_missing_group_b_tables_on_a_legacy_supabase_blob(self):
        legacy = sqlite3.connect(':memory:')
        legacy.execute('CREATE TABLE payouts (id TEXT PRIMARY KEY, status TEXT NOT NULL, '
                       'fingerprint TEXT, invoice_id TEXT, attempt TEXT, snapshot TEXT)')
        legacy.commit()
        store = {'settlement': (supabase_store.sqlite_to_bytes(legacy), 1)}
        legacy.close()

        def fake_get(key, required=True):
            self.assertEqual(key, 'state/settlement.sqlite3')
            return store['settlement']

        def fake_put(key, content, expected_version=None):
            self.assertEqual(expected_version, store['settlement'][1])
            store['settlement'] = (content, expected_version + 1)
            return expected_version + 1

        with patch.object(supabase_store, 'enabled', return_value=True), \
             patch.object(supabase_store, 'get', side_effect=fake_get), \
             patch.object(supabase_store, 'put', side_effect=fake_put):
            # Before the fix this raises sqlite3.OperationalError: no such
            # table: group_b_rounds, exactly like the reported production error.
            with core.ledger() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT INTO group_b_rounds VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (rounds.ROUND_ONE, 2026, 1, 'immutable_evelyn_invoice', None, None,
                            '1.00', 'hash', '{}', '2026-01-01T00:00:00Z'))
                db.commit()
        version_after_write = store['settlement'][1]
        self.assertEqual(version_after_write, 2)

        # The schema fix is written back to Supabase itself (source of truth),
        # not kept only in a throwaway local copy: a fresh ledger() call sees
        # the persisted round without any special-casing.
        with patch.object(supabase_store, 'enabled', return_value=True), \
             patch.object(supabase_store, 'get', side_effect=fake_get), \
             patch.object(supabase_store, 'put', side_effect=fake_put):
            with core.ledger() as db:
                saved = [row[0] for row in db.execute('SELECT id FROM group_b_rounds')]
        self.assertEqual(saved, [rounds.ROUND_ONE])

    def test_ledger_migrates_and_persists_missing_import_coverage_columns(self):
        legacy = sqlite3.connect(':memory:')
        legacy.execute('CREATE TABLE imports (id INTEGER PRIMARY KEY, kind TEXT, filename TEXT, at TEXT, '
                       'start TEXT, end TEXT, detected INTEGER, added INTEGER, present INTEGER, '
                       'issues INTEGER, error TEXT)')
        legacy.execute("INSERT INTO imports(kind,filename,error) VALUES('orders','legacy.csv','')")
        legacy.commit()
        store = {'settlement': (supabase_store.sqlite_to_bytes(legacy), 1)}
        legacy.close()

        def fake_get(key, required=True):
            return store['settlement']

        def fake_put(key, content, expected_version=None):
            self.assertEqual(expected_version, store['settlement'][1])
            store['settlement'] = (content, expected_version + 1)
            return expected_version + 1

        patches = (patch.object(supabase_store, 'enabled', return_value=True),
                   patch.object(supabase_store, 'get', side_effect=fake_get),
                   patch.object(supabase_store, 'put', side_effect=fake_put))
        with patches[0], patches[1], patches[2]:
            with core.ledger() as db:
                columns = {row[1] for row in db.execute('PRAGMA table_info(imports)')}
                self.assertTrue({'status', 'coverage_start', 'coverage_end',
                                 'observed_start', 'observed_end'}.issubset(columns))
                self.assertEqual(db.execute('SELECT observed_start FROM imports').fetchone()[0], None)

        self.assertEqual(store['settlement'][1], 2)
        with patch.object(supabase_store, 'enabled', return_value=True), \
             patch.object(supabase_store, 'get', side_effect=fake_get), \
             patch.object(supabase_store, 'put', side_effect=fake_put):
            with core.ledger() as db:
                columns = {row[1] for row in db.execute('PRAGMA table_info(imports)')}
        self.assertIn('observed_start', columns)


if __name__ == '__main__':
    unittest.main()
