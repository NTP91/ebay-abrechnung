"""Das 003+-Modell: Direktabrechnung Partner -> Evelyn plus die eigene
Vermittlungsabrechnung Patrick -> Evelyn.

Fixtures/Seed-Muster sind exakt die von test_round_status.py (dieselbe
tempdir-Isolation, dieselbe payout()-Hilfe aus test_recovery).
"""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import broker_commission
import core
import partner_conditions as conditions
import partner_export
import partner_snapshot
import position_workflow as workflow
import round_planner as planner
import round_status
import studio_view
from test_recovery import payout

BERLIN = ZoneInfo('Europe/Berlin')
BASE_CUT = datetime(2026, 9, 20, 23, 59, tzinfo=BERLIN)   # beendet 2026-003
AFTER_003 = datetime(2026, 9, 21, 0, 5, tzinfo=BERLIN)
AFTER_004 = datetime(2026, 9, 28, 0, 5, tzinfo=BERLIN)


def berlin(*args):
    return datetime(*args, tzinfo=BERLIN)


class Seeded(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        paths = patch.multiple(core, PAYOUTS_DB_PATH=str(self.root / 'Master_Payouts.csv'),
                                ORDERS_DB_PATH=str(self.root / 'Master_Orders.csv'))
        paths.start(); self.addCleanup(paths.stop)

    def seed_sale(self, payout_id, order, sku, amount='50,00', payout_date='14.09.2026'):
        frame = payout(payout_id, order, order, sku=sku, amount=amount)
        frame['Artikelnummer'] = order
        core.import_reports([frame], core.ORDERS_DB_PATH, 'orders')
        sale = payout(payout_id, order, order, sku=sku, amount=amount)
        sale['Artikelnummer'] = order
        sale['Auszahlungsdatum'] = payout_date
        sale['Auszahlungsstatus'] = 'Betrag überwiesen'
        sale['Transaktionsbetrag (inkl. Kosten)'] = amount
        core.import_reports([sale], core.PAYOUTS_DB_PATH, 'payout')

    def seed_order_without_payout(self, order, sku):
        """Eine Bestellung ohne jede eBay-Auszahlungsnummer."""
        frame = payout('', order, order, sku=sku, amount='50,00')
        frame['Artikelnummer'] = order
        core.import_reports([frame], core.ORDERS_DB_PATH, 'orders')

    def model_for(self, partner, sku):
        """prepare_partner_export fuer genau eine frisch geseedete Position."""
        business = workflow.positions()
        block = business[business.Partner == partner]
        self.assertFalse(block.empty, f'{sku} wurde nicht als {partner} erkannt')
        return partner_export.prepare_partner_export(block)


class ConditionsTests(unittest.TestCase):
    """Die zentrale Konditionsquelle selbst - eine Stelle, drei Zeilen Modell."""

    def test_group_a_is_half_percent_and_no_broker_commission(self):
        for name in ('PP', 'BA', 'MK', '001'):
            item = conditions.conditions(name)
            self.assertEqual(item['group'], 'Gruppe A')
            self.assertEqual(item['partner_rate'], Decimal('0.005'))
            self.assertEqual(item['broker_rate'], Decimal('0'))

    def test_group_b_standard_is_three_five_and_three(self):
        for name in ('MH', 'NB', 'FS'):
            item = conditions.conditions(name)
            self.assertEqual(item['group'], 'Gruppe B')
            self.assertEqual(item['partner_rate'], Decimal('0.035'))
            self.assertEqual(item['broker_rate'], Decimal('0.030'))

    def test_pm_is_two_five_and_two(self):
        item = conditions.conditions('PM')
        self.assertEqual(item['group'], 'Gruppe B')
        self.assertEqual(item['partner_rate'], Decimal('0.025'))
        self.assertEqual(item['broker_rate'], Decimal('0.020'))
        self.assertTrue(item['special'])

    def test_evelyn_keeps_exactly_half_a_percent_in_every_variant(self):
        for name in ('PP', 'MH', 'PM'):
            item = conditions.conditions(name)
            self.assertEqual(item['partner_rate'] - item['broker_rate'], Decimal('0.005'))

    def test_partner_excel_label_never_discloses_patricks_commission(self):
        self.assertNotIn('Provision', conditions.label('PM'))
        self.assertIn('2,5 %', conditions.label('PM'))
        self.assertIn('Rechnung direkt an Evelyn', conditions.label('PM'))
        self.assertIn('Patrick-Provision 2,0 %', conditions.label('PM', broker=True))


class PrefixMatchingTests(Seeded):
    """PM vs PMX: der Partnercode ist das exakte Segment VOR dem ersten Slash.
    Kein startswith() - 'PMX' darf niemals auf 'PM' abgebildet werden."""

    def test_exact_prefix_before_first_slash(self):
        self.assertEqual(core.normalized_partner('PM/ABC'), 'PM')
        self.assertEqual(core.normalized_partner('PM/'), 'PM')
        self.assertEqual(core.normalized_partner('PM / ABC'), 'PM')
        self.assertEqual(core.normalized_partner('PMX/ABC'), 'PMX')
        self.assertNotEqual(core.normalized_partner('PMX/ABC'), 'PM')

    def test_pmx_gets_neither_pms_rate_nor_pms_group_membership(self):
        self.assertEqual(conditions.partner_rate('PMX'), Decimal('0.035'))
        self.assertEqual(conditions.broker_rate('PMX'), Decimal('0.030'))

    def test_unknown_longer_prefix_never_becomes_a_known_group_a_partner(self):
        # 'PPX' beginnt mit 'PP', ist aber ein anderer, unbekannter Partner:
        # er wird als ungeklaert markiert, nicht still zu Gruppe A gemacht.
        self.assertEqual(conditions.group_for('PPX'), 'Gruppe B')
        self.seed_sale('p-ppx', 'order-ppx', 'PPX / TEST')
        master = core.load_master_data()
        row = master[master.Bestellnummer == 'order-ppx'].iloc[0]
        self.assertEqual(row.Partner, 'PPX')
        self.assertEqual(row.Gruppe, 'Ohne Zuordnung')
        self.assertIn('unbekannter Partner', row['Prüfhinweis'])

    def test_pmx_is_unknown_while_pm_is_a_confirmed_group_b_partner(self):
        self.seed_sale('p-pm', 'order-pm', 'PM / TEST')
        self.seed_sale('p-pmx', 'order-pmx', 'PMX / TEST')
        master = core.load_master_data()
        pm = master[master.Bestellnummer == 'order-pm'].iloc[0]
        pmx = master[master.Bestellnummer == 'order-pmx'].iloc[0]
        self.assertEqual((pm.Partner, pm.Gruppe), ('PM', 'Gruppe B'))
        self.assertEqual(pmx.Partner, 'PMX')
        self.assertEqual(pmx.Gruppe, 'Ohne Zuordnung')


class PartnerDeductionTests(Seeded):
    """Der Partnerabzug landet tatsaechlich in der Partner-Excel/-Abrechnung."""

    def test_group_a_partner_deduction_is_half_a_percent(self):
        self.seed_sale('p1', 'order-pp', 'PP / TEST')
        self.assertEqual(self.model_for('PP', 'PP / TEST')['rate'], Decimal('0.005'))

    def test_group_b_standard_partner_deduction_is_three_five(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST')
        self.assertEqual(self.model_for('MH', 'MH / TEST')['rate'], Decimal('0.035'))

    def test_pm_partner_deduction_is_two_five(self):
        self.seed_sale('p1', 'order-pm', 'PM / TEST')
        self.assertEqual(self.model_for('PM', 'PM / TEST')['rate'], Decimal('0.025'))

    def test_pm_stays_a_normal_group_b_partner_in_the_workflow(self):
        """Kein eigener Pfad: PM taucht in genau derselben Partnerliste und
        derselben Gruppe-B-Abrechnung auf wie MH/NB."""
        business = workflow.positions()
        confirmed = dict(planner.confirmed_partners(business))
        self.assertEqual(confirmed.get('PM'), 'Gruppe B')
        self.seed_sale('p1', 'order-pm', 'PM / TEST')
        self.assertEqual(self.model_for('PM', 'PM / TEST')['group'], 'Gruppe B')


class BrokerCommissionTests(Seeded):
    def commit(self, now=berlin(2026, 9, 18, 12, 0)):
        return planner.commit_round(now=now, base_cut=BASE_CUT)

    def test_group_b_standard_pays_three_percent(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        self.commit()
        model = broker_commission.basis('2026-003')
        self.assertEqual([p['partner'] for p in model['partners']], ['MH'])
        item = model['partners'][0]
        self.assertEqual(item['rate'], Decimal('0.030'))
        self.assertEqual(item['commission'], partner_export.cents(item['net_basis'] * Decimal('0.030')))
        self.assertEqual(model['total_commission'], item['commission'])

    def test_pm_pays_two_percent_never_three(self):
        self.seed_sale('p1', 'order-pm', 'PM / TEST', amount='119,00')
        self.commit()
        item = broker_commission.basis('2026-003')['partners'][0]
        self.assertEqual(item['partner'], 'PM')
        self.assertEqual(item['rate'], Decimal('0.020'))
        self.assertNotEqual(item['commission'], partner_export.cents(item['net_basis'] * Decimal('0.030')))

    def test_group_a_never_enters_the_broker_settlement(self):
        self.seed_sale('p1', 'order-pp', 'PP / TEST', amount='119,00')
        self.commit()
        model = broker_commission.basis('2026-003')
        self.assertEqual(model['partners'], [])
        self.assertFalse(model['required'])
        self.assertEqual(model['total_commission'], Decimal(0))
        result = broker_commission.status('2026-003')
        self.assertEqual(result['status'], 'nicht_erforderlich')
        record, created = broker_commission.finalize('2026-003', now=AFTER_003)
        self.assertIsNone(record)
        self.assertFalse(created)

    def test_mixed_round_is_one_settlement_with_per_partner_rates(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        self.seed_sale('p2', 'order-nb', 'NB / TEST', amount='238,00')
        self.seed_sale('p3', 'order-pm', 'PM / TEST', amount='357,00')
        self.commit()
        model = broker_commission.basis('2026-003')
        by_partner = {p['partner']: p for p in model['partners']}
        self.assertEqual(set(by_partner), {'MH', 'NB', 'PM'})
        self.assertEqual(by_partner['MH']['rate'], Decimal('0.030'))
        self.assertEqual(by_partner['NB']['rate'], Decimal('0.030'))
        self.assertEqual(by_partner['PM']['rate'], Decimal('0.020'))
        # Die Summe ist exakt die Summe der einzeln berechneten Teilprovisionen.
        self.assertEqual(model['total_commission'],
                         sum(p['commission'] for p in model['partners']))
        for item in model['partners']:
            self.assertEqual(item['commission'],
                             partner_export.cents(item['net_basis'] * item['rate']))
        # Genau EIN Beleg fuer die ganze Runde, trotz drei Partnern.
        record, created = broker_commission.finalize('2026-003', now=AFTER_003)
        self.assertTrue(created)
        self.assertEqual(Decimal(record['total_commission']), model['total_commission'])
        with core.ledger() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM broker_commissions').fetchone()[0], 1)
        self.assertEqual(len(json.loads(record['breakdown'])), 3)

    def test_holds_and_orders_without_payout_are_excluded(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        self.seed_order_without_payout('order-mh-open', 'MH / OFFEN')
        self.commit()
        model = broker_commission.basis('2026-003')
        self.assertEqual(model['partners'][0]['positions'], 1)
        keys = model['position_keys']
        business = workflow.positions()
        included = business[business.position_key.isin(keys)]
        self.assertTrue((included['Auszahlung Nr.'].astype(str) != '').all())
        self.assertNotIn('order-mh-open', set(included.Bestellnummer))

    def test_held_position_is_never_commission_relevant(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        self.commit()
        business = workflow.positions()
        with patch('api_holds.mask', side_effect=lambda frame: frame.index.to_series().map(lambda _: True)):
            model = broker_commission.basis('2026-003', business=business)
        self.assertFalse(model['required'])

    def test_finalize_twice_creates_no_second_commission_case(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        self.commit()
        first, created_first = broker_commission.finalize('2026-003', now=AFTER_003)
        second, created_second = broker_commission.finalize('2026-003', now=AFTER_003)
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first['snapshot_hash'], second['snapshot_hash'])
        self.assertEqual(first['total_commission'], second['total_commission'])
        with core.ledger() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM broker_commissions').fetchone()[0], 1)
            self.assertEqual(db.execute(
                'SELECT COUNT(*) FROM broker_commission_positions').fetchone()[0],
                first['position_count'])

    def test_a_position_can_back_only_one_broker_settlement_ever(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        self.commit()
        record, _ = broker_commission.finalize('2026-003', now=AFTER_003)
        key = json.loads(record['position_keys'])[0]
        with core.ledger() as db:
            broker_commission.initialize(db)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('INSERT INTO broker_commission_positions VALUES(?,?,?)',
                           (key, '2026-004', ''))

    def test_finalize_refuses_a_running_round(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        self.commit()
        with self.assertRaises(ValueError):
            broker_commission.finalize('2026-003', now=berlin(2026, 9, 19, 12, 0))

    def test_payment_is_idempotent(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        self.commit()
        broker_commission.finalize('2026-003', now=AFTER_003)
        first, created_first = broker_commission.confirm_payment('2026-003')
        second, created_second = broker_commission.confirm_payment('2026-003')
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first['paid_at'], second['paid_at'])

    def test_finalized_settlement_is_never_recomputed_from_live_data(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        self.commit()
        record, _ = broker_commission.finalize('2026-003', now=AFTER_003)
        frozen = record['total_commission']
        # Neue Live-Daten desselben Partners danach
        self.seed_sale('p9', 'order-mh-2', 'MH / SPAET', amount='999,00')
        again = broker_commission.status('2026-003')
        self.assertEqual(again['status'], 'erstellt')
        self.assertEqual(str(again['total_commission']), frozen)


class HistoricalProtectionTests(Seeded):
    def test_broker_settlement_refuses_historical_rounds(self):
        """GB-2026-001/002 laufen ueber ihr eigenes Modell; dieses Modul
        weigert sich, dort ueberhaupt zu rechnen."""
        import group_b_rounds
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        business = workflow.positions()
        with core.ledger() as db:
            group_b_rounds.initialize(db)
            broker_commission.initialize(db)
            db.execute('INSERT INTO group_b_rounds VALUES(?,?,?,?,?,?,?,?,?,?)', (
                'GB-2026-001', 2026, 1, 'immutable_evelyn_invoice', None, None,
                '0', 'hash-historisch', '{}', '2026-01-01T00:00:00.000+00:00'))
            db.commit()
        for call in (lambda: broker_commission.basis('GB-2026-001', business=business),
                     lambda: broker_commission.status('GB-2026-001', business=business),
                     lambda: broker_commission.finalize('GB-2026-001', now=AFTER_003)):
            with self.assertRaises(ValueError):
                call()

    def test_historical_round_rows_are_byte_identical_afterwards(self):
        import group_b_rounds
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        with core.ledger() as db:
            group_b_rounds.initialize(db)
            db.execute('INSERT INTO group_b_rounds VALUES(?,?,?,?,?,?,?,?,?,?)', (
                'GB-2026-002', 2026, 2, 'current_eligible_snapshot', None, None,
                '123.45', 'hash-002', '{"payouts": ["p-alt"]}', '2026-02-01T00:00:00.000+00:00'))
            db.commit()

        def fingerprint():
            with core.ledger() as db:
                return [tuple(r) for r in db.execute(
                    "SELECT * FROM group_b_rounds WHERE source_kind != 'neutral_weekly' ORDER BY id")]

        before = fingerprint()
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        broker_commission.finalize('2026-003', now=AFTER_003)
        broker_commission.confirm_payment('2026-003')
        self.assertEqual(before, fingerprint())

    def test_existing_final_partner_snapshot_is_never_regenerated(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        snap, created = partner_snapshot.finalize('2026-003', 'MH', now=AFTER_003)
        self.assertTrue(created)
        before = (snap['file_hash'], snap['snapshot_hash'], snap['final_amount'])
        broker_commission.finalize('2026-003', now=AFTER_003)
        self.seed_sale('p9', 'order-mh-2', 'MH / SPAET', amount='999,00')
        again, created_again = partner_snapshot.finalize('2026-003', 'MH', now=AFTER_003)
        self.assertFalse(created_again)
        self.assertEqual(before, (again['file_hash'], again['snapshot_hash'], again['final_amount']))


class PmActivationTests(Seeded):
    def test_pm_order_without_payout_activates_nothing(self):
        self.seed_order_without_payout('order-pm-open', 'PM / OFFEN')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self.assertIsNone(broker_commission.pm_effective_round())

    def test_first_real_pm_payout_in_003_makes_pm_effective_from_003(self):
        self.seed_sale('p1', 'order-pm', 'PM / TEST', amount='119,00', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self.assertEqual(broker_commission.pm_effective_round(), '2026-003')

    def test_first_real_pm_payout_in_004_makes_pm_effective_from_004(self):
        # 003 enthaelt nur MH, PM hat dort keine einzige echte Auszahlung.
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0))
        self.assertIsNone(broker_commission.pm_effective_round())
        # Erst in 004 taucht die erste echte PM-Auszahlung auf.
        self.seed_sale('p2', 'order-pm', 'PM / TEST', amount='119,00', payout_date='23.09.2026')
        planner.commit_round(now=berlin(2026, 9, 25, 12, 0), base_cut=BASE_CUT)
        self.assertEqual(broker_commission.pm_effective_round(), '2026-004')

    def test_determination_is_persisted_and_never_shifts_on_reimport(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00', payout_date='14.09.2026')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self.seed_sale('p2', 'order-pm', 'PM / TEST', amount='119,00', payout_date='23.09.2026')
        planner.commit_round(now=berlin(2026, 9, 25, 12, 0), base_cut=BASE_CUT)
        self.assertEqual(broker_commission.pm_effective_round(), '2026-004')
        with core.ledger() as db:
            broker_commission.initialize(db)
            stored = db.execute(
                'SELECT round_id FROM partner_condition_activation WHERE partner=?', ('PM',)).fetchone()
        self.assertEqual(stored[0], '2026-004')
        # Ein spaeterer Re-Import traegt PM-Daten in die FRUEHERE Runde 003
        # nach: die einmal getroffene Feststellung bleibt trotzdem 004.
        self.seed_sale('p3', 'order-pm-alt', 'PM / ALT', amount='119,00', payout_date='14.09.2026')
        planner.assign_late_payouts()
        self.assertEqual(broker_commission.pm_effective_round(), '2026-004')


class LateRefundSafetyTests(Seeded):
    """Fuer eine Erstattung NACH finalisiertem Vermittlungsbeleg existiert noch
    keine entschiedene Geschaeftsregel. Erwartet wird deshalb ausdruecklich:
    sichtbar melden, aber nichts automatisch korrigieren."""

    def seed_refund(self, payout_id, order, sku, amount, payout_date):
        credit = payout(payout_id, 'refund1', order, sku=sku, amount=amount, kind='Rückerstattung')
        credit['Artikelnummer'] = order
        credit['Auszahlungsdatum'] = payout_date
        credit['Transaktionsbetrag (inkl. Kosten)'] = amount
        core.import_reports([credit], core.PAYOUTS_DB_PATH, 'payout')

    def test_late_refund_is_flagged_and_changes_nothing(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        record, _ = broker_commission.finalize('2026-003', now=AFTER_003)
        frozen = dict(record)
        self.seed_refund('p1', 'order-mh', 'MH / TEST', '-119,00', '14.09.2026')
        flags = broker_commission.late_refund_flags('2026-003')
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]['partner'], 'MH')
        with core.ledger() as db:
            after = dict(db.execute('SELECT * FROM broker_commissions WHERE round_id=?',
                                    ('2026-003',)).fetchone())
        self.assertEqual(frozen, after)          # Beleg unveraendert
        result = broker_commission.status('2026-003')
        self.assertEqual(result['status'], 'erstellt')
        self.assertTrue(result['late_refunds'])
        self.assertIn('manuelle Klärung', broker_commission.label(result))

    def test_documents_the_open_divergence_that_needs_a_business_decision(self):
        """OFFENER FACHLICHER PUNKT - bewusst NICHT automatisch geloest.

        Nach einer spaeten Erstattung stehen zwei Zahlen nebeneinander:
        der eingefrorene Betrag von Patricks bereits gestellter
        Vermittlungsrechnung und sein wirtschaftlich korrekter Lifetime-Wert,
        der die Erstattung bereits beruecksichtigt. Dieser Test friert genau
        diese Differenz als BEKANNT und SICHTBAR ein. Es gibt bis zur
        Geschaeftsentscheidung keine Regel, wie sie auszugleichen ist -
        deshalb wird hier weder gutgeschrieben noch neu gerechnet, sondern
        nur gemeldet. Er schlaegt fehl, sobald jemand still eine
        Korrekturautomatik einbaut.
        """
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        record, _ = broker_commission.finalize('2026-003', now=AFTER_003)
        invoiced = Decimal(record['total_commission'])
        self.assertEqual(invoiced, studio_view.project_totals(core.load_master_data())['patrick'])

        self.seed_refund('p1', 'order-mh', 'MH / TEST', '-119,00', '14.09.2026')
        lifetime_after = studio_view.project_totals(core.load_master_data())['patrick']

        # Der Beleg bleibt exakt wie gestellt ...
        self.assertEqual(Decimal(broker_commission.status('2026-003')['total_commission']), invoiced)
        # ... der wirtschaftliche Lifetime-Wert folgt der Erstattung ...
        self.assertLess(lifetime_after, invoiced)
        # ... und die offene Differenz ist ausschliesslich als Flag sichtbar,
        # nicht als stille Gutschrift.
        self.assertTrue(broker_commission.late_refund_flags('2026-003'))


class DashboardTests(Seeded):
    """Lifetime-Provisionen: Evelyn 0,5 %, Patrick 3 % / 2 % / 0 %."""

    def totals(self):
        return studio_view.project_totals(core.load_master_data())

    def test_group_a_gives_patrick_nothing_and_evelyn_half_a_percent(self):
        self.seed_sale('p1', 'order-pp', 'PP / TEST', amount='119,00')
        totals = self.totals()
        self.assertEqual(totals['patrick'], Decimal(0))
        self.assertGreater(totals['evelyn'], Decimal(0))

    def test_standard_group_b_gives_patrick_three_percent(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        net = Decimal(str(core.load_master_data().iloc[0]['eBay_Netto']))
        totals = self.totals()
        evelyn = net - partner_export.cents(net * Decimal('.995'))
        self.assertEqual(totals['evelyn'], evelyn)
        self.assertEqual(totals['patrick'], net - partner_export.cents(net * Decimal('.965')) - evelyn)

    def test_pm_gives_patrick_two_percent_not_three(self):
        self.seed_sale('p1', 'order-pm', 'PM / TEST', amount='119,00')
        net = Decimal(str(core.load_master_data().iloc[0]['eBay_Netto']))
        totals = self.totals()
        evelyn = net - partner_export.cents(net * Decimal('.995'))
        two = net - partner_export.cents(net * Decimal('.975')) - evelyn
        three = net - partner_export.cents(net * Decimal('.965')) - evelyn
        self.assertEqual(totals['patrick'], two)
        self.assertNotEqual(totals['patrick'], three)
        self.assertEqual(totals['evelyn'], evelyn)

    def test_every_position_counted_exactly_once_in_a_mixed_portfolio(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        self.seed_sale('p2', 'order-pm', 'PM / TEST', amount='119,00')
        self.seed_sale('p3', 'order-pp', 'PP / TEST', amount='119,00')
        master = core.load_master_data()
        totals = self.totals()
        net = Decimal(str(master.iloc[0]['eBay_Netto']))

        def evelyn_of(value):
            return value - partner_export.cents(value * Decimal('.995'))

        self.assertEqual(totals['evelyn'], evelyn_of(net) * 3)
        self.assertEqual(totals['patrick'],
                         (net - partner_export.cents(net * Decimal('.965')) - evelyn_of(net))
                         + (net - partner_export.cents(net * Decimal('.975')) - evelyn_of(net)))


class RoundStatusIndependenceTests(Seeded):
    """Die neue Spur ist vollstaendig von Rechnung/Zahlung der Partner
    entkoppelt - in beide Richtungen."""

    def test_broker_status_never_reopens_partner_payment(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        result = round_status.round_status('2026-003', now=berlin(2026, 9, 21, 9, 0))
        self.assertEqual(result['broker']['status'], 'offen')
        mh = next(p for p in result['partners'] if p['partner'] == 'MH')
        # Der offene Vermittlungsbeleg taucht in KEINEM Partner-Blocker auf.
        self.assertNotIn('Vermittlung', ' '.join(mh['blockers']))
        self.assertTrue(any('Vermittlungs' in b for b in result['blockers']))

    def test_done_broker_settlement_does_not_close_an_open_partner(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        broker_commission.finalize('2026-003', now=AFTER_003)
        broker_commission.confirm_payment('2026-003')
        result = round_status.round_status('2026-003', now=berlin(2026, 9, 21, 9, 0))
        mh = next(p for p in result['partners'] if p['partner'] == 'MH')
        self.assertNotEqual(mh['overall_status'], 'abgeschlossen')
        self.assertEqual(result['round_status'], 'in_Abwicklung')


class StreamlitSmokeTests(Seeded):
    """Dasselbe AppTest-Muster wie test_round_ui.py - kein laufender Server,
    kein Supabase, keine eBay-Credentials, nur Fixture-Daten."""

    def run_app(self):
        from streamlit.testing.v1 import AppTest
        return AppTest.from_file(str(Path(__file__).with_name('app.py'))).run(timeout=60)

    def all_text(self, app):
        parts = []
        for kind in ('markdown', 'caption', 'text', 'success', 'warning', 'error',
                      'info', 'header', 'subheader'):
            parts.extend(str(element.value) for element in getattr(app, kind))
        return '\n'.join(parts)

    def test_app_renders_broker_line_and_pm_card_without_exception(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        self.seed_sale('p2', 'order-pm', 'PM / TEST', amount='119,00')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        app = self.run_app()
        self.assertFalse(list(app.exception))
        body = self.all_text(app)
        self.assertIn('Vermittlungsprovision Patrick → Evelyn', body)
        # PM hat keinen eigenen Tab und erscheint unter Gruppe B.
        labels = [tab.label for tab in app.tabs]
        self.assertIn('Gruppe B', labels)
        self.assertNotIn('PM', labels)
        self.assertIn('Sonderkondition · 2,5 % Abzug', body)
        self.assertIn('Patrick-Provision 2,0 %', body)

    def test_app_renders_after_a_finalized_broker_settlement(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        broker_commission.finalize('2026-003', now=AFTER_003)
        app = self.run_app()
        self.assertFalse(list(app.exception))
        self.assertIn('Vermittlungsabrechnung erstellt/dokumentiert', self.all_text(app))


if __name__ == '__main__':
    unittest.main()
