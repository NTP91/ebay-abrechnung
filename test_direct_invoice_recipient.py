"""Zwei bestaetigte Finanzrisiko-Bugs, abgesichert:

1. Belegempfaenger der 003+-Einzelabrechnung: ab Runde 2026-003 rechnet JEDER
   Partner (Gruppe A und B, PM eingeschlossen) direkt mit Evelyn ab, also muss
   Evelyn auf dem Beleg stehen. Die historische GB-2026-001/002-Logik
   (Patrick kassiert Gruppe B und stellt Evelyn eine Sammelrechnung) bleibt
   unveraendert - dort steht weiterhin Patrick auf dem Partnerbeleg.

2. Kein 003+-Posten darf noch einmal ueber das alte Lexware-/Evelyn-
   Sammelmodell abgerechnet werden - weder in der Auswahlliste noch in der
   tatsaechlichen Uebertragung.

Fixtures: dieselbe Seeded-Basis wie test_broker_commission.py.
"""
import io
import unittest
from decimal import Decimal

from openpyxl import load_workbook

import core
import group_b_rounds
import partner_export
import partner_snapshot
import position_workflow as workflow
import round_planner as planner
import studio_view
from test_broker_commission import AFTER_003, BASE_CUT, Seeded, berlin

COMMIT_NOW = berlin(2026, 9, 18, 12, 0)


def recipient_of(blob):
    return load_workbook(io.BytesIO(blob), data_only=True)['Rechnung']['E4'].value


class DirectInvoiceRecipientTests(Seeded):
    """partner_export.py: Empfaenger haengt am Abrechnungsmodell, nicht am Satz."""

    def final_recipient(self, partner, sku, order):
        self.seed_sale('p1', order, sku, amount='119,00')
        planner.commit_round(now=COMMIT_NOW, base_cut=BASE_CUT)
        record, created = partner_snapshot.finalize('2026-003', partner, now=AFTER_003)
        self.assertTrue(created)
        return recipient_of(record['file_bytes'])

    def test_group_b_partner_invoice_names_evelyn(self):
        self.assertEqual(self.final_recipient('MH', 'MH / TEST', 'order-mh'), 'Evelyn')

    def test_pm_special_condition_still_names_evelyn(self):
        self.assertEqual(self.final_recipient('PM', 'PM / TEST', 'order-pm'), 'Evelyn')

    def test_group_a_partner_invoice_still_names_evelyn(self):
        """Regression: Gruppe A war bereits korrekt und bleibt es."""
        self.assertEqual(self.final_recipient('PP', 'PP / TEST', 'order-pp'), 'Evelyn')

    def test_interim_export_uses_the_same_recipient_as_the_final_one(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        planner.commit_round(now=COMMIT_NOW, base_cut=BASE_CUT)
        self.assertEqual(recipient_of(partner_snapshot.interim_export('2026-003', 'MH')), 'Evelyn')

    def test_historical_group_b_statement_keeps_patrick(self):
        """Pin fuer das alte Modell: ohne direct_to_evelyn bleibt alles wie
        bisher, damit bereits erzeugte GB-2026-001/002-Belege unveraendert
        reproduzierbar sind."""
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        block = workflow.positions()
        self.assertEqual(partner_export.prepare_partner_export(block)['recipient'], 'Patrick Pfender')
        self.assertEqual(recipient_of(partner_export.export_partner_excel(block)), 'Patrick Pfender')
        # Die Gruppe-B-Sammelrechnung an Evelyn selbst war und bleibt an Evelyn.
        self.assertEqual(partner_export.prepare_partner_export(
            block, statement_type='group_b_evelyn')['recipient'], 'Evelyn')

    def test_only_the_recipient_changes_not_the_amounts(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        block = workflow.positions()
        old = partner_export.prepare_partner_export(block)
        new = partner_export.prepare_partner_export(block, direct_to_evelyn=True)
        self.assertEqual(old['rate'], new['rate'])
        self.assertEqual(old['totals'], new['totals'])
        self.assertNotEqual(old['recipient'], new['recipient'])

    def test_existing_final_snapshot_is_never_regenerated(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        planner.commit_round(now=COMMIT_NOW, base_cut=BASE_CUT)
        first, created = partner_snapshot.finalize('2026-003', 'MH', now=AFTER_003)
        self.assertTrue(created)
        before = (bytes(first['file_bytes']), first['file_hash'], first['snapshot_hash'], first['final_amount'])
        again, created_again = partner_snapshot.finalize('2026-003', 'MH', now=AFTER_003)
        self.assertFalse(created_again)
        self.assertEqual(before, (bytes(again['file_bytes']), again['file_hash'],
                                   again['snapshot_hash'], again['final_amount']))


class LegacyLexwareExclusionTests(Seeded):
    """studio_view/group_b_rounds: 003+-Positionen verlassen das Altmodell."""

    def overview(self):
        business = workflow.positions()
        master = core.load_master_data()
        eligible = studio_view.eligible_rows(master, core.sync_status(master))
        return studio_view.evelyn_overview(business, eligible, {})

    def assign_historical(self, position_key, round_id='GB-2026-002', sequence=2):
        with core.ledger() as db:
            group_b_rounds.initialize(db)
            db.execute('INSERT OR IGNORE INTO group_b_rounds VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (round_id, 2026, sequence, 'current_eligible_snapshot', None, None, '0',
                        'hash-' + round_id, '{"payouts": []}', '2026-01-01T00:00:00Z'))
            db.execute('INSERT OR REPLACE INTO group_b_round_positions VALUES(?,?,?,?)',
                       (position_key, round_id, 'evelyn_invoice', 'test'))
            db.commit()

    def test_fresh_003_position_disappears_from_the_legacy_eligible_list(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        before = self.overview()
        self.assertIn('order-mh', before['new_ready'].Bestellnummer.tolist())
        self.assertGreater(before['total'], Decimal('0'))

        planner.commit_round(now=COMMIT_NOW, base_cut=BASE_CUT)
        after = self.overview()
        for bucket in ('ready', 'review', 'held', 'new_ready', 'new_review', 'new_held', 'prior_held'):
            self.assertNotIn('order-mh', after[bucket].Bestellnummer.tolist(), bucket)
        self.assertEqual(after['total'], Decimal('0'))

    def test_historical_round_positions_stay_fully_visible(self):
        """GB-2026-002 (source_kind != 'neutral_weekly') bleibt unberuehrt -
        Positionszahl und Summe der Altansicht aendern sich nicht."""
        for index in range(3):
            self.seed_sale('p1', f'order-hist-{index}', 'NB / TEST', amount='119,00')
        before = self.overview()
        self.assertEqual(len(before['new_ready']), 3)
        for key in before['new_ready'].position_key:
            self.assign_historical(key)
        after = self.overview()
        self.assertEqual(len(after['new_ready']), 3)
        self.assertEqual(after['total'], before['total'])
        self.assertEqual(sorted(after['new_ready'].Bestellnummer), sorted(before['new_ready'].Bestellnummer))

    def test_transmission_rejects_a_003_position_even_if_forced_into_the_selection(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        self.seed_sale('p1', 'order-nb', 'NB / TEST', amount='59,00')
        chosen = self.overview()['new_ready']
        self.assertEqual(len(chosen), 2)
        planner.commit_round(now=COMMIT_NOW, base_cut=BASE_CUT)
        # order-nb wird kuenstlich als historischer GB-2026-002-Bestand
        # hinterlegt, order-mh bleibt in 2026-003 - die Auswahl enthaelt
        # (wie bei einem hypothetisch zurueckkehrenden UI-Filter-Bug) beide.
        nb_key = chosen.loc[chosen.Bestellnummer == 'order-nb', 'position_key'].iloc[0]
        with core.ledger() as db:
            group_b_rounds.initialize(db)
            db.execute('INSERT OR IGNORE INTO group_b_rounds VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (group_b_rounds.ROUND_TWO, 2026, 2, 'current_eligible_snapshot', None, None, '0',
                        'hash-gb002', '{"payouts": []}', '2026-01-01T00:00:00Z'))
            db.execute('INSERT OR REPLACE INTO group_b_round_positions VALUES(?,?,?,?)',
                       (nb_key, group_b_rounds.ROUND_TWO, 'evelyn_invoice', 'test'))
            db.commit()
        business = workflow.positions()
        with core.ledger() as db:
            with self.assertRaisesRegex(ValueError, '2026-003'):
                group_b_rounds.evelyn_link_precheck(db, group_b_rounds.ROUND_TWO, business, {}, chosen)

    def test_invoice_payload_refuses_a_payout_containing_a_003_position(self):
        """Der Lexware-Entwurf wird aus dem GANZEN Payout gebaut, nicht aus
        der UI-Auswahl - ein gemischter Payout muss serverseitig scheitern."""
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        master = core.load_master_data()
        core.sync_status(master)
        self.assertTrue(core.build_invoice_payload(master, 'p1', 'contact', True)['lineItems'])
        planner.commit_round(now=COMMIT_NOW, base_cut=BASE_CUT)
        with self.assertRaisesRegex(ValueError, '2026-003'):
            core.build_invoice_payload(core.load_master_data(), 'p1', 'contact', True)

    def test_no_double_booking_settled_and_transmittable_are_disjoint(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        self.seed_sale('p1', 'order-nb', 'NB / TEST', amount='59,00')
        planner.commit_round(now=COMMIT_NOW, base_cut=BASE_CUT)
        partner_snapshot.finalize('2026-003', 'MH', now=AFTER_003)
        settled = set(studio_view.neutral_round_keys())
        self.assertTrue(settled)
        transmittable = set(self.overview()['new_ready'].position_key)
        self.assertFalse(settled & transmittable)


class LegacyLexwareAreaSmokeTests(Seeded):
    """Gerendertes UI: der Gruppe-B-Tab laeuft fehlerfrei und die Altansicht
    meldet eine frische 2026-003-Position nicht mehr als Lexware-bereit."""

    def run_app(self):
        from streamlit.testing.v1 import AppTest
        from pathlib import Path
        return AppTest.from_file(str(Path(__file__).with_name('app.py'))).run(timeout=60)

    def all_text(self, app):
        parts = []
        for kind in ('markdown', 'caption', 'text', 'success', 'warning', 'error', 'info',
                      'header', 'subheader'):
            parts.extend(str(element.value) for element in getattr(app, kind))
        for metric in app.metric:
            parts.append(f'{metric.label} {metric.value}')
        return '\n'.join(parts)

    def test_fresh_003_position_is_not_reported_as_ready_for_lexware(self):
        self.seed_sale('p1', 'order-mh', 'MH / TEST', amount='119,00')
        before = self.all_text(self.run_app())
        self.assertIn('1 neu für Lexware bereit', before)

        planner.commit_round(now=COMMIT_NOW, base_cut=BASE_CUT)
        app = self.run_app()
        self.assertFalse(list(app.exception))
        body = self.all_text(app)
        self.assertIn('0 neu für Lexware bereit', body)
        self.assertIn('Neu für Evelyn (Altmodell) 0', body)
        self.assertIn('Gruppe B', [tab.label for tab in app.tabs])


if __name__ == '__main__':
    unittest.main()
