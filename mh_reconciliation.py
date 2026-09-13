"""Read-only 1:1 reconciliation: reviewed MH settlement vs independent eBay data.

Used only by the Trust/Risk diagnostic section (see trust_risk_ui.py).

SEITE 1 — the exportable set. Calls the exact, unmodified production
functions that produce the "Einzelabrechnung herunterladen" export for a
Gruppe-B partner (core.load_master_data -> position_workflow.positions ->
studio_view.partner_rows), then applies the identical two selection steps
app.py itself applies before that download: partner_panel()'s
next_invoice = block[~block.reviewed_at.astype(bool)], and download()'s
exclusion of closed_at/API_Hold positions. This avoids reimplementing
Group-B refund-linking, which is exactly the settlement fachlogik this
project's standing rule says not to touch. It does read
state/settlement.sqlite3 (via core.ledger() inside position_workflow/
studio_view) — unavoidably, since review/payment/closed status is only
tracked there — but only ever executes SELECT statements in that context,
and core.sync_status() (the one function in this chain that can genuinely
write even for an already-known payout) is deliberately never called; see
_empty_sync_status().

SEITE 2 — independent confirmation, straight from eBay. A prior version of
this module compared Seite 1 against public.orders/public.payout_transactions
in Supabase Postgres. Analysis showed no code path in this application
writes to those tables — no import path, manual or API-sync, ever touches
them (they were an abandoned schema-only migration; see
supabase/MIGRATION_PLAN.md) — so a comparison against them proved nothing
about whether the exportable set matches eBay's own current data. This
version instead reads directly and only from eBay itself, via the existing
read-only ebay_readonly.Client (GET-only; its sole POST is OAuth refresh),
using the exact same calls ebay_sync.run() already makes for a known
payout (client.get('payout', pid), client.pages('transactions', ...,
{'filter': f'payoutId:{{{pid}}}'})), and reuses ebay_sync.validate_payout()
and ebay_sync.adapt() completely unmodified — the same functions the
production API-sync import path uses to turn eBay's raw JSON into
canonical rows (SALE/REFUND/NON_SALE_CHARGE typing, sign checks, gross
computation). adapt() also cross-matches against an existing CSV/orders
frame to skip transactions already known there (its job in production is
"what's new for the importer"); handing it two empty, correctly-shaped
frames here makes that matching step find zero candidates, so every
qualifying transaction is returned instead of being skipped — a pure
canonicalizing use of the same function, not a behaviour change to its
actual SALE/REFUND/NON_SALE_CHARGE/amount rules.

No SKU is independently re-derived from eBay: eBay's Finances transaction
data carries no SKU field, and re-deriving Partner/SKU assignment would be
exactly the "eigene Mapping-/Partnerregeln" this task says not to build.
Instead, the eBay-side confirmation is scoped to the Bestellnummern that
Seite 1 already recognises as MH (via the real production classification)
— it verifies "does eBay confirm every position Seite 1 is about to
export, exactly once, at the right amount", not "does eBay independently
agree these particular orders belong to MH". A wholly unknown order eBay
carries under one of the checked payouts, for a SKU/partner Seite 1 has
never seen, cannot be attributed to MH without SKU resolution and is
therefore out of this check's reach by design — see check()'s docstring.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime

import pandas as pd

import api_holds
import core
import ebay_sync
import position_workflow
import studio_view
import supabase_store
from ebay_readonly import Client, EbayError

PAYOUT_IDS = ('7710027297', '7712804241', '7714928937', '7718008497', '7725289401')
PARTNER = 'MH'
EXPECTED_REGULAR = 59
EXPECTED_REFUNDS = 7
_TYP_TO_ART = {'Bestellung': 'Bestellung', 'Rückerstattung': 'Erstattung'}  # 'Andere Gebühr' (NON_SALE_CHARGE) excluded — never MH


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


def _empty_master_frame():
    """Neutral 'nothing known yet' input for ebay_sync.adapt() — see
    _fetch_ebay_raw for why this turns adapt() into a pure canonicalizer."""
    return core.canonicalize(core.pd.DataFrame())


def _fetch_ebay_raw(client):
    """Independent read-only confirmation straight from eBay's own API —
    no Supabase, no Postgres, no local blob, on either side of this call.
    Returns (verified, error, frame). verified is False on ANY failure
    (network, auth, rate limit, incomplete pagination, or an integrity
    check failing) — this function never returns a partially-usable frame
    alongside verified=False, and the caller must never report green
    without verified=True."""
    payouts = {}
    transactions = {}
    try:
        for payout_id in PAYOUT_IDS:
            detail = client.get('payout', payout_id)
            movements = client.pages('transactions', 'transactions', {'filter': f'payoutId:{{{payout_id}}}'})
            ebay_sync.validate_payout(detail, movements)
            payouts[payout_id] = detail
            for row in movements['items']:
                transactions[ebay_sync.identity(row)] = row
        empty = _empty_master_frame()
        frame, _known, _ledger_only = ebay_sync.adapt(list(transactions.values()), payouts, empty, empty)
    except (EbayError, ValueError, KeyError, TypeError) as exc:
        message = client.redact(str(exc)) if isinstance(exc, (EbayError, ValueError)) else 'eBay-Antwort unvollständig oder unerwartet strukturiert.'
        return False, message, None
    return True, None, frame


def _to_comparable(frame):
    """ebay_sync.adapt()'s canonical output -> the shape _compare() expects.
    Drops NON_SALE_CHARGE ('Andere Gebühr') rows entirely: partnerlose
    Gebühren are never attributable to a partner and must not be counted
    as MH positions on either side."""
    columns = ['Auszahlung Nr.', 'Transaktionsnummer', 'Bestellnummer', 'Erlös_Brutto', 'Art']
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    kept = frame[frame['Typ'].isin(_TYP_TO_ART)].copy()
    if kept.empty:
        return pd.DataFrame(columns=columns)
    kept['Erlös_Brutto'] = kept['Betrag abzügl. Kosten'].map(core.parse_money).astype(float)
    kept['Art'] = kept['Typ'].map(_TYP_TO_ART)
    return kept[columns]


def _position_key(row):
    transaktionsnummer = str(row.get('Transaktionsnummer') or '').strip()
    if transaktionsnummer:
        return ('T', transaktionsnummer)
    return ('K', str(row.get('Bestellnummer') or ''), str(row.get('Auszahlung Nr.') or ''), row.get('Art'))


def _duplicate_keys(frame):
    counts = Counter(_position_key(row) for _, row in frame.iterrows())
    return {key for key, count in counts.items() if count > 1}


def _row_summary(row):
    return {'Bestellnummer': row.get('Bestellnummer'), 'SKU': row.get('SKU', ''),
            'Auszahlung Nr.': row.get('Auszahlung Nr.'), 'Betrag': row.get('Erlös_Brutto'),
            'Transaktionsnummer': row.get('Transaktionsnummer')}


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
        'total_amount_reviewed': float(reviewed['Erlös_Brutto'].sum()) if len(reviewed) else 0.0,
        'total_amount_raw': float(raw_truth['Erlös_Brutto'].sum()) if len(raw_truth) else 0.0,
        'missing': [_row_summary(raw_by_key[key]) for key in missing_keys],
        'extra': [_row_summary(reviewed_by_key[key]) for key in extra_keys],
        'amount_mismatches': [
            {**_row_summary(reviewed_by_key[key]), 'Betrag_eBay': raw_by_key[key]['Erlös_Brutto']}
            for key in sorted(amount_mismatch_keys)
        ],
        'duplicates': [{'Key': str(key)} for key in sorted(duplicate_keys)],
    }


def check(client=None):
    """Run the MH 01.09.-08.09.2026 reconciliation. Read-only throughout;
    raises supabase_store.StoreError (message already redacted of secrets)
    if Supabase is unreachable for Seite 1.

    Three stages, reported separately:
    (1) precondition — does the exact set 'Einzelabrechnung herunterladen'
        would export for MH right now already total 59 regular + 7 refunds,
        before any eBay comparison;
    (2) independent eBay confirmation — payout+transactions read live from
        eBay for exactly these payout IDs (verified=False on any failure,
        never a partial/fallback result);
    (3) the 1:1 comparison of the exportable set against eBay's own data,
        scoped to Bestellnummern Seite 1 already recognises as MH (no SKU
        re-derivation from eBay — see module docstring).

    'ok' is True only when precondition holds, eBay confirmation fully
    succeeded, and the comparison shows zero missing/extra/mismatch/
    duplicate on both regular and refund positions.
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
    run_at = datetime.now().isoformat(timespec='seconds')

    verified, error, ebay_frame = _fetch_ebay_raw(client or Client())
    if not verified:
        return {
            'ok': False, 'verified': False, 'error': error, 'precondition': precondition,
            'regular': None, 'refunds': None, 'run_at': run_at,
        }

    comparable = _to_comparable(ebay_frame)
    known_orders = set(exported_scope['Bestellnummer']) if not exported_scope.empty else set()
    ebay_scoped = comparable[comparable['Bestellnummer'].isin(known_orders) & comparable['Auszahlung Nr.'].isin(PAYOUT_IDS)]

    regular = _compare(exported_regular, ebay_scoped[ebay_scoped['Art'] == 'Bestellung'])
    refunds = _compare(exported_refunds, ebay_scoped[ebay_scoped['Art'] == 'Erstattung'])
    ok = (
        precondition['regular_ok'] and precondition['refund_ok']
        and regular['matched'] == EXPECTED_REGULAR
        and not regular['missing'] and not regular['extra'] and not regular['amount_mismatches'] and not regular['duplicates']
        and refunds['matched'] == EXPECTED_REFUNDS
        and not refunds['missing'] and not refunds['extra'] and not refunds['amount_mismatches'] and not refunds['duplicates']
    )
    return {
        'ok': ok, 'verified': True, 'error': None, 'precondition': precondition,
        'regular': regular, 'refunds': refunds, 'ebay_total_positions': len(comparable),
        'run_at': run_at,
    }
