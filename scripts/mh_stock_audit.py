"""Read-only live audit of the MH order stock in source/orders.csv.

Purpose: find out whether the current production order stock (as loaded by
the app's own core.read_master()) actually contains ~504 MH orders, or
~242, or something else — and why. No fachlogik is changed or reimplemented:
Partner classification uses core.normalized_partner() exactly as the app
does today.

Read-only end to end: only core.read_master() (Supabase blob storage via
supabase_store.get) is used. No supabase_store.put/put_json, no
core.import_reports, no core.sync_status, no ebay_sync.run, no
position_workflow.confirm, no Lexware/invoice call anywhere in this file.

Usage:
    PAYMENT_BACKEND=supabase SUPABASE_ACCESS_TOKEN=... SUPABASE_PROJECT_REF=... \\
        python3 scripts/mh_stock_audit.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core
import supabase_store

PARTNER = 'MH'
QUANTITY_HEADER_HINTS = ('anzahl', 'menge', 'quantity', 'stückzahl', 'stueckzahl', 'verkaufte menge', 'qty')
DATE_HEADER_HINTS = ('verkauft am', 'bestelldatum', 'order date', 'sale date', 'datum', 'date')


def raw_prefix(sku):
    """Exactly the pre-fold value core.normalized_partner() computes right
    before deciding 'MH' vs. anything else — not a new rule, just exposing
    the intermediate step of the existing one."""
    normalized_sku = core.clean(sku).upper()
    return normalized_sku.split('/')[0].strip()


def find_populated_column(frame, hints, skip=()):
    """First column matching one of the header hints that actually has at
    least one non-empty value — never a same-name canonical column that
    exists but was left blank by canonicalize() (e.g. 'Datum' itself is a
    date-hint match by name but is exactly the empty canonical field this
    is trying to find a real replacement for)."""
    for column in frame.columns:
        if column in skip:
            continue
        normalized_name = core.normalized(column)
        for hint in hints:
            if core.normalized(hint) in normalized_name or normalized_name in core.normalized(hint):
                if frame[column].map(core.clean).astype(bool).any():
                    return column
                break
    return None


def to_float(value):
    text = core.clean(value)
    if not text:
        return None
    try:
        return float(str(core.parse_money(text)))
    except ValueError:
        try:
            return float(text.replace(',', '.'))
        except ValueError:
            return None


def main():
    supabase_store.preflight()
    orders = core.read_master(core.ORDERS_DB_PATH)
    total_rows = len(orders)
    print(f'Gesamtzahl Zeilen in source/orders.csv (alle Partner): {total_rows}')
    print(f'Gesamtzahl eindeutiger Bestellnummern (alle Partner): {orders["Bestellnummer"].nunique()}')
    print()

    orders = orders.copy()
    orders['_partner'] = orders['SKU'].map(core.normalized_partner)
    orders['_raw_prefix'] = orders['SKU'].map(raw_prefix)
    orders['_sku_clean'] = orders['SKU'].map(core.clean)

    quantity_col = find_populated_column(orders, QUANTITY_HEADER_HINTS)
    if orders['Datum'].map(core.clean).astype(bool).any():
        date_col = 'Datum'
    else:
        date_col = find_populated_column(orders, DATE_HEADER_HINTS, skip=('Datum',))
    print(f'Erkannte Mengenspalte: {quantity_col!r}' if quantity_col else 'Erkannte Mengenspalte: KEINE — Quelle enthält keine Mengeninformation.')
    print(f'Erkannte Datumsspalte: {date_col!r}' if date_col else 'Erkannte Datumsspalte: KEINE gefunden.')
    print()

    mh = orders[orders['_partner'] == PARTNER]
    unique_orders = mh['Bestellnummer'].nunique()
    positions = len(mh)
    print('=' * 78)
    print(f'A. MH laut aktuellem produktiven Bestellbestand: {unique_orders} eindeutige Bestellungen')
    print(f'B. MH-Positionen: {positions}')

    if quantity_col:
        quantities = mh[quantity_col].map(to_float)
        if quantities.isna().any():
            unparsed = int(quantities.isna().sum())
            print(f'C. MH-Stückzahl: nicht vollständig bestimmbar — {unparsed} von {positions} Positionen '
                  f'haben einen nicht auswertbaren Wert in Spalte {quantity_col!r}.')
        else:
            print(f'C. MH-Stückzahl: {quantities.sum():.0f}')
    else:
        print('C. MH-Stückzahl: nicht aus Quelle bestimmbar (keine Mengenspalte in source/orders.csv gefunden).')

    if date_col and mh[date_col].map(core.clean).astype(bool).any():
        dated = mh[mh[date_col].map(core.clean).astype(bool)]
        parsed = core.pd.to_datetime(dated[date_col].map(core.clean), dayfirst=True, errors='coerce')
        unparsed = int(parsed.isna().sum())
        if parsed.notna().any():
            print(f'D. Zeitraum: von {parsed.min():%d.%m.%Y} bis {parsed.max():%d.%m.%Y} '
                  f'(Spalte {date_col!r}, {len(dated)} von {positions} Positionen mit Datumswert'
                  + (f', davon {unparsed} nicht als Datum interpretierbar — als Text: '
                     f'{sorted(dated.loc[parsed.isna(), date_col].map(core.clean).unique())}' if unparsed else '') + ')')
        else:
            print(f'D. Zeitraum: nicht bestimmbar — Spalte {date_col!r} enthält Werte, aber keiner ist als Datum '
                  f'interpretierbar (Beispiele: {sorted(dated[date_col].map(core.clean).unique())[:5]}).')
    else:
        print('D. Zeitraum: nicht bestimmbar — keine auswertbare Datumsspalte mit Werten in source/orders.csv gefunden.')

    mh_substring_not_mh = orders[orders['_sku_clean'].str.upper().str.contains('MH', na=False) & (orders['_partner'] != PARTNER)]
    print(f'E. MH-haltige SKU, aber nicht als MH erkannt: {len(mh_substring_not_mh)} Position(en) '
          f'({mh_substring_not_mh["Bestellnummer"].nunique()} eindeutige Bestellung(en))')
    print('=' * 78)
    print()

    print('--- 4./5. Aufteilung nach tatsächlichem SKU-Präfix (unter Partner=MH) ---')
    groups = defaultdict(lambda: {'orders': set(), 'positions': 0, 'quantity': 0.0, 'quantity_known': True})
    for _, row in mh.iterrows():
        bucket = groups[row['_raw_prefix']]
        bucket['orders'].add(row['Bestellnummer'])
        bucket['positions'] += 1
        if quantity_col:
            value = to_float(row[quantity_col])
            if value is None:
                bucket['quantity_known'] = False
            else:
                bucket['quantity'] += value
    for prefix in sorted(groups):
        bucket = groups[prefix]
        quantity_text = f"{bucket['quantity']:.0f}" if quantity_col and bucket['quantity_known'] else 'nicht bestimmbar'
        print(f"  {prefix or '(leer)'}: {len(bucket['orders'])} Bestellungen, {bucket['positions']} Positionen, Stückzahl {quantity_text}")
    print()

    print(f'--- 6. Details: SKU enthält "MH", aber core.normalized_partner() != "MH" ({len(mh_substring_not_mh)}) ---')
    for _, row in mh_substring_not_mh.iterrows():
        print(f"  Bestellnummer={row['Bestellnummer']!r} SKU={row['SKU']!r} -> Partner={row['_partner']!r}")
    print()

    unexpected_mh = mh[~mh['_raw_prefix'].str.startswith('MH')]
    print(f'--- 7. Details: Partner=MH, aber roher Präfix beginnt NICHT mit "MH" '
          f'(klassifiziert über partners.json-Override, nicht über den Namens-Fallback) ({len(unexpected_mh)}) ---')
    for _, row in unexpected_mh.iterrows():
        print(f"  Bestellnummer={row['Bestellnummer']!r} SKU={row['SKU']!r} roher Präfix={row['_raw_prefix']!r}")
    print()

    empty_sku = orders[orders['_sku_clean'] == '']
    print(f'8. Bestellungen mit leerer/fehlender SKU (alle Partner, nicht nur MH): '
          f'{empty_sku["Bestellnummer"].nunique()} eindeutige Bestellungen, {len(empty_sku)} Positionen')
    print()

    print('F. Ursache der Differenz 504 vs. A: NOCH NICHT BELEGT — dieser Lauf liefert nur den tatsächlichen '
          'Ist-Stand (A-E) und die oben genannten Kandidaten (Zeitraum, leere SKU, nicht erkannte MH-Substrings, '
          'Override-Fälle). Ob das die Differenz zu 504 erklärt, muss anhand dieser Zahlen von Hand beurteilt werden.')


if __name__ == '__main__':
    raise SystemExit(main())
