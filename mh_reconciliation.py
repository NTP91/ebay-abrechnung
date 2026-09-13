"""Read-only 1:1 reconciliation: reviewed MH settlement vs raw eBay-API data.

Used only by the Trust/Risk diagnostic section (see trust_risk_ui.py). Never
writes: only core.read_master() (Supabase blob storage, source/orders.csv +
source/payouts.csv) and supabase_store.read_only_sql() (the app's existing
Management API read-only SQL channel — same SUPABASE_ACCESS_TOKEN/
SUPABASE_PROJECT_REF the app already uses everywhere, no separate
credential) are used. Deliberately never calls core.ledger(),
payout_reconciliation.gates() or api_holds.annotate(): those only ever add
extra text/flags to positions that already exist and never change which
rows exist or their amounts, so skipping them changes nothing about this
comparison — and it means this diagnostic never touches
state/settlement.sqlite3. Both sides of the comparison come from genuine,
already-imported Supabase-native data: source/orders.csv + source/payouts.csv
(Supabase blob storage) for the reviewed side, public.orders +
public.payout_transactions (Supabase Postgres) for the raw side.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime

import pandas as pd

import core
import supabase_store
from payout_structure import validate as validate_children

PAYOUT_IDS = ('7710027297', '7712804241', '7714928937', '7718008497', '7725289401')
PARTNER = 'MH'
EXPECTED_REGULAR = 59
EXPECTED_REFUNDS = 7
ALLOWED_FINANCE_TYPES = {'SALE', 'REFUND', 'NON_SALE_CHARGE'}


def _amount(value):
    return '' if value is None else format(value, 'f') if hasattr(value, 'as_tuple') else str(value)


def _report_date(value):
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')).strftime('%d.%m.%Y')
    except ValueError:
        return text


def _raw_orders_frame(rows):
    """Mirrors scripts/rebuild_local_from_supabase.py:order_frame(); duplicated
    here (not imported) so the deployed app never depends on the scripts/
    directory being importable at runtime."""
    records = []
    for row in rows:
        records.append({
            'Bestellnummer': row.get('bestellnummer') or '',
            'Transaktionsnummer': row.get('transaktionsnummer') or '',
            'Artikelnummer': row.get('artikelnummer') or '',
            'SKU': row.get('sku') or '',
            'Angebotstitel': row.get('angebotstitel') or '',
        })
    return pd.DataFrame(records)


def _raw_payouts_frame(rows):
    """Mirrors scripts/rebuild_local_from_supabase.py:payout_frame(); see note above."""
    by_id = {int(row['id']): row for row in rows}
    records = []
    for row in rows:
        parent = by_id.get(row.get('parent_transaction_id')) if row.get('is_child_reference') else row
        raw = (parent or {}).get('raw_row') or {}
        native = raw.get('transactionType')
        if native not in ALLOWED_FINANCE_TYPES:
            continue
        child = bool(row.get('is_child_reference'))
        records.append({
            'Auszahlung Nr.': str(row.get('auszahlung_nr') or ''),
            'Bestellnummer': row.get('bestellnummer') or '',
            'Transaktionsnummer': row.get('transaktionsnummer') or '',
            'Artikelnummer': row.get('artikelnummer') or '',
            'Typ': row.get('typ') or '',
            'Datum': _report_date(row.get('datum')),
            'Betrag abzügl. Kosten': '' if child else _amount(row.get('betrag_abzueglich_kosten')),
            'Transaktionsbetrag (inkl. Kosten)': '' if child else _amount(row.get('transaktionsbetrag_inkl_kosten')),
            'Zwischensumme Artikel': '' if child else _amount(row.get('zwischensumme_artikel')),
            'Verpackung und Versand': '' if child else _amount(row.get('verpackung_und_versand')),
            'API_Artikelreferenzen': json.dumps(raw.get('orderLineItems') or [], ensure_ascii=False),
        })
    return pd.DataFrame(records)


def _fetch_raw_frames():
    """Read-only: public.orders + public.payout_transactions via the app's
    existing Supabase connection, restricted to the 5 payout IDs."""
    payout_id_list = ','.join(f"'{pid}'" for pid in PAYOUT_IDS)
    transactions = supabase_store.read_only_sql(
        'select id,auszahlung_nr,bestellnummer,transaktionsnummer,artikelnummer,typ,datum,'
        'betrag_abzueglich_kosten,zwischensumme_artikel,verpackung_und_versand,'
        'transaktionsbetrag_inkl_kosten,is_child_reference,parent_transaction_id,raw_row '
        f'from public.payout_transactions where auszahlung_nr in ({payout_id_list}) order by id'
    )
    transaktionsnummern = sorted({str(row['transaktionsnummer']) for row in transactions if row.get('transaktionsnummer')})
    bestellnummern = sorted({str(row['bestellnummer']) for row in transactions if row.get('bestellnummer')})
    filters = []
    if transaktionsnummern:
        filters.append('transaktionsnummer in (' + ','.join(f"'{t}'" for t in transaktionsnummern) + ')')
    if bestellnummern:
        filters.append('bestellnummer in (' + ','.join(f"'{b}'" for b in bestellnummern) + ')')
    where_clause = ' or '.join(filters) if filters else 'false'
    orders_rows = supabase_store.read_only_sql(
        'select bestellnummer,transaktionsnummer,artikelnummer,sku,angebotstitel '
        f'from public.orders where {where_clause} order by id'
    )
    return _raw_orders_frame(orders_rows), _raw_payouts_frame(transactions)


def _classify(payouts, orders):
    """Exact port of core.load_master_data()'s row inclusion/exclusion and
    Partner-field computation (drops only the issue-string/group/title
    bookkeeping this comparison does not need, and the two sqlite-backed
    overlays — see module docstring). Every continue/append condition below
    matches core.load_master_data() line for line, so a row is Partner=='MH'
    here if and only if it would be in the production ledger."""
    payouts = payouts[payouts['Auszahlung Nr.'] != '']
    child_indices = validate_children(payouts)
    processed = []
    for index, row in payouts.iterrows():
        if index in child_indices:
            continue
        if row['Typ'].strip().casefold() == 'auszahlung':
            continue
        if row['Typ'].strip().casefold() == 'einbehalten':
            continue
        amount = float(core.parse_money(row['Betrag abzügl. Kosten']))
        order_id = row['Bestellnummer']
        fee = not order_id and any(word in row['Typ'].lower() for word in ('gebühr', 'fee', 'belastung'))
        sku = ''
        if not fee:
            match, _issue = core.match_order(row, orders)
            if match is not None:
                sku = match['SKU']
                if not sku:
                    continue  # historical order predates partner SKUs; never assignable
        partner = '' if fee else core.normalized_partner(sku)
        processed.append({
            'Auszahlung Nr.': row['Auszahlung Nr.'], 'Transaktionsnummer': row['Transaktionsnummer'],
            'Bestellnummer': order_id, 'Partner': partner, 'SKU': sku,
            'Erlös_Brutto': amount,
            'Art': 'Gebühr' if fee else 'Erstattung' if amount < 0 else 'Bestellung',
        })
    return pd.DataFrame(processed)


def _position_key(row):
    transaktionsnummer = str(row.get('Transaktionsnummer') or '').strip()
    if transaktionsnummer:
        return ('T', transaktionsnummer)
    return ('K', str(row.get('Bestellnummer') or ''), str(row.get('Auszahlung Nr.') or ''), row.get('Art'))


def _duplicate_keys(frame):
    counts = Counter(_position_key(row) for _, row in frame.iterrows())
    return {key for key, count in counts.items() if count > 1}


def _row_summary(row):
    return {'Bestellnummer': row.get('Bestellnummer'), 'Auszahlung Nr.': row.get('Auszahlung Nr.'),
            'Betrag': row.get('Erlös_Brutto'), 'Transaktionsnummer': row.get('Transaktionsnummer')}


def _compare(reviewed, raw_truth):
    reviewed_by_key = {_position_key(row): row for _, row in reviewed.iterrows()}
    raw_by_key = {_position_key(row): row for _, row in raw_truth.iterrows()}
    matched_keys = set(reviewed_by_key) & set(raw_by_key)
    missing_keys = sorted(set(raw_by_key) - set(reviewed_by_key))
    extra_keys = sorted(set(reviewed_by_key) - set(raw_by_key))
    amount_mismatch_keys = [
        key for key in matched_keys
        if abs(float(reviewed_by_key[key]['Erlös_Brutto']) - float(raw_by_key[key]['Erlös_Brutto'])) > 0.005
    ]
    duplicate_keys = _duplicate_keys(reviewed) | _duplicate_keys(raw_truth)
    return {
        'total_reviewed': len(reviewed), 'total_raw': len(raw_truth), 'matched': len(matched_keys),
        'missing': [_row_summary(raw_by_key[key]) for key in missing_keys],
        'extra': [_row_summary(reviewed_by_key[key]) for key in extra_keys],
        'amount_mismatches': [
            {**_row_summary(reviewed_by_key[key]), 'Betrag_Rohdaten': raw_by_key[key]['Erlös_Brutto']}
            for key in sorted(amount_mismatch_keys)
        ],
        'duplicates': [{'Key': str(key)} for key in sorted(duplicate_keys)],
    }


def check():
    """Run the MH 01.09.-08.09.2026 reconciliation. Read-only; raises
    supabase_store.StoreError (message already redacted of secrets) if
    Supabase is unreachable."""
    supabase_store.preflight()
    reviewed_payouts = core.read_master(core.PAYOUTS_DB_PATH)
    reviewed_orders = core.read_master(core.ORDERS_DB_PATH)
    reviewed_all = _classify(reviewed_payouts, reviewed_orders)
    raw_orders, raw_payouts = _fetch_raw_frames()
    raw_all = _classify(raw_payouts, raw_orders)

    reviewed_mh = reviewed_all[(reviewed_all['Partner'] == PARTNER) & (reviewed_all['Auszahlung Nr.'].isin(PAYOUT_IDS))]
    raw_mh = raw_all[(raw_all['Partner'] == PARTNER) & (raw_all['Auszahlung Nr.'].isin(PAYOUT_IDS))]

    regular = _compare(reviewed_mh[reviewed_mh['Art'] == 'Bestellung'], raw_mh[raw_mh['Art'] == 'Bestellung'])
    refunds = _compare(reviewed_mh[reviewed_mh['Art'] == 'Erstattung'], raw_mh[raw_mh['Art'] == 'Erstattung'])
    ok = (
        regular['total_reviewed'] == EXPECTED_REGULAR and regular['matched'] == EXPECTED_REGULAR
        and not regular['missing'] and not regular['extra'] and not regular['amount_mismatches'] and not regular['duplicates']
        and refunds['total_reviewed'] == EXPECTED_REFUNDS and refunds['matched'] == EXPECTED_REFUNDS
        and not refunds['missing'] and not refunds['extra'] and not refunds['amount_mismatches'] and not refunds['duplicates']
    )
    return {'ok': ok, 'regular': regular, 'refunds': refunds, 'run_at': datetime.now().isoformat(timespec='seconds')}
