"""Read-only classification of negative eBay positions without a
Bestellnummer/SKU for the 5 tracked MH payouts (see
mh_reconciliation.PAYOUT_IDS).

Context: mh_reconciliation.check()'s Seite-2 eBay comparison
(_to_comparable) deliberately drops every 'Andere Gebühr' (NON_SALE_CHARGE)
row before comparing, and further scopes the remaining rows to
Bestellnummern already known as MH (ebay_scoped in check()). A
NON_SALE_CHARGE transaction never carries an orderId (see ebay_sync.adapt():
'order' is only ever taken from transaction.get('orderId', ''), which eBay
never sets on a non-sale charge), while SALE/REFUND always require one
(adapt() raises otherwise) — so Bestellnummer=='' is the correct,
structural way to isolate these rows. SKU cannot be used for this on the
live-eBay side: mh_reconciliation._fetch_ebay_raw() calls
ebay_sync.adapt(rows, payouts, empty, empty) with an *empty* orders frame
(see its own docstring — it only needs adapt() as a canonicalizer, not for
SKU resolution), so SKU is structurally '' for every row on that path, not
just for charges; filtering on SKU there would also catch genuine REFUND
rows and misrepresent them as "ohne SKU" positions. Such Bestellnummer-less
rows structurally cannot appear in mh_reconciliation.check()'s missing/
extra lists — they are excluded before the comparison even runs, not
silently mismatched. This script surfaces exactly those rows for the 5
tracked payouts and classifies each with the *unmodified* production fee
rule from core.load_master_data():

    fee = not order_id and any(word in Typ.lower()
                                for word in ('gebühr', 'fee', 'belastung'))

No partner is invented here for any row. If fee is True for a row, the
unmodified production pipeline (core.load_master_data()) already sets
Partner='', Gruppe='Gebühren' for it — i.e. that row is excluded from every
partner's billing, MH included, by a rule already in production, not by a
special case added in this script. Any row that does NOT satisfy this rule
is flagged explicitly instead of being silently treated as a fee.

Two independent angles are checked, since it is not certain from which side
the 4 positions were observed:

  Angle A — live eBay pull: mh_reconciliation._fetch_ebay_raw() (already
  committed, already tested), i.e. the same client.get('payout')/
  client.pages('transactions')/ebay_sync.validate_payout()/ebay_sync.adapt()
  calls the real API-sync path uses, called here read-only for exactly the
  5 tracked payout IDs.

  Angle B — already-imported settlement data: core.read_master(
  core.PAYOUTS_DB_PATH) for the same 5 payout IDs, in case these positions
  were already synced into source/payouts.csv before this check runs.

Purely read-only: only core.read_master()/mh_reconciliation._fetch_ebay_raw()/
ebay_readonly.Client are used. No supabase_store.put/put_json, no
core.import_reports, no core.sync_status, no ebay_sync.run, no
position_workflow.confirm anywhere in this file.

Usage:
    PAYMENT_BACKEND=supabase SUPABASE_ACCESS_TOKEN=... SUPABASE_PROJECT_REF=... \\
        python3 scripts/mh_ebay_nonsku_charges_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core
import mh_reconciliation
import supabase_store
from ebay_readonly import Client


def _is_fee(typ, order_id):
    return not order_id and any(word in typ.lower() for word in ('gebühr', 'fee', 'belastung'))


def _print_row(label, row):
    order_id = row.get('Bestellnummer') or ''
    tx = row.get('Transaktionsnummer') or ''
    ref = row.get('Referenznummer', '') or ''
    title = row.get('Angebotstitel', '') or ''
    print(f"  [{label}] Auszahlung Nr.={row.get('Auszahlung Nr.')!r} Datum={row.get('Datum')!r} "
          f"Typ={row.get('Typ')!r} Bestellnummer={order_id or '(leer)'!r} "
          f"Transaktionsnummer={tx or '(leer)'!r} Referenznummer={ref or '(leer)'!r} "
          f"Angebotstitel={title or '(leer)'!r} "
          f"Betrag abzügl. Kosten={row.get('Betrag abzügl. Kosten')!r}")


def _report(label, rows, order_id_key):
    total = 0.0
    any_unexpected = False
    for _, row in rows.iterrows():
        _print_row(label, row)
        order_id = row.get(order_id_key) or ''
        fee = _is_fee(row['Typ'], order_id)
        amount = float(core.parse_money(row['Betrag abzügl. Kosten']))
        total += amount
        print(f"      -> fee (core.load_master_data()-Regel)={fee}  Betrag={amount:.2f}")
        if not fee:
            any_unexpected = True
            print('      !! WEICHT VON DER ERWARTUNG AB: erfüllt NICHT die bestehende Gebühren-Regel — '
                  'manuelle Prüfung nötig, hier wird kein Partner erfunden.')
    return total, any_unexpected


def main():
    supabase_store.preflight()

    print('=' * 78)
    print('ANGLE A: Live eBay-Abfrage für die 5 überwachten Payouts (read-only)')
    print('=' * 78)
    verified, error, ebay_frame = mh_reconciliation._fetch_ebay_raw(Client())
    ebay_count = None
    ebay_unexpected = False
    if not verified:
        print(f'Unabhängige eBay-Prüfung nicht möglich: {error}')
    elif ebay_frame is None:
        # ebay_sync.adapt() returns None (not an empty frame) when there is
        # nothing at all to report (see its final line) — zero SALE/REFUND/
        # NON_SALE_CHARGE rows for these payouts, i.e. zero candidates here.
        ebay_count = 0
        print('Keine SALE/REFUND/NON_SALE_CHARGE-Bewegungen im live eBay-Datensatz für diese Payouts.')
    else:
        scoped = ebay_frame[ebay_frame['Auszahlung Nr.'].isin(mh_reconciliation.PAYOUT_IDS)]
        negative = scoped[scoped['Betrag abzügl. Kosten'].map(core.parse_money).astype(float) < 0]
        # Bestellnummer=='', not SKU=='', is the correct filter here — see module docstring.
        ebay_candidates = negative[negative['Bestellnummer'] == '']
        ebay_count = len(ebay_candidates)
        print(f'Negative Positionen ohne Bestellnummer (von {len(scoped)} Positionen gesamt in diesen Payouts): '
              f'{ebay_count}')
        total, ebay_unexpected = _report('eBay live', ebay_candidates, 'Bestellnummer')
        print(f'Summe dieser Positionen (eBay live): {total:.2f}')

    print()
    print('=' * 78)
    print('ANGLE B: Bereits importierte Payout-Zeilen (source/payouts.csv) für dieselben Payouts')
    print('=' * 78)
    payouts = core.read_master(core.PAYOUTS_DB_PATH)
    scoped_csv = payouts[payouts['Auszahlung Nr.'].isin(mh_reconciliation.PAYOUT_IDS)]
    negative_csv = scoped_csv[scoped_csv['Betrag abzügl. Kosten'].map(core.parse_money).astype(float) < 0]
    csv_candidates = negative_csv[negative_csv['Bestellnummer'] == '']
    print(f'Negative Positionen ohne Bestellnummer (von {len(scoped_csv)} Zeilen gesamt in diesen Payouts): '
          f'{len(csv_candidates)}')
    total_csv, csv_unexpected = _report('payouts.csv', csv_candidates, 'Bestellnummer')
    print(f'Summe dieser Positionen (payouts.csv): {total_csv:.2f}')

    print()
    print('=' * 78)
    print('ZUSAMMENFASSUNG')
    print('=' * 78)
    print(f'Angle A (live eBay): {ebay_count if ebay_count is not None else "nicht verifiziert"} '
          f'negative Position(en) ohne Bestellnummer')
    print(f'Angle B (payouts.csv): {len(csv_candidates)} negative Position(en) ohne Bestellnummer')
    print("Fachliche Einordnung: core.load_master_data() klassifiziert jede Zeile ohne Bestellnummer, deren Typ "
          "'gebühr'/'fee'/'belastung' enthält, unverändert als fee=True -> Partner='', Gruppe='Gebühren'. Eine "
          'solche Zeile erhält production-seitig nie Partner="MH" und fließt nie in die MH-Abrechnung ein.')
    if ebay_count or len(csv_candidates):
        if not ebay_unexpected and not csv_unexpected:
            print('Alle oben gefundenen Positionen erfüllen fee=True: partnerlose Gebühren/Charges, korrekt '
                  'außerhalb der MH-Abrechnung. ok=False in mh_reconciliation.check() ist damit für diese '
                  'Positionen ein reiner Prüfhinweis, kein Fehler der MH-Abrechnung.')
        else:
            print('MINDESTENS EINE Position erfüllt fee=True NICHT (siehe "WEICHT VON DER ERWARTUNG AB" oben) — '
                  'für diese Position ist die Einordnung "partnerlose Gebühr" NICHT bestätigt und muss manuell '
                  'geprüft werden, statt sie hier automatisch als Gebühr zu werten.')
    else:
        print('Keine negativen Positionen ohne SKU/Bestellnummer in diesen 5 Payouts gefunden (weder live noch '
              'in payouts.csv) — die "4 Positionen" aus der Beobachtung des Nutzers sind mit dieser Abgrenzung '
              'nicht reproduzierbar; ggf. andere Payout-IDs oder ein anderes Kriterium gemeint.')


if __name__ == '__main__':
    raise SystemExit(main())
