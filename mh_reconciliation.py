"""Read-only 1:1 reconciliation: reviewed MH settlement vs raw eBay-API data.

Used only by the Trust/Risk diagnostic section (see trust_risk_ui.py).

The "reviewed" side calls the exact, unmodified production functions that
produce the "Einzelabrechnung herunterladen" export for a Gruppe-B partner
(core.load_master_data -> position_workflow.positions ->
studio_view.partner_rows), then applies the identical two selection steps
app.py itself applies before that download: partner_panel()'s
next_invoice = block[~block.reviewed_at.astype(bool)], and download()'s
exclusion of closed_at/API_Hold positions. This is deliberate: an earlier
version of this diagnostic reimplemented only core.load_master_data()'s
classification and skipped position_workflow/partner_rows entirely, which
does NOT reproduce what the download button actually exports whenever any
position has already been reviewed, paid, closed, or held — exactly the
gap this module now closes by calling the real functions instead of
re-deriving their logic (Group-B refund-linking in particular is exactly
the kind of settlement fachlogik this project's standing rule says not to
touch/reimplement).

This means the reviewed side does read state/settlement.sqlite3, via
core.ledger() inside position_workflow.positions()/studio_view.partner_rows()
— unavoidably, since position review/payment/closed status and invoice-
position links are only tracked there. That blob is itself fetched from
Supabase (supabase_store.get, same as every other read in this app), and
every call in this module only ever executes SELECT statements inside
core.ledger()'s context, so the existing, already-tested no-op-on-
unchanged-content guarantee (core.ledger() only calls supabase_store.put()
if the in-memory copy's bytes actually changed) means this stays read-only
in practice, not just in intent. core.sync_status() is the one exception
that genuinely can write even for an already-known payout — it is
deliberately never called here; see _empty_sync_status().

The raw side is unrelated to any of this: it is rebuilt straight from
Supabase Postgres (public.orders + public.payout_transactions) via the
app's existing supabase_store.read_only_sql() channel (a thin wrapper
around the same _request(readonly=True) get()/preflight() already use —
no separate credential), and classified with a faithful, read-only port of
core.load_master_data()'s Partner/Art logic (_classify_raw below) — it has
no review-workflow state of its own to consult, by design: it represents
"what eBay's own transactions say should exist", independent of what has
or hasn't been reviewed in the app yet.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime

import pandas as pd

import api_holds
import core
import position_workflow
import studio_view
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


def _classify_raw(payouts, orders):
    """Faithful, read-only port of core.load_master_data()'s row
    inclusion/exclusion and Partner-field computation (drops only the
    issue-string/group/title bookkeeping this comparison does not need).
    Used for the raw Postgres side only — see module docstring for why the
    reviewed side instead calls the real production functions directly."""
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


def _empty_sync_status():
    """Stand-in for core.sync_status(master) that never writes.

    core.sync_status() unconditionally opens a write transaction and can
    genuinely INSERT a new payout row or UPDATE its status even for an
    already-known payout (whenever its computed status differs from the
    stored one) — never guaranteed to be a no-op, so it must not be called
    from a diagnostic that promises zero writes. Its output (Auszahlung/
    Status/Entwurf/Sperre) only affects position_workflow.positions()'s
    'transferred'/'correction'/Bearbeitungsstatus-label computation, never
    which rows belong to Gruppe B's sales_b/refunds_b or next_invoice/
    download()'s closed_at+API_Hold exclusion (those read Prüfhinweis,
    Quellenpruefung, api_holds.mask, closed_at, paid_at, reviewed_at
    directly off the position — not Entwurf/Sperre) — so an empty
    placeholder yields identical row membership and amounts for the MH/
    Gruppe-B export this diagnostic reproduces."""
    return pd.DataFrame({'Auszahlung': pd.Series([], dtype=str), 'Status': pd.Series([], dtype=str),
                          'Entwurf': pd.Series([], dtype=object), 'Sperre': pd.Series([], dtype=object)})


def _exported_reviewed_positions():
    """The exact set 'Einzelabrechnung herunterladen' would export for MH
    right now: same production functions app.py itself calls, same two
    selection steps partner_panel()/download() apply. Read-only: only
    SELECTs run inside core.ledger() (see _empty_sync_status for why
    core.sync_status() itself is deliberately not called)."""
    master = core.load_master_data()
    business = position_workflow.positions(master, _empty_sync_status())
    if business.empty:
        return business
    partner_ready = studio_view.partner_rows(business)
    mh_block = partner_ready[(partner_ready['Gruppe'] == 'Gruppe B') & (partner_ready['Partner'] == PARTNER)]
    next_invoice = mh_block[~mh_block['reviewed_at'].astype(bool)]
    if next_invoice.empty:
        return next_invoice
    forbidden = set(business.loc[business['closed_at'].astype(bool) | api_holds.mask(business), 'position_key'])
    return next_invoice[~next_invoice['position_key'].isin(forbidden)]


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
    Supabase is unreachable.

    Two stages, reported separately: (1) the precondition — does the exact
    set 'Einzelabrechnung herunterladen' would export for MH right now
    (see _exported_reviewed_positions) already total 59 regular + 7
    refunds, before any raw-data comparison; (2) the 1:1 comparison of
    that exported set against the raw eBay-API transactions in Postgres.
    """
    supabase_store.preflight()
    exported = _exported_reviewed_positions()
    exported_scope = exported[exported['Auszahlung Nr.'].isin(PAYOUT_IDS)] if not exported.empty else exported
    exported_regular = exported_scope[exported_scope['Art'] == 'Bestellung'] if not exported_scope.empty else exported_scope
    exported_refunds = exported_scope[exported_scope['Art'] == 'Erstattung'] if not exported_scope.empty else exported_scope
    precondition = {
        'regular_count': len(exported_regular), 'refund_count': len(exported_refunds),
        'regular_ok': len(exported_regular) == EXPECTED_REGULAR, 'refund_ok': len(exported_refunds) == EXPECTED_REFUNDS,
    }

    raw_orders, raw_payouts = _fetch_raw_frames()
    raw_all = _classify_raw(raw_payouts, raw_orders)
    raw_mh = raw_all[(raw_all['Partner'] == PARTNER) & (raw_all['Auszahlung Nr.'].isin(PAYOUT_IDS))]

    regular = _compare(exported_regular, raw_mh[raw_mh['Art'] == 'Bestellung'])
    refunds = _compare(exported_refunds, raw_mh[raw_mh['Art'] == 'Erstattung'])
    ok = (
        precondition['regular_ok'] and precondition['refund_ok']
        and regular['matched'] == EXPECTED_REGULAR
        and not regular['missing'] and not regular['extra'] and not regular['amount_mismatches'] and not regular['duplicates']
        and refunds['matched'] == EXPECTED_REFUNDS
        and not refunds['missing'] and not refunds['extra'] and not refunds['amount_mismatches'] and not refunds['duplicates']
    )
    return {
        'ok': ok, 'precondition': precondition, 'regular': regular, 'refunds': refunds,
        'run_at': datetime.now().isoformat(timespec='seconds'),
    }
