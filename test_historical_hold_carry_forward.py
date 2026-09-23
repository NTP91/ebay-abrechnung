"""Fall 3: ein historischer Einbehalt (GB-2026-001/002, role='hold_reserve')
wird von eBay ECHT freigegeben und danach genau EINMAL in der ersten noch
offenen neutralen 2026-003+-Runde wirtschaftlich abgerechnet.

Die Ursprungsposition wird dabei nie verschoben, kopiert oder wiedereroeffnet:
ihre Zeile in group_b_round_positions behaelt round_id='GB-2026-001' und ihre
role fuer immer. Der Vortrag steht ausschliesslich in
historical_hold_carry_forward (position_key PRIMARY KEY).

Fixtures/Seed-Muster wiederverwendet aus test_zero_pair_and_hold_release.py
(b79bf31); Seeded selbst enthaelt keine eigenen Testfaelle und wird durch den
Import daher nicht doppelt ausgefuehrt.
"""
import json
import unittest
from decimal import Decimal

import broker_commission
import core
import group_b_rounds
import partner_conditions
import partner_snapshot
import position_workflow as workflow
import round_planner as planner
import round_ui
from test_zero_pair_and_hold_release import (AFTER_003, BASE_CUT, Seeded, api_holds,
                                              berlin, evidence, hold_movement,
                                              release_movement)

NOW_003 = berlin(2026, 9, 18, 12, 0)
NOW_004 = berlin(2026, 9, 22, 12, 0)


class CarryForward(Seeded):
    def seed_hold(self, round_id='GB-2026-001', amount='39,90'):
        self.seed('p-hold', 'order-hold', 'FS / TEST', amount=amount)
        key = self.keys_for('order-hold')['Bestellung']
        self.assign(key, round_id, role='hold_reserve', sequence=1)
        api_holds.ingest(str(self.root), evidence(
            [hold_movement('order-hold', 'SALE-hold', amount.replace(',', '.'))],
            '2026-09-04T10:00:00Z'))
        return key

    def release(self, amount='39,90', at='2026-09-11T10:00:00Z'):
        api_holds.ingest(str(self.root), evidence(
            [release_movement('order-hold', 'SALE-hold', amount.replace(',', '.'))], at))

    def seed_partner_position(self, order='order-new', amount='100,00'):
        """Eine normale 003-Position desselben Partners - noetig, damit ein
        partner_snapshot.finalize() die Runde ueberhaupt sperren kann."""
        self.seed('p-new', order, 'FS / NEU', amount=amount)
        return self.keys_for(order)['Bestellung']

    def make_003(self):
        planner.commit_round(now=NOW_003, base_cut=BASE_CUT)

    def make_004(self):
        planner.commit_round(now=NOW_004, base_cut=BASE_CUT)

    def carried_rows(self):
        with core.ledger() as db:
            group_b_rounds.initialize(db)
            return [dict(r) for r in db.execute(
                'SELECT * FROM historical_hold_carry_forward ORDER BY position_key')]

    def round_keys(self, round_id):
        with core.ledger() as db:
            return group_b_rounds.round_position_keys(db, round_id)


class TriggerTests(CarryForward):
    """Nur ein echtes, belegtes Freigabeereignis loest einen Vortrag aus."""

    def test_hold_without_release_stays_only_a_hold(self):
        """Regression gegen b79bf31: ohne Freigabe passiert weiterhin nichts."""
        key = self.seed_hold()
        self.make_003()
        self.assertEqual(planner.carry_forward_released_holds(), [])
        self.assertEqual(self.carried_rows(), [])
        self.assertNotIn(key, self.round_keys('2026-003'))
        self.assertNotIn(key, broker_commission.basis('2026-003')['position_keys'])

    def test_genuine_release_creates_exactly_one_carry_forward(self):
        key = self.seed_hold()
        self.make_003()
        self.release()
        created = planner.carry_forward_released_holds()
        self.assertEqual([item['position_key'] for item in created], [key])
        rows = self.carried_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['position_key'], rows[0]['origin_round_id'],
                          rows[0]['settlement_round_id']), (key, 'GB-2026-001', '2026-003'))
        self.assertIn(key, self.round_keys('2026-003'))

    def test_ambiguous_evidence_stays_a_klaerfall(self):
        """Eine Gutschrift mit abweichendem Betrag ist keine dokumentierte
        Freigabe: api_holds sieht den Einbehalt weiter als aktiv."""
        key = self.seed_hold()
        self.make_003()
        self.release(amount='10,00')
        business = workflow.positions().set_index('position_key')
        self.assertTrue(business.loc[key, 'API_Hold'])
        self.assertEqual(planner.carry_forward_released_holds(), [])
        self.assertEqual(self.carried_rows(), [])

    def test_a_missing_hold_record_is_not_a_release(self):
        """Absence of evidence is not evidence: ein hold_reserve ohne jede
        Hold-/Freigabebeobachtung wird nie vorgetragen."""
        self.seed('p-bare', 'order-bare', 'FS / TEST', amount='39,90')
        key = self.keys_for('order-bare')['Bestellung']
        self.assign(key, 'GB-2026-001', role='hold_reserve', sequence=1)
        self.make_003()
        self.assertEqual(planner.carry_forward_released_holds(), [])
        self.assertNotIn(key, self.round_keys('2026-003'))

    def test_released_uses_the_same_rule_as_active(self):
        """api_holds.released() ist die Gegenseite von active(), nicht eine
        zweite Heuristik."""
        self.seed_hold()
        document = api_holds.load(str(self.root))
        self.assertIn('order-hold', api_holds.active(document))
        self.assertEqual(api_holds.released(document), {})
        self.release()
        document = api_holds.load(str(self.root))
        self.assertNotIn('order-hold', api_holds.active(document))
        self.assertIn('order-hold', api_holds.released(document))


class TargetRoundTests(CarryForward):
    """Zielrundenwahl - dieselbe Logik wie assign_late_payouts()."""

    def test_target_is_the_first_open_neutral_round(self):
        self.seed_hold()
        self.seed_partner_position()
        self.make_003()
        self.make_004()
        self.release()
        created = planner.carry_forward_released_holds()
        self.assertEqual(created[0]['settlement_round_id'], '2026-003')

    def test_a_finalized_round_is_skipped_for_the_next_open_one(self):
        """Ist die natuerliche Zielrunde fuer diesen Partner bereits
        finalisiert, landet der Vortrag in der NAECHSTEN offenen Runde; die
        finalisierte wird nie wiedereroeffnet."""
        key = self.seed_hold()
        self.seed_partner_position()
        self.make_003()
        self.make_004()
        record, created = partner_snapshot.finalize('2026-003', 'FS', now=AFTER_003)
        self.assertTrue(created)
        frozen = sorted(json.loads(record['position_keys']))
        self.release()
        result = planner.carry_forward_released_holds()
        self.assertEqual(result[0]['settlement_round_id'], '2026-004')
        self.assertNotIn(key, self.round_keys('2026-003'))
        self.assertIn(key, self.round_keys('2026-004'))
        with core.ledger() as db:
            partner_snapshot.initialize(db)
            after = db.execute('SELECT position_keys FROM partner_round_snapshots '
                               'WHERE round_id=? AND partner=?', ('2026-003', 'FS')).fetchone()[0]
        self.assertEqual(sorted(json.loads(after)), frozen)
        self.assertNotIn(key, frozen)

    def test_without_any_neutral_round_nothing_happens_yet(self):
        self.seed_hold()
        self.release()
        self.assertEqual(planner.carry_forward_released_holds(), [])
        self.assertEqual(self.carried_rows(), [])


class DuplicateGuardTests(CarryForward):
    """Dublettenschutz - hoechste Prioritaet."""

    def test_repeated_release_import_never_creates_a_second_carry_forward(self):
        key = self.seed_hold()
        self.make_003()
        self.release()
        first = planner.carry_forward_released_holds()
        self.release(at='2026-09-12T10:00:00Z')
        self.release(at='2026-09-13T10:00:00Z')
        second = planner.carry_forward_released_holds()
        self.make_004()
        third = planner.carry_forward_released_holds()
        planner.assign_late_payouts()
        self.assertEqual(len(first), 1)
        self.assertEqual((second, third), ([], []))
        self.assertEqual(len(self.carried_rows()), 1)
        self.assertIn(key, self.round_keys('2026-003'))
        self.assertNotIn(key, self.round_keys('2026-004'))
        with core.ledger() as db:
            rows = list(db.execute('SELECT round_id, role FROM group_b_round_positions '
                                   'WHERE position_key=?', (key,)))
        self.assertEqual([(r['round_id'], r['role']) for r in rows],
                         [('GB-2026-001', 'hold_reserve')])

    def test_a_second_carry_forward_row_is_structurally_impossible(self):
        key = self.seed_hold()
        self.make_003()
        self.make_004()
        self.release()
        planner.carry_forward_released_holds()
        with core.ledger() as db:
            group_b_rounds.initialize(db)
            with self.assertRaises(Exception):
                db.execute('INSERT INTO historical_hold_carry_forward VALUES(?,?,?,?,?,?)',
                           (key, 'GB-2026-001', '2026-004', 'x', 'x', 'zweiter Versuch'))
                db.commit()
            db.rollback()
        self.assertEqual(len(self.carried_rows()), 1)

    def test_a_historical_position_without_carry_forward_is_still_excluded(self):
        """SICHERHEIT: die historisch_zugeordnet-Sperre wird nicht aufgeweicht.
        Eine normale Altposition ohne Vortrag bleibt aus jeder 003+-Runde
        draussen - auch wenn daneben ein Vortrag existiert."""
        key = self.seed_hold()
        self.seed('p-old', 'order-old', 'FS / ALT', amount='55,00')
        other = self.keys_for('order-old')['Bestellung']
        self.assign(other, 'GB-2026-001', role='evelyn_invoice', sequence=1)
        self.make_003()
        self.release()
        planner.carry_forward_released_holds()
        keys = self.round_keys('2026-003')
        self.assertIn(key, keys)
        self.assertNotIn(other, keys)
        plan = planner.plan_round(now=NOW_003, base_cut=BASE_CUT)
        excluded = {item['position_key'] for item in plan['excluded']['historisch_zugeordnet']}
        self.assertIn(key, excluded)
        self.assertIn(other, excluded)
        self.assertNotIn(key, {item['position_key'] for item in plan['included']})

    def test_a_carried_position_is_blocked_from_the_old_lexware_flow(self):
        """Der vorgetragene Einbehalt wird direkt Partner -> Evelyn
        abgerechnet und darf deshalb nie zusaetzlich im alten
        Lexware-Sammelbeleg landen."""
        import studio_view
        key = self.seed_hold()
        self.make_003()
        self.assertNotIn(key, studio_view.neutral_round_keys())
        self.release()
        planner.carry_forward_released_holds()
        self.assertIn(key, studio_view.neutral_round_keys())

    def test_a_released_hold_is_never_written_into_group_b_round_positions_again(self):
        key = self.seed_hold()
        self.make_003()
        self.release()
        planner.carry_forward_released_holds()
        planner.assign_late_payouts()
        planner.rollover(now=NOW_004)
        with core.ledger() as db:
            count = db.execute('SELECT COUNT(*) FROM group_b_round_positions '
                               'WHERE position_key=?', (key,)).fetchone()[0]
        self.assertEqual(count, 1)


class PartnerStatementTests(CarryForward):
    """Die vorgetragene Position ist ein normaler Buerger der neuen Runde."""

    def test_partner_snapshot_contains_the_carried_position_exactly_once(self):
        key = self.seed_hold()
        self.seed_partner_position()
        self.make_003()
        self.release()
        planner.carry_forward_released_holds()
        self.assertTrue(partner_snapshot.interim_export('2026-003', 'FS'))
        record, created = partner_snapshot.finalize('2026-003', 'FS', now=AFTER_003)
        self.assertTrue(created)
        frozen = json.loads(record['position_keys'])
        self.assertEqual(frozen.count(key), 1)
        # Genau zwei Rechnungszeilen: der vorgetragene Einbehalt (39,90 brutto)
        # und die normale 003-Position (100,00 brutto) - jede genau einmal.
        items = json.loads(record['line_items'])
        self.assertEqual(sorted(item['order'] for item in items),
                         ['order-hold', 'order-new'])
        # Betrag aus dem unveraenderten partner_export mit der generisch
        # aufgeloesten FS-Kondition (Gruppe B, 3,5 %) auf 139,90 EUR brutto.
        self.assertEqual(Decimal(record['final_amount']), Decimal('135.01'))

    def test_round_status_sees_the_carried_position(self):
        import round_status
        key = self.seed_hold()
        self.make_003()
        self.release()
        planner.carry_forward_released_holds()
        status = round_status.partner_status('2026-003', 'FS', now=NOW_003)
        self.assertEqual(status['positions'], 1)
        self.assertIn(key, self.round_keys('2026-003'))


class BrokerCommissionTests(CarryForward):
    """Vermittlungsprovision Patrick -> Evelyn: genau einmal, nur neue Runde."""

    def test_commission_fires_exactly_once_only_in_the_new_round(self):
        key = self.seed_hold()
        self.seed_partner_position()
        self.make_003()
        self.make_004()
        self.release()
        planner.carry_forward_released_holds()
        self.assertIn(key, broker_commission.basis('2026-003')['position_keys'])
        self.assertNotIn(key, broker_commission.basis('2026-004')['position_keys'])
        record, created = broker_commission.finalize('2026-003', now=AFTER_003)
        self.assertTrue(created)
        self.assertIn(key, json.loads(record['position_keys']))
        # Der Satz kommt generisch aus partner_conditions, nicht hartcodiert.
        breakdown = {item['partner']: item for item in json.loads(record['breakdown'])}
        self.assertEqual(Decimal(breakdown['FS']['rate']),
                         partner_conditions.broker_rate('FS', 'Gruppe B'))
        with core.ledger() as db:
            broker_commission.initialize(db)
            rows = list(db.execute('SELECT round_id FROM broker_commission_positions '
                                   'WHERE position_key=?', (key,)))
            self.assertEqual([r['round_id'] for r in rows], ['2026-003'])
            # PRIMARY KEY ueber ALLE Runden: ein zweiter Provisionseintrag
            # derselben Position ist strukturell unmoeglich.
            with self.assertRaises(Exception):
                db.execute('INSERT INTO broker_commission_positions VALUES(?,?,?)',
                           (key, '2026-004', 'zweiter Versuch'))
                db.commit()
            db.rollback()

    def test_the_historical_origin_round_has_no_commission_at_all(self):
        self.seed_hold()
        self.make_003()
        self.release()
        planner.carry_forward_released_holds()
        with self.assertRaises(ValueError):
            broker_commission.basis('GB-2026-001')
        with self.assertRaises(ValueError):
            broker_commission.finalize('GB-2026-001', now=AFTER_003)

    def test_late_refund_on_a_carried_position_uses_the_existing_correction(self):
        key = self.seed_hold()
        self.seed_partner_position()
        self.make_003()
        self.release()
        planner.carry_forward_released_holds()
        broker_commission.finalize('2026-003', now=AFTER_003)
        # Erstattung erst NACH Vortrag und Abrechnung.
        self.seed('p-hold', 'order-hold', 'FS / TEST', amount='-39,90', kind='Erstattung')
        flags = broker_commission.late_refund_flags('2026-003')
        self.assertEqual([row['origin_position_key'] for row in flags], [key])
        self.assertEqual(flags[0]['partner'], 'FS')
        self.assertLess(flags[0]['correction'], 0)


class RefundInsteadOfReleaseTests(CarryForward):
    def test_full_refund_creates_no_carry_forward_and_no_new_position(self):
        key = self.seed_hold()
        self.seed('p-hold', 'order-hold', 'FS / TEST', amount='-39,90', kind='Erstattung')
        self.make_003()
        self.release()
        self.assertEqual(planner.carry_forward_released_holds(), [])
        self.assertEqual(self.carried_rows(), [])
        business = workflow.positions()
        sale = business[(business.Bestellnummer == 'order-hold')
                        & (business.Art == 'Bestellung')].iloc[0]
        self.assertEqual(Decimal(str(sale['Offen_Brutto'])), Decimal('0.00'))
        self.assertNotIn(key, self.round_keys('2026-003'))
        self.assertNotIn(key, broker_commission.basis('2026-003')['position_keys'])
        with core.ledger() as db:
            round_id = db.execute('SELECT round_id FROM group_b_round_positions '
                                  'WHERE position_key=?', (key,)).fetchone()[0]
        self.assertEqual(round_id, 'GB-2026-001')


class OriginRoundFingerprintTests(CarryForward):
    def test_origin_round_stays_byte_identical_through_the_whole_sequence(self):
        key = self.seed_hold()
        self.seed_partner_position()
        before = self.historical_fingerprint()
        self.make_003()
        self.make_004()
        self.release()
        planner.carry_forward_released_holds()
        planner.carry_forward_released_holds()
        planner.assign_late_payouts()
        partner_snapshot.finalize('2026-003', 'FS', now=AFTER_003)
        broker_commission.finalize('2026-003', now=AFTER_003)
        broker_commission.confirm_payment('2026-003')
        self.seed('p-hold', 'order-hold', 'FS / TEST', amount='-39,90', kind='Erstattung')
        broker_commission.late_refund_flags('2026-003')
        round_ui._historical_matrix(workflow.positions(), 'GB-2026-001')
        self.assertEqual(before, self.historical_fingerprint())
        with core.ledger() as db:
            row = db.execute('SELECT round_id, role FROM group_b_round_positions '
                             'WHERE position_key=?', (key,)).fetchone()
        self.assertEqual((row['round_id'], row['role']), ('GB-2026-001', 'hold_reserve'))


class WordingTests(CarryForward):
    """Die drei fachlichen Anzeigezustaende - ohne technische IDs im Text."""

    def test_still_held_shows_einbehalt_in_klaerung(self):
        self.seed_hold()
        self.make_003()
        body = self.all_text(self.run_app())
        self.assertIn('Einbehalt in Klärung', body)
        self.assertNotIn('Historischer Einbehalt freigegeben', body)

    def test_released_shows_its_target_round(self):
        key = self.seed_hold()
        self.make_003()
        self.release()
        planner.carry_forward_released_holds()
        app = self.run_app()
        self.assertFalse(list(app.exception))
        body = self.all_text(app)
        self.assertIn('Historischer Einbehalt freigegeben · Abrechnung in 2026-003', body)
        self.assertNotIn('Einbehalt in Klärung', body)
        self.assertNotIn('manuelle Klärung erforderlich', body)
        self.assertNotIn(key, body)  # keine rohen position_key-Hashes im Text

    def test_settled_shows_origin_and_settlement_round(self):
        key = self.seed_hold()
        self.make_003()
        self.release()
        planner.carry_forward_released_holds()
        workflow.mark_paid_without_invoice([key], '2026-09-22', 'FS', {'GB-2026-001'},
                                           {'p-hold'}, 'tester', 'vorgetragener Einbehalt')
        app = self.run_app()
        self.assertFalse(list(app.exception))
        body = self.all_text(app)
        self.assertIn('Ursprung GB-2026-001 · freigegeben · abgerechnet in 2026-003', body)
        self.assertNotIn('Einbehalt in Klärung', body)
        self.assertNotIn(key, body)

    def test_fully_refunded_hold_reads_as_terminally_settled(self):
        """Erstattung statt Freigabe: derselbe Endzustand wie ein zero_pair -
        0,00 EUR, nichts erforderlich, kein Klaerfall."""
        self.seed_hold()
        self.seed('p-hold', 'order-hold', 'FS / TEST', amount='-39,90', kind='Erstattung')
        self.make_003()
        self.release()
        planner.carry_forward_released_holds()
        app = self.run_app()
        self.assertFalse(list(app.exception))
        body = self.all_text(app)
        self.assertIn('Einbehalt vollständig erstattet · 0,00 € · nichts erforderlich', body)
        self.assertNotIn('manuelle Klärung erforderlich', body)
        self.assertNotIn('Historischer Einbehalt freigegeben', body)

    def test_without_a_carry_forward_the_old_manual_wording_stays(self):
        """Ohne neutrale Runde (also ohne Vortrag) bleibt der unveraenderte
        Klaerhinweis aus b79bf31 stehen."""
        self.seed_hold()
        self.release()
        body = self.all_text(self.run_app())
        self.assertIn('Einbehalt aufgelöst', body)
        self.assertIn('manuelle Klärung erforderlich', body)


if __name__ == '__main__':
    unittest.main()
