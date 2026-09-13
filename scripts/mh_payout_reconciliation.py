"""Read-only 1:1 reconciliation: reviewed MH settlement vs raw eBay-API data.

Compares the currently reviewed MH settlement positions (as produced by
core.load_master_data(), i.e. exactly what the app itself treats as the
authoritative, already-imported ledger) against a freshly rebuilt view of
the raw eBay-API transactions held in Supabase Postgres
(public.orders / public.payout_transactions), for a fixed set of payout
IDs. It never writes anything: only supabase_store.get/get_json (via
core.load_master_data) and the Supabase Management API's read-only SQL
endpoint (via rebuild_local_from_supabase.Management.read) are used.
core.read_master is monkeypatched for the duration of one call only, to
let core.load_master_data() (the app's own, already-tested classification
logic: Partner via SKU prefix, Gruppe A/B, Einbehalten/Auszahlung/Gebühr
exclusion) run against the freshly rebuilt raw frames instead of the
currently stored source/orders.csv + source/payouts.csv. The monkeypatch
is restored in a finally block even on error.

Requires SUPABASE_ACCESS_TOKEN and SUPABASE_PROJECT_REF (Management API,
read-only SQL endpoint) and PAYMENT_BACKEND=supabase, SUPABASE-Zugriff
über supabase_store wie im Live-Betrieb.

Usage:
    PAYMENT_BACKEND=supabase SUPABASE_ACCESS_TOKEN=... SUPABASE_PROJECT_REF=... \\
        python3 scripts/mh_payout_reconciliation.py
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core
import supabase_store
from rebuild_local_from_supabase import Management, order_frame, payout_frame

PAYOUT_IDS = ['7710027297', '7712804241', '7714928937', '7718008497', '7725289401']
EXPECTED_REGULAR = 59
EXPECTED_REFUNDS = 7
PARTNER = 'MH'


def fail(message: str) -> None:
    print('ABBRUCH:', message)
    raise SystemExit(1)


def fetch_raw_frames(management: Management):
    payout_id_list = ','.join(f"'{pid}'" for pid in PAYOUT_IDS)
    transactions = management.read(
        'select id,auszahlung_nr,bestellnummer,transaktionsnummer,artikelnummer,typ,datum,'
        'betrag_abzueglich_kosten,zwischensumme_artikel,verpackung_und_versand,'
        'transaktionsbetrag_inkl_kosten,referenznummer,auszahlungsstatus,is_child_reference,'
        'parent_transaction_id,raw_row from public.payout_transactions '
        f'where auszahlung_nr in ({payout_id_list}) order by id'
    )
    if not transactions:
        fail(f'Keine payout_transactions-Zeilen für die 5 Payouts gefunden ({PAYOUT_IDS}).')
    transaktionsnummern = sorted({str(row['transaktionsnummer']) for row in transactions if row.get('transaktionsnummer')})
    bestellnummern = sorted({str(row['bestellnummer']) for row in transactions if row.get('bestellnummer')})
    order_filters = []
    if transaktionsnummern:
        order_filters.append('transaktionsnummer in (' + ','.join(f"'{t}'" for t in transaktionsnummern) + ')')
    if bestellnummern:
        order_filters.append('bestellnummer in (' + ','.join(f"'{b}'" for b in bestellnummern) + ')')
    where_clause = ' or '.join(order_filters) if order_filters else 'false'
    orders_rows = management.read(
        'select bestellnummer,transaktionsnummer,artikelnummer,sku,angebotstitel,raw_row '
        f'from public.orders where {where_clause} order by id'
    )
    state_rows = management.read('select watermark,payouts,transactions from public.ebay_sync_state where id=1')
    sync_state = state_rows[0] if state_rows else {}
    order_data = order_frame(orders_rows)
    payout_data = payout_frame(transactions, order_data, sync_state)
    holds = management.read(
        'select observed_at,order_id,transaction_id,transaction_type,transaction_status,transaction_date,'
        'booking_entry,amount,payout_id,"references",transaction_memo,raw_observation '
        'from public.api_hold_evidence order by id'
    )
    api_identity_by_transaktionsnummer = {}
    for row in transactions:
        raw = row.get('raw_row') or {}
        identity = raw.get('api_identity')
        if identity and row.get('transaktionsnummer'):
            api_identity_by_transaktionsnummer[str(row['transaktionsnummer'])] = identity
    return order_data, payout_data, holds, api_identity_by_transaktionsnummer


def load_mh(reader_orders, reader_payouts):
    """Run core.load_master_data() with core.read_master temporarily patched."""
    original = core.read_master

    def patched(path):
        name = Path(path).name
        return core.canonicalize(reader_orders if name == 'Master_Orders.csv' else reader_payouts)

    core.read_master = patched
    try:
        return core.load_master_data()
    finally:
        core.read_master = original


def position_key(row):
    transaktionsnummer = str(row.get('Transaktionsnummer') or '').strip()
    if transaktionsnummer:
        return ('T', transaktionsnummer)
    return ('K', str(row.get('Bestellnummer') or ''), str(row.get('Artikelnummer') or ''),
            str(row.get('Auszahlung Nr.') or ''), row.get('Art'))


def duplicates(frame):
    counts = Counter(position_key(row) for _, row in frame.iterrows())
    return {key: count for key, count in counts.items() if count > 1}


def compare_side(label, reviewed, raw_truth):
    reviewed_keys = {position_key(row): row for _, row in reviewed.iterrows()}
    raw_keys = {position_key(row): row for _, row in raw_truth.iterrows()}
    missing_in_reviewed = sorted(set(raw_keys) - set(reviewed_keys))
    missing_in_raw = sorted(set(reviewed_keys) - set(raw_keys))
    matched = set(reviewed_keys) & set(raw_keys)
    field_mismatches = []
    for key in sorted(matched):
        left, right = reviewed_keys[key], raw_keys[key]
        for field in ('Bestellnummer', 'SKU', 'Auszahlung Nr.', 'Erlös_Brutto'):
            left_value, right_value = left.get(field), right.get(field)
            if isinstance(left_value, float) or isinstance(right_value, float):
                if round(float(left_value or 0), 2) != round(float(right_value or 0), 2):
                    field_mismatches.append((key, field, left_value, right_value))
            elif str(left_value) != str(right_value):
                field_mismatches.append((key, field, left_value, right_value))
    print(f'--- {label} ---')
    print(f'Abrechnung (reviewed): {len(reviewed)} Positionen')
    print(f'Rohdaten (raw_truth):  {len(raw_truth)} Positionen')
    print(f'Gematcht:              {len(matched)}')
    print(f'Fehlend in Abrechnung (nur in Rohdaten): {len(missing_in_reviewed)}')
    for key in missing_in_reviewed:
        row = raw_keys[key]
        print('  FEHLT:', dict(Bestellnummer=row.get('Bestellnummer'), SKU=row.get('SKU'),
                                Auszahlung=row.get('Auszahlung Nr.'), Betrag=row.get('Erlös_Brutto'), Key=key))
    print(f'Zusätzlich in Abrechnung (nur dort, nicht in Rohdaten): {len(missing_in_raw)}')
    for key in missing_in_raw:
        row = reviewed_keys[key]
        print('  ZUSAETZLICH:', dict(Bestellnummer=row.get('Bestellnummer'), SKU=row.get('SKU'),
                                      Auszahlung=row.get('Auszahlung Nr.'), Betrag=row.get('Erlös_Brutto'), Key=key))
    print(f'Feldabweichungen bei gematchten Positionen: {len(field_mismatches)}')
    for key, field, left_value, right_value in field_mismatches:
        print(f'  ABWEICHUNG {key} Feld={field}: Abrechnung={left_value!r} Rohdaten={right_value!r}')
    reviewed_dupes = duplicates(reviewed)
    raw_dupes = duplicates(raw_truth)
    print(f'Dubletten in Abrechnung: {len(reviewed_dupes)}')
    for key, count in reviewed_dupes.items():
        print(f'  DUBLETTE (Abrechnung) {key}: {count}x')
    print(f'Dubletten in Rohdaten: {len(raw_dupes)}')
    for key, count in raw_dupes.items():
        print(f'  DUBLETTE (Rohdaten) {key}: {count}x')
    print()
    return len(reviewed), len(matched), len(missing_in_reviewed), len(missing_in_raw), len(field_mismatches), len(reviewed_dupes), len(raw_dupes)


def main() -> int:
    supabase_store.preflight()  # fail-closed, identisch zum Live-Betrieb; kein Fallback
    management = Management()  # nutzt ausschließlich den read-only SQL-Endpunkt

    print('Lade aktuell geprüften MH-Abrechnungsbestand (state/settlement.sqlite3 + source/*.csv)...')
    reviewed_all = core.load_master_data()

    print('Lade Rohtransaktionen aus Supabase-Postgres (public.orders / public.payout_transactions)...')
    raw_orders, raw_payouts, holds, api_identity_by_transaktionsnummer = fetch_raw_frames(management)
    raw_truth_all = load_mh(raw_orders, raw_payouts)

    reviewed_mh = reviewed_all[(reviewed_all['Partner'] == PARTNER) & (reviewed_all['Auszahlung Nr.'].isin(PAYOUT_IDS))]
    raw_truth_mh = raw_truth_all[(raw_truth_all['Partner'] == PARTNER) & (raw_truth_all['Auszahlung Nr.'].isin(PAYOUT_IDS))]

    unexpected_art = sorted((set(reviewed_mh['Art']) | set(raw_truth_mh['Art'])) - {'Bestellung', 'Erstattung'})
    if unexpected_art:
        print('WARNUNG: unerwartete Art-Werte bei Partner MH (sollten nie auftreten, da Einbehalte/Gebühren '
              'strukturell vor der Partnerzuordnung ausgeschlossen werden):', unexpected_art)

    reviewed_regular = reviewed_mh[reviewed_mh['Art'] == 'Bestellung']
    reviewed_refunds = reviewed_mh[reviewed_mh['Art'] == 'Erstattung']
    raw_regular = raw_truth_mh[raw_truth_mh['Art'] == 'Bestellung']
    raw_refunds = raw_truth_mh[raw_truth_mh['Art'] == 'Erstattung']

    print()
    print('=' * 78)
    print(f'Erwartung: {EXPECTED_REGULAR} reguläre MH-Positionen, {EXPECTED_REFUNDS} Refunds, Payouts={PAYOUT_IDS}')
    print('=' * 78)
    print()

    regular_stats = compare_side('Reguläre MH-Positionen', reviewed_regular, raw_regular)
    refund_stats = compare_side('Refunds', reviewed_refunds, raw_refunds)

    dispute_orders_in_scope = {
        row['order_id'] for row in holds
        if row.get('transaction_type') == 'DISPUTE' and str(row.get('order_id')) in set(reviewed_mh['Bestellnummer'])
    }
    print('--- Einbehalte (zur Kontrolle, dürfen NICHT als reguläre Position zählen) ---')
    print(f'Anzahl DISPUTE-Einbehalte, die eine Bestellnummer dieser 5 Payouts betreffen: {len(dispute_orders_in_scope)}')
    leaked = reviewed_mh[(reviewed_mh['Art'] != 'Bestellung') & (reviewed_mh['Art'] != 'Erstattung')]
    print(f'Einbehalte/Gebühren, die trotzdem als MH-Position auftauchen: {len(leaked)} (muss 0 sein)')
    print()

    n_reviewed_reg, n_matched_reg, n_missing_reg, n_extra_reg, n_mismatch_reg, n_dup_reviewed_reg, n_dup_raw_reg = regular_stats
    n_reviewed_ref, n_matched_ref, n_missing_ref, n_extra_ref, n_mismatch_ref, n_dup_reviewed_ref, n_dup_raw_ref = refund_stats

    total_dupes = n_dup_reviewed_reg + n_dup_raw_reg + n_dup_reviewed_ref + n_dup_raw_ref
    all_clean = (
        n_reviewed_reg == EXPECTED_REGULAR == n_matched_reg and n_missing_reg == 0 and n_extra_reg == 0 and n_mismatch_reg == 0
        and n_reviewed_ref == EXPECTED_REFUNDS == n_matched_ref and n_missing_ref == 0 and n_extra_ref == 0 and n_mismatch_ref == 0
        and total_dupes == 0 and len(leaked) == 0
    )

    print('=' * 78)
    print('ERGEBNIS')
    print('=' * 78)
    print(f'Regulär: {n_matched_reg}/{EXPECTED_REGULAR} gematcht (tatsächlich in Abrechnung: {n_reviewed_reg}, in Rohdaten: {n_matched_reg + n_missing_reg})')
    print(f'Refunds: {n_matched_ref}/{EXPECTED_REFUNDS} gematcht (tatsächlich in Abrechnung: {n_reviewed_ref}, in Rohdaten: {n_matched_ref + n_missing_ref})')
    print(f'Fehlend gesamt: {n_missing_reg + n_missing_ref}')
    print(f'Zusätzlich gesamt: {n_extra_reg + n_extra_ref}')
    print(f'Dubletten gesamt: {total_dupes}')
    print(f'Feldabweichungen gesamt: {n_mismatch_reg + n_mismatch_ref}')
    print()
    if all_clean:
        print(f'{EXPECTED_REGULAR}/{EXPECTED_REGULAR} regulär gematcht, {EXPECTED_REFUNDS}/{EXPECTED_REFUNDS} '
              'Refunds gematcht, 0 fehlend, 0 zusätzlich, 0 Dubletten.')
    else:
        print('ABWEICHUNG(EN) GEFUNDEN — siehe Detailzeilen oben (FEHLT/ZUSAETZLICH/ABWEICHUNG/DUBLETTE).')
    return 0 if all_clean else 1


if __name__ == '__main__':
    raise SystemExit(main())
