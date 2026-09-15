"""Final MH read-only safety check for two open points against the real
production data.

1. Order 15-15104-67168 / payout 7725289401: show every payout row and
   every order position for this order, confirm both SKUs classify as MH
   via core.normalized_partner(), confirm whether core.match_order()
   excludes any of its payout rows, confirm whether it is present in the
   current MH settlement (core.load_master_data(), Partner=='MH') for
   this payout — exactly once, not duplicated — and what amount it
   contributes to the current MH partner claim.

2. Paid positions (real Auszahlung Nr.) with no Bestellnummer at all:
   list every field present and classify each by its own Typ text
   (Verkauf vs. Einbehalt/Erstattung/Gebühr/Auszahlungs-Kontrollzeile/
   unklar) — no partner is invented for these, since partner assignment
   requires a SKU that cannot be resolved without an order match.

Also reports the total count of core.match_order()-unmatched paid
positions overall, for context.

Duplicate check note: "Dublette" means the SAME position (same
Transaktionsnummer) appears more than once — not simply "this order has
more than one row", since a legitimate multi-line order has several
distinct Transaktionsnummer/SKU line items under one Bestellnummer.

Purely read-only: only core.read_master()/core.match_order()/
core.normalized_partner()/core.load_master_data() are used. No
supabase_store.put/put_json, no core.import_reports, no
core.sync_status, no ebay_sync.run, no position_workflow.confirm, no
Lexware/invoice call anywhere in this file.

Usage:
    PAYMENT_BACKEND=supabase SUPABASE_ACCESS_TOKEN=... SUPABASE_PROJECT_REF=... \\
        python3 scripts/mh_final_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core
import supabase_store

TARGET_ORDER = '15-15104-67168'
TARGET_PAYOUT = '7725289401'


def main():
    supabase_store.preflight()
    payouts = core.read_master(core.PAYOUTS_DB_PATH)
    orders = core.read_master(core.ORDERS_DB_PATH)
    master = core.load_master_data()

    print('=' * 78)
    print(f'TEIL 1: Bestellung {TARGET_ORDER!r} / Payout {TARGET_PAYOUT!r}')
    print('=' * 78)

    payout_rows = payouts[payouts['Bestellnummer'] == TARGET_ORDER]
    print(f'\n-- Payout-Zeilen für Bestellnummer {TARGET_ORDER!r} ({len(payout_rows)}) --')
    for _, row in payout_rows.iterrows():
        print(' ', dict(row))

    order_rows = orders[orders['Bestellnummer'] == TARGET_ORDER]
    print(f'\n-- Bestellpositionen für {TARGET_ORDER!r} ({len(order_rows)}) --')
    for _, row in order_rows.iterrows():
        sku = row['SKU']
        partner = core.normalized_partner(sku) if sku else '(keine SKU)'
        print(f"  Transaktionsnummer={row['Transaktionsnummer']!r} Artikelnummer={row['Artikelnummer']!r} "
              f"SKU={sku!r} Titel={row['Angebotstitel']!r} -> normalized_partner={partner!r}")

    print('\n-- core.match_order() je Payout-Zeile --')
    excluded_by_match_order = False
    for _, row in payout_rows.iterrows():
        match, issue = core.match_order(row, orders)
        if match is None:
            excluded_by_match_order = True
            print(f"  Transaktionsnummer={row['Transaktionsnummer']!r}: AUSGESCHLOSSEN durch core.match_order() — issue={issue!r}")
        else:
            print(f"  Transaktionsnummer={row['Transaktionsnummer']!r}: gematcht -> SKU={match['SKU']!r} Titel={match['Angebotstitel']!r}")
    if excluded_by_match_order:
        print('  => MINDESTENS EINE Payout-Zeile dieser Bestellung wird von core.match_order() ausgeschlossen.')
    else:
        print('  => Keine Payout-Zeile dieser Bestellung wird von core.match_order() ausgeschlossen.')

    mh_rows = master[(master['Bestellnummer'] == TARGET_ORDER) & (master['Partner'] == 'MH')]
    print(f'\n-- Im aktuellen MH-Abrechnungsbestand (core.load_master_data(), Partner==MH) für {TARGET_ORDER!r} --')
    print(f'Anzahl Positionen gesamt (alle Payouts): {len(mh_rows)}')
    for _, row in mh_rows.iterrows():
        same_payout = row['Auszahlung Nr.'] == TARGET_PAYOUT
        print(f"  Auszahlung Nr.={row['Auszahlung Nr.']!r} Transaktionsnummer={row['Transaktionsnummer']!r} "
              f"Art={row['Art']!r} SKU={row['SKU']!r} Erlös_Brutto={row['Erlös_Brutto']!r} "
              f"Prüfhinweis={row['Prüfhinweis']!r} -> gehört zu Payout {TARGET_PAYOUT!r}: {'JA' if same_payout else 'NEIN'}")

    target_payout_rows = mh_rows[mh_rows['Auszahlung Nr.'] == TARGET_PAYOUT]
    contribution = float(target_payout_rows['Erlös_Brutto'].sum()) if not target_payout_rows.empty else 0.0
    duplicate_lines = (target_payout_rows[target_payout_rows.duplicated(subset=['Transaktionsnummer'], keep=False)]
                        if not target_payout_rows.empty else target_payout_rows)
    print(f'\nIm MH-Bestand für genau Payout {TARGET_PAYOUT!r} enthalten: {"JA" if not target_payout_rows.empty else "NEIN"}')
    print(f'Anzahl unterschiedlicher Positionen (Transaktionsnummern) dieser Bestellung unter diesem Payout: '
          f'{target_payout_rows["Transaktionsnummer"].nunique()}')
    print(f'Dublette (dieselbe Transaktionsnummer mehrfach): {"JA" if not duplicate_lines.empty else "NEIN"}'
          + (f' -> betroffen: {sorted(duplicate_lines["Transaktionsnummer"].unique())}' if not duplicate_lines.empty else ''))
    print(f'Beitrag dieser Bestellung zum aktuellen MH-Partneranspruch (Summe Erlös_Brutto, Payout {TARGET_PAYOUT!r}): {contribution:.2f}')

    print()
    print('=' * 78)
    print('TEIL 2: Ausgezahlte Positionen ohne Bestellnummer (echte Auszahlung Nr.)')
    print('=' * 78)
    paid = payouts[payouts['Auszahlung Nr.'] != '']
    no_order = paid[paid['Bestellnummer'] == '']
    print(f'\nAnzahl: {len(no_order)}')
    for _, row in no_order.iterrows():
        typ = row['Typ'].strip().casefold()
        if 'bestellung' in typ:
            kind = 'Verkauf'
        elif 'einbehalten' in typ:
            kind = 'kein Verkauf (Einbehalt)'
        elif 'rückerstattung' in typ or 'rueckerstattung' in typ or 'erstattung' in typ:
            kind = 'kein Verkauf (Erstattung)'
        elif 'auszahlung' in typ:
            kind = 'kein Verkauf (Auszahlungs-Kontrollzeile)'
        elif any(word in typ for word in ('gebühr', 'fee', 'belastung')):
            kind = 'kein Verkauf (Gebühr)'
        else:
            kind = 'unklar / sonstige Bewegung — keine Partnerzuordnung ableitbar'
        print(f"  {dict(row)} -> {kind}")

    print()
    print('=' * 78)
    print('OFFENE UNMATCHED POSITIONEN GESAMT (core.match_order()==None, zur Einordnung)')
    print('=' * 78)
    still_unmatched = 0
    for _, row in paid.iterrows():
        match, _issue = core.match_order(row, orders)
        if match is None:
            still_unmatched += 1
    print(f'Gesamtzahl core.match_order()-unmatched ausgezahlter Positionen: {still_unmatched}')

    print()
    print('=' * 78)
    print('ZUSAMMENFASSUNG')
    print('=' * 78)
    print(f'{TARGET_ORDER}: im MH-Bestand enthalten {"JA" if not target_payout_rows.empty else "NEIN"}, '
          f'Betrag {contribution:.2f}, Dublette {"JA" if not duplicate_lines.empty else "NEIN"}')
    print(f'{len(no_order)} leere Payout-Zeilen (ohne Bestellnummer): siehe Klassifikation TEIL 2 oben je Position')
    print(f'noch ungeklärte MH-relevante Positionen (core.match_order()-unmatched, Partner nicht bestimmbar): {still_unmatched}')


if __name__ == '__main__':
    raise SystemExit(main())
