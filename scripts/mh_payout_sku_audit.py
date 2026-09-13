"""Read-only audit: paid (payout-linked) positions vs. MH SKU classification.

Scope, per request: only positions with a real eBay Auszahlung Nr. (paid),
never open orders without a payout. SKU is resolved via the real
core.match_order() (the same order-matching function
core.load_master_data() itself uses) — not reimplemented — and classified
with the real, unmodified core.normalized_partner(). No quantity
evaluation, no changes.

Purely read-only: only core.read_master() (Supabase blob storage) is used.
No supabase_store.put/put_json, no core.import_reports, no
core.sync_status, no ebay_sync.run, no position_workflow.confirm, no
Lexware/invoice call anywhere in this file.

Usage:
    PAYMENT_BACKEND=supabase SUPABASE_ACCESS_TOKEN=... SUPABASE_PROJECT_REF=... \\
        python3 scripts/mh_payout_sku_audit.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core
import supabase_store

PARTNER = 'MH'


def raw_prefix(sku):
    """Exactly the pre-fold value core.normalized_partner() computes right
    before deciding 'MH' vs. anything else — not a new rule."""
    return core.clean(sku).upper().split('/')[0].strip()


def main():
    supabase_store.preflight()
    payouts = core.read_master(core.PAYOUTS_DB_PATH)
    orders = core.read_master(core.ORDERS_DB_PATH)
    paid = payouts[payouts['Auszahlung Nr.'] != '']
    print(f'Ausgezahlte Positionen gesamt (alle Partner, echte Auszahlung Nr.): {len(paid)}')

    unmatched = 0
    records = []
    for _, row in paid.iterrows():
        match, issue = core.match_order(row, orders)
        sku = match['SKU'] if match is not None else ''
        if match is None:
            unmatched += 1
        partner = core.normalized_partner(sku) if sku else ''
        records.append({
            'Bestellnummer': row['Bestellnummer'], 'SKU': sku, 'Partner': partner,
            'raw_prefix': raw_prefix(sku),
        })
    result = core.pd.DataFrame(records)
    print(f'Davon ohne eindeutige Bestellzuordnung (keine SKU ermittelbar, hier nicht weiter ausgewertet): {unmatched}')
    print()

    mh_prefix = result[result['raw_prefix'].str.startswith('MH')]
    as_mh = mh_prefix[mh_prefix['Partner'] == PARTNER]
    not_mh = mh_prefix[mh_prefix['Partner'] != PARTNER]

    print('=' * 78)
    print(f"Ausgezahlte Positionen mit SKU-Präfix 'MH*': {len(mh_prefix)}")
    print(f'  davon als MH zugeordnet: {len(as_mh)}')
    print(f'  davon NICHT als MH zugeordnet: {len(not_mh)}')
    print('=' * 78)
    print()

    print(f'--- Details: SKU-Präfix "MH*", aber Partner != MH ({len(not_mh)}) ---')
    for _, r in not_mh.iterrows():
        print(f"  Bestellnummer={r['Bestellnummer']!r} SKU={r['SKU']!r} Partner={r['Partner']!r}")
    print()

    override_mh = result[(result['Partner'] == PARTNER) & (~result['raw_prefix'].str.startswith('MH'))]
    print(f'--- Details: Partner=MH, aber Roh-SKU beginnt NICHT mit "MH" ({len(override_mh)}) ---')
    for _, r in override_mh.iterrows():
        print(f"  Bestellnummer={r['Bestellnummer']!r} SKU={r['SKU']!r}")


if __name__ == '__main__':
    raise SystemExit(main())
