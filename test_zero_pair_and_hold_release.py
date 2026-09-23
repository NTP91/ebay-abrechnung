"""Zwei historische Sonderfaelle aus GB-2026-001/002 und ihr Dublettenschutz.

Fall 1 (zero_pair): ein Verkauf und die exakt gegenlaeufige Erstattung
derselben Bestellung. Wirtschaftlich 0,00 EUR - also weder Partnerrechnung
noch Partnerzahlung erforderlich. Geprueft wird ausschliesslich die generische
Eigenschaft (role='zero_pair' + gespeicherte group_b_round_refunds-Verknuepfung
+ Verkauf + Erstattung == exakt 0), nie eine konkrete Bestellnummer.

Fall 2 (hold_reserve): ein noch offener eBay-Einbehalt in einer historischen
Runde. Solange er offen ist: keine Rechnung, keine Zahlung, keine
Vermittlungsprovision - aber sichtbar als "Einbehalt in Klaerung". Und nach
einem spaeteren Release: wohin die Position dann laeuft.

Fixtures/Seed-Muster wie test_round_ui.py / test_broker_commission.py.
"""
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import api_holds
import broker_commission
import core
import group_b_rounds
import position_workflow as workflow
import round_planner as planner
import round_ui
from test_recovery import payout

BERLIN = ZoneInfo('Europe/Berlin')
BASE_CUT = datetime(2026, 9, 20, 23, 59, tzinfo=BERLIN)
AFTER_003 = datetime(2026, 9, 21, 0, 5, tzinfo=BERLIN)
MATRIX_ROWS = ['Einzelabrechnung', 'Rechnung', 'Zahlung', 'Gutschrift', 'Status']


def berlin(*args):
    return datetime(*args, tzinfo=BERLIN)


def hold_movement(order, identifier, value, **values):
    """Ein offener SALE-Einbehalt (FUNDS_ON_HOLD) - api_holds.active()."""
    row = dict(orderId=order, transactionId=identifier, transactionType='SALE',
               bookingEntry='DEBIT', amount={'value': value, 'currency': 'EUR'},
               transactionStatus='FUNDS_ON_HOLD', transactionDate='2026-09-03T10:00:00Z')
    row.update(values)
    return row


def release_movement(order, identifier, value):
    """Die spaetere, dokumentierte Freigabe genau dieses Einbehalts: gleiche
    transactionId/-Type, gleicher Betrag, CREDIT + PAYOUT mit payoutId."""
    return dict(orderId=order, transactionId=identifier, transactionType='SALE',
                bookingEntry='CREDIT', amount={'value': value, 'currency': 'EUR'},
                transactionStatus='PAYOUT', payoutId='po-release',
                transactionDate='2026-09-10T10:00:00Z')


def evidence(rows, at):
    return dict(account='ebay_durchstart', fetched_at=at,
                resources={'transactions': {'available': True, 'data': {'items': rows}}})


class Seeded(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        paths = patch.multiple(core, PAYOUTS_DB_PATH=str(self.root / 'Master_Payouts.csv'),
                                ORDERS_DB_PATH=str(self.root / 'Master_Orders.csv'))
        paths.start()
        self.addCleanup(paths.stop)

    def seed(self, payout_id, order, sku, amount='1150,00', kind='Bestellung',
             transaction=None, payout_date='14.09.2026'):
        """Eine Bewegung in Bestell- und Payoutbericht. Artikelnummer wird
        gesetzt, damit core._refund_matches() Verkauf und Erstattung derselben
        Bestellzeile ueberhaupt zusammenfuehren kann."""
        for path, kindname in ((core.ORDERS_DB_PATH, 'orders'), (core.PAYOUTS_DB_PATH, 'payout')):
            frame = payout(payout_id, transaction or f'{order}-{kind}', order, sku=sku,
                           amount=amount, kind=kind)
            frame['Artikelnummer'] = order
            frame['Auszahlungsdatum'] = payout_date
            frame['Auszahlungsstatus'] = 'Betrag überwiesen'
            frame['Transaktionsbetrag (inkl. Kosten)'] = amount
            core.import_reports([frame], path, kindname)

    def make_round(self, round_id, sequence):
        with core.ledger() as db:
            group_b_rounds.initialize(db)
            db.execute('INSERT OR IGNORE INTO group_b_rounds VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (round_id, 2026, sequence, 'current_eligible_snapshot', None, None,
                        '0', 'hash-' + round_id, '{"payouts": []}', '2026-01-01T00:00:00.000+00:00'))
            db.commit()

    def assign(self, position_key, round_id, role='evelyn_invoice', sequence=2):
        self.make_round(round_id, sequence)
        with core.ledger() as db:
            group_b_rounds.initialize(db)
            db.execute('INSERT OR REPLACE INTO group_b_round_positions VALUES(?,?,?,?)',
                       (position_key, round_id, role, 'test'))
            db.commit()

    def link_refund(self, refund_key, sale_key, round_id):
        with core.ledger() as db:
            group_b_rounds.initialize(db)
            db.execute('INSERT OR REPLACE INTO group_b_round_refunds VALUES(?,?,?,?,?)',
                       (refund_key, sale_key, round_id, round_id, 'test'))
            db.commit()

    def keys_for(self, order):
        rows = workflow.positions()
        block = rows[rows.Bestellnummer == order]
        return {row.Art: row.position_key for _, row in block.iterrows()}

    def historical_fingerprint(self):
        """Byte-genaue Momentaufnahme aller historischen Rundendaten."""
        with core.ledger() as db:
            group_b_rounds.initialize(db)
            rounds = [tuple(r) for r in db.execute(
                "SELECT * FROM group_b_rounds WHERE source_kind != 'neutral_weekly' ORDER BY id")]
            historical_ids = [r[0] for r in db.execute(
                "SELECT id FROM group_b_rounds WHERE source_kind != 'neutral_weekly'")]
            marks = ','.join('?' * len(historical_ids)) or "''"
            positions = [tuple(r) for r in db.execute(
                f'SELECT * FROM group_b_round_positions WHERE round_id IN ({marks}) ORDER BY position_key',
                historical_ids)]
            refunds = [tuple(r) for r in db.execute(
                f'SELECT * FROM group_b_round_refunds WHERE origin_round_id IN ({marks}) ORDER BY refund_key',
                historical_ids)]
        return rounds, positions, refunds

    def run_app(self):
        from streamlit.testing.v1 import AppTest
        return AppTest.from_file(str(Path(__file__).with_name('app.py'))).run(timeout=60)

    def all_text(self, app):
        parts = []
        for kind in ('markdown', 'caption', 'text', 'success', 'warning', 'error', 'info',
                     'header', 'subheader'):
            parts.extend(str(element.value) for element in getattr(app, kind))
        return '\n'.join(parts)


class ZeroPairTests(Seeded):
    """Fall 1: Verkauf + eigene Erstattung = exakt 0,00 EUR."""

    def seed_zero_pair(self, refund_amount='-1150,00', link=True, round_id='GB-2026-002'):
        self.seed('p-zp', 'order-zp', 'FS / TEST', amount='1150,00')
        self.seed('p-zp', 'order-zp', 'FS / TEST', amount=refund_amount, kind='Erstattung')
        keys = self.keys_for('order-zp')
        self.assign(keys['Bestellung'], round_id, role='zero_pair')
        if link:
            self.link_refund(keys['Erstattung'], keys['Bestellung'], round_id)
        return keys

    def test_partner_is_recognised_as_group_b(self):
        self.seed('p-zp', 'order-zp', 'FS / TEST')
        row = workflow.positions().iloc[0]
        self.assertEqual(row.Partner, 'FS')
        self.assertEqual(row.Gruppe, 'Gruppe B')

    def test_zero_pair_is_economically_zero(self):
        self.seed_zero_pair()
        sale = workflow.positions()
        sale = sale[(sale.Bestellnummer == 'order-zp') & (sale.Art == 'Bestellung')].iloc[0]
        self.assertEqual(Decimal(str(sale['Offen_Brutto'])), Decimal('0.00'))
        self.assertEqual(Decimal(str(sale['Erlös_Brutto'])) + Decimal(str(sale['Erstattet_Brutto'])),
                         Decimal('0'))

    def test_fully_neutralized_zero_pair_shows_no_invoice_blocker(self):
        self.seed_zero_pair()
        frame, blockers, header_icon = round_ui._historical_matrix(workflow.positions(), 'GB-2026-002')
        self.assertIsNotNone(frame)
        self.assertEqual(frame.loc['Rechnung', 'FS'], '➖')
        self.assertNotIn('FS · Rechnung fehlt', blockers)

    def test_fully_neutralized_zero_pair_shows_no_payment_blocker(self):
        self.seed_zero_pair()
        frame, blockers, header_icon = round_ui._historical_matrix(workflow.positions(), 'GB-2026-002')
        self.assertEqual(frame.loc['Zahlung', 'FS'], '➖')
        self.assertEqual(frame.loc['Gutschrift', 'FS'], '➖')
        self.assertEqual(frame.loc['Status', 'FS'], '➖')
        self.assertNotIn('FS · Zahlung offen', blockers)
        self.assertEqual(blockers, [])
        self.assertEqual(header_icon, '✅')

    def test_zero_pair_is_no_open_older_case_and_no_open_document(self):
        self.seed_zero_pair()
        import partner_invoices
        business = workflow.positions()
        invoices = partner_invoices.list_invoices()  # eigener ledger() - vor unserem oeffnen
        with core.ledger() as db:
            group_b_rounds.initialize(db)
            case = round_ui._historical_partner_case(business, ['GB-2026-002'], 'FS',
                                                      invoices=invoices, db=db)
            archive = round_ui._historical_round_partner_cases(business, 'GB-2026-002',
                                                               invoices=invoices, db=db)
        self.assertIsNone(case)
        self.assertEqual(archive, [])

    def test_partial_refund_is_not_treated_as_settled(self):
        """SICHERHEIT: ein zero_pair, dessen Betraege NICHT exakt aufgehen,
        faellt auf die unveraenderte ❌-Logik zurueck."""
        self.seed_zero_pair(refund_amount='-500,00')
        frame, blockers, _ = round_ui._historical_matrix(workflow.positions(), 'GB-2026-002')
        self.assertEqual(frame.loc['Rechnung', 'FS'], '❌')
        self.assertEqual(frame.loc['Zahlung', 'FS'], '❌')
        self.assertIn('FS · Rechnung fehlt', blockers)

    def test_zero_pair_without_stored_refund_link_is_not_treated_as_settled(self):
        """SICHERHEIT: die role allein genuegt nicht - ohne gespeicherte
        group_b_round_refunds-Zeile bleibt es ein offener Fall."""
        self.seed_zero_pair(link=False)
        frame, blockers, _ = round_ui._historical_matrix(workflow.positions(), 'GB-2026-002')
        self.assertEqual(frame.loc['Rechnung', 'FS'], '❌')
        self.assertEqual(frame.loc['Zahlung', 'FS'], '❌')

    def test_held_zero_pair_candidate_is_never_swept_into_erledigt(self):
        """SICHERHEIT: ein noch laufender API-Hold wird nie als erledigt
        behandelt - er bleibt in der eigenen Hold-Kategorie."""
        self.seed_zero_pair()
        api_holds.ingest(str(self.root), evidence([hold_movement('order-zp', 'SALE-zp', '1150.00')],
                                                   '2026-09-04T10:00:00Z'))
        business = workflow.positions()
        active, settled, held = round_ui._historical_round_rows(business, 'GB-2026-002')
        self.assertTrue(active.empty)
        self.assertTrue(settled.empty)
        self.assertEqual(held.Bestellnummer.tolist(), ['order-zp'])

    def test_mixed_partner_keeps_the_open_position_open(self):
        """Ein Partner mit einer erledigten zero_pair UND einer echten offenen
        Position bleibt insgesamt ❌ - die 0-Position verdeckt nichts."""
        self.seed_zero_pair()
        self.seed('p-open', 'order-open', 'FS / TEST', amount='80,00')
        self.assign(self.keys_for('order-open')['Bestellung'], 'GB-2026-002')
        frame, blockers, header_icon = round_ui._historical_matrix(workflow.positions(), 'GB-2026-002')
        self.assertEqual(frame.loc['Rechnung', 'FS'], '❌')
        self.assertEqual(frame.loc['Status', 'FS'], '❌')
        self.assertEqual(header_icon, '❌')

    def test_app_renders_zero_pair_round_without_exception(self):
        self.seed_zero_pair()
        app = self.run_app()
        self.assertFalse(list(app.exception))
        matrix = next(el.value for el in app.dataframe
                      if list(el.value.index) == MATRIX_ROWS and 'FS' in el.value.columns)
        self.assertEqual(matrix.loc['Rechnung', 'FS'], '➖')
        self.assertEqual(matrix.loc['Zahlung', 'FS'], '➖')
        body = self.all_text(app)
        self.assertNotIn('FS · Rechnung fehlt', body)
        self.assertNotIn('FS · Zahlung offen', body)
        self.assertTrue(any(label.startswith('✅ GB-2026-002') for label in
                            (exp.label for exp in app.expander)))


class HistoricalHoldTests(Seeded):
    """Fall 2: offener Einbehalt in einer historischen Runde."""

    def seed_hold(self, round_id='GB-2026-001', amount='39,90'):
        self.seed('p-hold', 'order-hold', 'FS / TEST', amount=amount)
        key = self.keys_for('order-hold')['Bestellung']
        self.assign(key, round_id, role='hold_reserve', sequence=1)
        api_holds.ingest(str(self.root), evidence(
            [hold_movement('order-hold', 'SALE-hold', amount.replace(',', '.'))],
            '2026-09-04T10:00:00Z'))
        return key

    def release(self, amount='39,90', at='2026-09-11T10:00:00Z'):
        """Nur die Freigabebuchung - api_holds.active() verlangt eine STRIKT
        spaetere Beobachtung als die juengste Hold-Beobachtung."""
        api_holds.ingest(str(self.root), evidence(
            [release_movement('order-hold', 'SALE-hold', amount.replace(',', '.'))], at))

    def test_open_hold_is_flagged_and_excluded_from_the_matrix(self):
        self.seed_hold()
        business = workflow.positions()
        self.assertTrue(business[business.Bestellnummer == 'order-hold'].API_Hold.all())
        active, settled, held = round_ui._historical_round_rows(business, 'GB-2026-001')
        self.assertTrue(active.empty)
        self.assertTrue(settled.empty)
        self.assertEqual(held.Bestellnummer.tolist(), ['order-hold'])

    def test_open_hold_is_visible_as_einbehalt_in_klaerung(self):
        self.seed_hold()
        app = self.run_app()
        self.assertFalse(list(app.exception))
        body = self.all_text(app)
        self.assertIn('Einbehalt in Klärung', body)
        self.assertNotIn('Einbehalt aufgelöst', body)

    def test_released_reserve_is_shown_as_aufgeloest_not_still_in_klaerung(self):
        self.seed_hold()
        self.release()
        app = self.run_app()
        self.assertFalse(list(app.exception))
        body = self.all_text(app)
        self.assertIn('Einbehalt aufgelöst', body)
        self.assertIn('GB-2026-001 zugeordnet', body)
        self.assertNotIn('Einbehalt in Klärung', body)

    def test_open_hold_is_not_eligible_for_invoice_payment_or_commission(self):
        key = self.seed_hold()
        # Keine Partnerzahlung/-pruefung moeglich, solange der Hold laeuft.
        for action in ('review', 'partner_paid'):
            with self.assertRaises(ValueError):
                workflow.confirm([key], action, '2026-09-15', invoice_id='x')
        # Keine Aufnahme in eine neue 2026-003+-Runde ...
        plan = planner.plan_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self.assertNotIn(key, {item['position_key'] for item in plan['included']})
        # ... und damit auch keine Vermittlungsprovision.
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        result = broker_commission.basis('2026-003')
        self.assertNotIn(key, result['position_keys'])

    def test_released_hold_stays_in_its_historical_round(self):
        """Der generelle Mechanismus: group_b_round_positions.position_key ist
        PRIMARY KEY, und round_planner.plan_round() schliesst jede bereits
        zugeordnete Position als 'historisch_zugeordnet' aus. Eine nachtraeglich
        freigegebene historische Hold-Position bleibt deshalb strukturell in
        GB-2026-001 und wandert NICHT in eine neue 2026-003+-Runde."""
        key = self.seed_hold()
        self.release()
        business = workflow.positions()
        self.assertFalse(business[business.Bestellnummer == 'order-hold'].API_Hold.any())
        plan = planner.plan_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self.assertNotIn(key, {item['position_key'] for item in plan['included']})
        self.assertIn(key, {item['position_key'] for item in plan['excluded']['historisch_zugeordnet']})
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        planner.assign_late_payouts()
        with core.ledger() as db:
            round_id = db.execute('SELECT round_id FROM group_b_round_positions WHERE position_key=?',
                                   (key,)).fetchone()[0]
        self.assertEqual(round_id, 'GB-2026-001')

    def test_historical_origin_is_still_readable_after_release(self):
        key = self.seed_hold()
        before = self.historical_fingerprint()
        self.release()
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        planner.assign_late_payouts()
        with core.ledger() as db:
            row = db.execute('SELECT round_id, role, source FROM group_b_round_positions '
                              'WHERE position_key=?', (key,)).fetchone()
        self.assertEqual((row['round_id'], row['role']), ('GB-2026-001', 'hold_reserve'))
        self.assertEqual(before, self.historical_fingerprint())

    def test_repeated_release_import_is_idempotent(self):
        key = self.seed_hold()
        self.release()
        before = self.historical_fingerprint()
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        first = planner.assign_late_payouts()
        self.release(at='2026-09-12T10:00:00Z')
        self.release(at='2026-09-13T10:00:00Z')
        second = planner.assign_late_payouts()
        planner.rollover(now=berlin(2026, 9, 22, 12, 0))
        with core.ledger() as db:
            count = db.execute('SELECT COUNT(*) FROM group_b_round_positions WHERE position_key=?',
                                (key,)).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual([item for item in first + second if item['position_key'] == key], [])
        self.assertEqual(before, self.historical_fingerprint())

    def test_hold_then_refund_ends_at_zero_without_a_new_round_position(self):
        """Statt Release eine volle Erstattung: wirtschaftlich 0,00 EUR, keine
        neue 2026-003+-Position, keine Zahlung, keine Provision."""
        key = self.seed_hold()
        self.seed('p-hold', 'order-hold', 'FS / TEST', amount='-39,90', kind='Erstattung')
        self.release()
        business = workflow.positions()
        sale = business[(business.Bestellnummer == 'order-hold') & (business.Art == 'Bestellung')].iloc[0]
        self.assertEqual(Decimal(str(sale['Offen_Brutto'])), Decimal('0.00'))
        plan = planner.plan_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self.assertNotIn(key, {item['position_key'] for item in plan['included']})
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        self.assertNotIn(key, broker_commission.basis('2026-003')['position_keys'])
        with core.ledger() as db:
            round_id = db.execute('SELECT round_id FROM group_b_round_positions WHERE position_key=?',
                                   (key,)).fetchone()[0]
        self.assertEqual(round_id, 'GB-2026-001')

    def test_historical_rounds_are_never_reopened_or_mutated(self):
        self.seed_hold()
        self.seed('p-new', 'order-new', 'FS / NEU', amount='100,00')
        before = self.historical_fingerprint()
        self.release()
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        broker_commission.finalize('2026-003', now=AFTER_003)
        broker_commission.confirm_payment('2026-003')
        planner.assign_late_payouts()
        round_ui._historical_matrix(workflow.positions(), 'GB-2026-001')
        self.assertEqual(before, self.historical_fingerprint())

    def test_no_duplicate_broker_commission_for_a_position_with_historical_past(self):
        """Eine Position mit 001/002-Vergangenheit, die erstmals in das neue
        Provisionsmodell liefe, kann dort nur EINMAL landen:
        broker_commission_positions.position_key ist PRIMARY KEY ueber ALLE
        Runden."""
        key = self.seed_hold()
        self.seed('p-new', 'order-new', 'FS / NEU', amount='100,00')
        self.release()
        planner.commit_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        broker_commission.finalize('2026-003', now=AFTER_003)
        with core.ledger() as db:
            broker_commission.initialize(db)
            rows = list(db.execute('SELECT position_key, round_id FROM broker_commission_positions'))
            # Die historische Hold-Position ist gar nicht erst enthalten ...
            self.assertNotIn(key, {r['position_key'] for r in rows})
            # ... und ein zweiter Eintrag derselben Position ist strukturell
            # ausgeschlossen.
            existing = rows[0]['position_key']
            with self.assertRaises(Exception):
                db.execute('INSERT INTO broker_commission_positions VALUES(?,?,?)',
                           (existing, '2026-004', 'zweiter Versuch'))
                db.commit()
            db.rollback()

    def test_no_second_partner_payment_for_an_already_paid_position(self):
        self.seed('p-new', 'order-new', 'FS / NEU', amount='100,00')
        key = self.keys_for('order-new')['Bestellung']
        self.assign(key, 'GB-2026-002')
        workflow.mark_paid_without_invoice([key], '2026-09-15', 'FS', {'GB-2026-002'}, {'p-new'},
                                            'tester', 'historischer Sammelfall')
        with self.assertRaises(ValueError):
            workflow.mark_paid_without_invoice([key], '2026-09-16', 'FS', {'GB-2026-002'}, {'p-new'},
                                                'tester', 'zweiter Versuch')
        # Eine zweite, nicht zugeordnete, bereits bezahlte Position landet in
        # keiner neuen 2026-003+-Runde.
        self.seed('p-paid', 'order-paid', 'FS / NEU2', amount='70,00')
        other = self.keys_for('order-paid')['Bestellung']
        self.assign(other, 'GB-2026-002')
        workflow.mark_paid_without_invoice([other], '2026-09-15', 'FS', {'GB-2026-002'}, {'p-paid'},
                                            'tester', 'historischer Sammelfall')
        plan = planner.plan_round(now=berlin(2026, 9, 18, 12, 0), base_cut=BASE_CUT)
        included = {item['position_key'] for item in plan['included']}
        self.assertNotIn(key, included)
        self.assertNotIn(other, included)


if __name__ == '__main__':
    unittest.main()
