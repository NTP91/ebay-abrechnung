"""Durable API evidence overlay; never rewrites source rows or invoice snapshots."""
import json
import os
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from filelock import FileLock
from atomic_io import replace_file

FILE = 'Settlement_API_Holds.json'


def load(directory):
    import supabase_store
    if supabase_store.enabled():
        import core
        copies=[]
        saved, _ = supabase_store.get_json('state/holds.json', default=None)
        if saved: copies.append(saved)
        with core.ledger() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='api_hold_evidence'").fetchone():
                record=db.execute('SELECT document FROM api_hold_evidence WHERE id=1').fetchone()
                if record: copies.append(json.loads(record[0]))
        if not copies: return {'version':0,'observations':[]}
        newest=[d for d in copies if d['version']==max(c['version'] for c in copies)]
        if any(d!=newest[0] for d in newest): raise ValueError('API-Hold-Nachweise widersprüchlich; Abrechnung gesperrt.')
        return newest[0]
    directory = Path(directory)
    copies = []
    if (directory / FILE).exists():
        copies.append(json.loads((directory / FILE).read_text(encoding='utf-8')))
    database = directory / 'Settlement_State.sqlite3'
    if database.exists():
        with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='api_hold_evidence'").fetchone():
                record = db.execute('SELECT document FROM api_hold_evidence WHERE id=1').fetchone()
                if record:
                    copies.append(json.loads(record[0]))
    if not copies:
        return {'version': 0, 'observations': []}
    for document in copies:
        if not isinstance(document.get('version'), int) or not isinstance(document.get('observations'), list):
            raise ValueError('API-Hold-Nachweis beschädigt; Abrechnung gesperrt.')
    newest = [d for d in copies if d['version'] == max(c['version'] for c in copies)]
    if any(d != newest[0] for d in newest):
        raise ValueError('API-Hold-Nachweise widersprüchlich; Abrechnung gesperrt.')
    return newest[0]


def stamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo else None
    except ValueError:
        return None


def amount(row):
    try:
        value = Decimal(str(row['amount']['value']))
        return value if value.is_finite() and value > 0 and row['amount']['currency'] == 'EUR' else None
    except (KeyError, TypeError, InvalidOperation):
        return None


def ingest(directory, snapshot):
    """Union observations: missing rows, failed refreshes and old caches cannot release holds."""
    if snapshot.get('account') != 'ebay_durchstart' or not stamp(snapshot.get('fetched_at')):
        raise ValueError('API-Hold-Nachweis ohne Account oder gültigen Abrufzeitpunkt.')
    observations = []
    for name, resource in snapshot.get('resources', {}).items():
        if not (name in ('transactions', 'reference_transactions') or name.startswith('order_')) or not resource.get('available'):
            continue
        for row in resource.get('data', {}).get('items', []):
            if row.get('orderId') and row.get('transactionId') and row.get('transactionType'):
                # Only financial evidence, never credentials or buyer profiles.
                fields = ('orderId', 'transactionId', 'transactionType', 'transactionStatus',
                          'transactionDate', 'bookingEntry', 'amount', 'payoutId', 'references', 'transactionMemo')
                observations.append({'at': snapshot['fetched_at'], 'transaction': {k: row[k] for k in fields if k in row}})
    import supabase_store
    if supabase_store.enabled():
        import core
        document=load(directory)
        encode=lambda row: json.dumps(row,ensure_ascii=False,sort_keys=True)
        merged={encode(row):row for row in document['observations']+observations}
        if set(merged)=={encode(row) for row in document['observations']}: return document
        document={'version':document['version']+1,'observations':[merged[k] for k in sorted(merged)]}
        text=json.dumps(document,ensure_ascii=False)
        with core.ledger() as db:
            db.execute('CREATE TABLE IF NOT EXISTS api_hold_evidence (id INTEGER PRIMARY KEY, document TEXT NOT NULL)')
            db.execute('INSERT OR REPLACE INTO api_hold_evidence VALUES(1,?)',(text,));db.commit()
        _, version=supabase_store.get('state/holds.json',required=False)
        supabase_store.put_json('state/holds.json',document,version)
        return document
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with FileLock(str(directory / 'Master_Payouts.csv') + '.lock'), FileLock(str(directory / 'Master_Orders.csv') + '.lock'), FileLock(str(directory / FILE) + '.lock'):
        document = load(directory)
        encode = lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True)
        merged = {encode(row): row for row in document['observations'] + observations}
        if set(merged) == {encode(row) for row in document['observations']}:
            return document
        document = {'version': document['version'] + 1, 'observations': [merged[k] for k in sorted(merged)]}
        text = json.dumps(document, ensure_ascii=False)
        with sqlite3.connect(directory / 'Settlement_State.sqlite3') as db:
            db.execute('CREATE TABLE IF NOT EXISTS api_hold_evidence (id INTEGER PRIMARY KEY, document TEXT NOT NULL)')
            db.execute('INSERT OR REPLACE INTO api_hold_evidence VALUES(1,?)', (text,))
            db.commit()
        temporary = (directory / FILE).with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        replace_file(temporary, directory / FILE)
        return document


def _resolve(document):
    """One pass over the evidence: which documented holds are still active,
    and which carry a genuine, corroborating release booking.

    There is exactly ONE release rule in this module - active() and released()
    are the two sides of that same decision, never two heuristics. A hold that
    merely stopped being observed (deleted, re-imported, expired evidence) is
    in NEITHER result: absence of a hold record is not a release.

    Returns (held, freed): held is orderId -> [hold rows] (active()'s own
    result), freed is orderId -> release timestamp (ISO string).
    """
    observations = document['observations']
    holds = {}
    for observation in observations:
        row = observation['transaction']
        if row.get('transactionType') == 'REFUND':
            continue
        retro = row.get('bookingEntry') == 'DEBIT' and row.get('transactionType') == 'DISPUTE' and (
            row['transactionId'].startswith(('RETRO_HOLD-', 'DISPUTE_HOLD-')))
        sale = row.get('transactionType') == 'SALE' and row.get('transactionStatus') == 'FUNDS_ON_HOLD'
        if retro or sale:
            identity = (row['transactionId'], row['transactionType'])
            holds.setdefault(identity, []).append(observation)
    held, freed = {}, {}
    for identity, evidence in holds.items():
        latest = max(evidence, key=lambda o: stamp(o['at']))
        row = latest['transaction']
        refs = {(r.get('referenceType'), r.get('referenceId')) for r in row.get('references', []) if r.get('referenceId')}
        released_at = None
        for observation in observations:
            credit = observation['transaction']
            if (credit.get('orderId') != row['orderId'] or amount(row) is None or amount(credit) != amount(row)
                    or credit.get('bookingEntry') != 'CREDIT' or credit.get('transactionStatus') != 'PAYOUT'
                    or not credit.get('payoutId')):
                continue
            # A documented transition of the SAME held sale; never a different ordinary sale.
            same_sale = (row['transactionType'] == 'SALE' and (credit['transactionId'], credit['transactionType']) == identity
                         and stamp(observation['at']) > stamp(latest['at']))
            credit_refs = {(r.get('referenceType'), r.get('referenceId')) for r in credit.get('references', [])}
            # Explicit counter-entry, uniquely tied to one hold, later in time.
            counterpart = (row['transactionType'] == 'DISPUTE' and credit.get('transactionType') == 'DISPUTE'
                           and bool(refs & credit_refs) and stamp(row.get('transactionDate')) is not None
                           and stamp(credit.get('transactionDate')) is not None
                           and stamp(credit['transactionDate']) > stamp(row['transactionDate'])
                           and sum(bool(refs & {(r.get('referenceType'), r.get('referenceId')) for r in h[-1]['transaction'].get('references', [])})
                                   for h in holds.values()) == 1)
            if same_sale or counterpart:
                booked = credit.get('transactionDate')
                released_at = booked if stamp(booked) is not None else observation['at']
                break
        if released_at is None:
            held.setdefault(row['orderId'], []).append(row)
        else:
            previous = freed.get(row['orderId'])
            if previous is None or stamp(released_at) < stamp(previous):
                freed[row['orderId']] = released_at
    return held, freed


def active(document):
    return _resolve(document)[0]


def released(document):
    """orderId -> release timestamp, for every hold with a real, documented
    release booking per _resolve()'s single rule. An order that still has ANY
    active hold stays out: partial/ambiguous evidence is never a release."""
    held, freed = _resolve(document)
    return {order: at for order, at in freed.items() if order not in held}


def mask(rows):
    """Compatible with existing fixtures and unannotated source data."""
    import pandas as pd
    return rows['API_Hold'].astype(bool) if 'API_Hold' in rows else pd.Series(False, index=rows.index)


def annotate(rows, directory):
    if rows.empty:
        return rows
    held = active(load(directory))
    rows = rows.copy()
    rows['API_Hold'] = (rows.Art == 'Bestellung') & rows.Bestellnummer.isin(held)
    rows['API_Hold_Hinweis'] = rows.apply(lambda row: (
        'API-Einbehalt: ' + ', '.join(h['transactionId'] for h in held[row.Bestellnummer])
        if row.API_Hold else ''), axis=1)
    return rows
