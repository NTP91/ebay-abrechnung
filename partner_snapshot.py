"""Immutable final per-round, per-partner settlement snapshots (2026-003+).

A partner-round snapshot is the frozen, unchangeable "final Einzelabrechnung"
for exactly one (round_id, partner) pair - never the whole round. Creating it
(finalize()) is only allowed once the round's own Sunday-23:59-Berlin cut has
passed (a still-'laufend' round can only ever produce a live, non-stored
interim_export()). Once created, a snapshot is immutable: re-finalizing the
same pair is a true no-op that returns the exact same stored record and file
bytes, never a recomputation - the same hash-locked insert-once pattern
group_b_rounds._insert_round() already uses for whole rounds.

Deliberately independent of the historical Gruppe-B/Lexware model, exactly
like round_planner: no RE0090 lookup, no Patrick-collects-then-invoices-
Evelyn export mode. The already-corrected 3-tab Excel logic in
partner_export.py (Rechnung / Gutschriften / HistorischeGutschriften) is
reused unchanged - this module only decides WHICH rows go into it and WHEN
the result may be frozen.
"""
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import core
import partner_export
import position_workflow

BERLIN = ZoneInfo('Europe/Berlin')


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS partner_round_snapshots (
        round_id TEXT NOT NULL, partner TEXT NOT NULL, partner_group TEXT NOT NULL,
        window_start TEXT NOT NULL, window_end TEXT NOT NULL,
        position_keys TEXT NOT NULL, position_count INTEGER NOT NULL,
        regular_claim TEXT NOT NULL, refunds_total TEXT NOT NULL, final_amount TEXT NOT NULL,
        payouts TEXT NOT NULL, rate TEXT NOT NULL,
        snapshot_hash TEXT NOT NULL UNIQUE, finalized_at TEXT NOT NULL,
        file_bytes BLOB NOT NULL, file_hash TEXT NOT NULL,
        line_items TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY(round_id, partner))''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(partner_round_snapshots)')}
    if 'line_items' not in columns:
        db.execute("ALTER TABLE partner_round_snapshots ADD COLUMN line_items TEXT NOT NULL DEFAULT '[]'")


def _stable(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def is_locked(db, round_id, partner):
    """True once (round_id, partner) has an immutable final snapshot - the
    partner-level lock assign_late_payouts() must respect: that exact round
    may never gain another position for that partner again, regardless of
    whether other partners in the same round are locked or not."""
    initialize(db)
    return db.execute('SELECT 1 FROM partner_round_snapshots WHERE round_id=? AND partner=?',
                       (round_id, partner)).fetchone() is not None


def _round_window(db, round_id):
    row = db.execute("SELECT snapshot FROM group_b_rounds WHERE id=? AND source_kind='neutral_weekly'",
                      (round_id,)).fetchone()
    if not row:
        raise ValueError(f'{round_id} ist keine bekannte neutrale Wochenrunde.')
    snapshot = json.loads(row['snapshot'])
    return snapshot['window_start'], snapshot['window_end']


def _partner_round_rows(business, assigned_keys, partner):
    """The partner's current live position bestand for one round: every
    'Bestellung' position group_b_round_positions has assigned it in that
    round, plus every refund row core.refund_links() ties to one of those
    sales (regardless of the refund's own payout date/round) - the same
    sale-to-refund stitching group_b_rounds.overview() already relies on, so
    Tab 2/3 of the export are populated correctly instead of always empty."""
    sale_index = business.index[business.position_key.isin(assigned_keys)
                                 & (business.Partner == partner) & (business.Art == 'Bestellung')]
    links = core.refund_links(business)
    sale_set = set(sale_index)
    refund_index = [refund for refund, sale in links.items() if sale in sale_set]
    return business.loc[sorted(sale_set | set(refund_index))]


def _line_items(rows, model):
    """The frozen per-position Tab-1 (Rechnung/sale) expectations a later
    partner-invoice check must reconcile against - order, SKU, net/gross,
    discount, rate - independent of any live data from then on. Mirrors
    partner_invoices.expected_statement()'s own item shape (same downstream
    consumers can reuse the same field names), restricted to the sale-only
    subset of `rows` since model['Rechnung'] only ever contains those, in the
    same relative order."""
    sale_rows = rows[rows.Art == 'Bestellung']
    items = []
    for (_, row), item in zip(sale_rows.iterrows(), model['Rechnung']):
        items.append(dict(order=item['order'], sku=str(row.SKU), article=item['article'], quantity='1',
                           net=str(item['net']), gross=str(item['gross']), discount=str(item['discount']),
                           rate=str((model['rate'] * 100).normalize()), payout=item['payout_id']))
    if len(items) != len(sale_rows):
        raise ValueError('Rechnungspositionen konnten nicht eindeutig zugeordnet werden.')
    return items


def interim_export(round_id, partner, business=None, payouts=None, orders=None):
    """Always-live Zwischenstand: never stored, never locks anything, safe to
    call any number of times while the round is still 'laufend' (or even
    after its cut, before the first real finalize()). Returns None for a
    partner with 0 current positions in this round ("nichts erforderlich" -
    no empty file needed)."""
    business = position_workflow.positions() if business is None else business
    payouts = core.read_master(core.PAYOUTS_DB_PATH) if payouts is None else payouts
    orders = core.read_master(core.ORDERS_DB_PATH) if orders is None else orders
    with core.ledger() as db:
        assigned_keys = {r[0] for r in db.execute(
            'SELECT position_key FROM group_b_round_positions WHERE round_id=?', (round_id,))}
    rows = _partner_round_rows(business, assigned_keys, partner)
    if rows.empty:
        return None
    return partner_export.export_partner_excel(rows, payouts, orders, statement_type='partner')


def finalize(round_id, partner, now=None, business=None, payouts=None, orders=None):
    """First-ever final download for (round_id, partner). Freezes the
    partner's current round membership - Bestellung positions plus their
    linked refunds - as an immutable snapshot (record + generated Excel
    bytes). Requires the round's own cut to already have passed: a
    still-'laufend' round can only produce interim_export(), never this.

    True no-op on repeat: an already-finalized pair returns its exact stored
    record and bytes unchanged, without touching prepare_partner_export or
    export_partner_excel again - so later corrections to live order/payout
    data can never retroactively change an already-frozen partner claim.

    Returns (record: dict, created: bool).
    """
    import group_b_rounds

    now_berlin = (now or datetime.now(BERLIN)).astimezone(BERLIN)
    business = position_workflow.positions() if business is None else business
    payouts = core.read_master(core.PAYOUTS_DB_PATH) if payouts is None else payouts
    orders = core.read_master(core.ORDERS_DB_PATH) if orders is None else orders

    with core.ledger() as db:
        group_b_rounds.initialize(db)
        initialize(db)
        window_start, window_end = _round_window(db, round_id)
        if now_berlin < datetime.fromisoformat(window_end):
            raise ValueError(f'{round_id} ist noch laufend; nur ein Zwischenstand ist moeglich, kein finaler Download.')
        existing = db.execute('SELECT * FROM partner_round_snapshots WHERE round_id=? AND partner=?',
                               (round_id, partner)).fetchone()
        if existing:
            return dict(existing), False

        assigned_keys = {r[0] for r in db.execute(
            'SELECT position_key FROM group_b_round_positions WHERE round_id=?', (round_id,))}
        rows = _partner_round_rows(business, assigned_keys, partner)
        if rows.empty:
            raise ValueError(f'{partner} hat 0 Positionen in {round_id}; kein finaler Snapshot noetig '
                              f'("0 Positionen - nichts erforderlich").')

        model = partner_export.prepare_partner_export(rows, payouts, orders, statement_type='partner')
        file_bytes = partner_export.export_partner_excel(rows, payouts, orders, statement_type='partner')
        regular_claim = model['totals']['Rechnung']['gross']
        refunds_total = model['totals']['Gutschriften']['gross']
        final_amount = regular_claim + refunds_total
        position_keys = sorted(rows.position_key)
        payout_ids = sorted(model['payouts'])
        line_items = _line_items(rows, model)
        payload = dict(round_id=round_id, partner=partner, position_keys=position_keys,
                        regular_claim=str(regular_claim), refunds_total=str(refunds_total),
                        final_amount=str(final_amount), payouts=payout_ids, rate=str(model['rate']),
                        line_items=line_items)
        digest = _hash(_stable(payload))
        file_digest = hashlib.sha256(file_bytes).hexdigest()

        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT * FROM partner_round_snapshots WHERE round_id=? AND partner=?',
                               (round_id, partner)).fetchone()
        if existing:
            db.rollback()
            return dict(existing), False
        finalized_at = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        db.execute('''INSERT INTO partner_round_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
            round_id, partner, rows.iloc[0].Gruppe, window_start, window_end,
            json.dumps(position_keys, ensure_ascii=False), len(position_keys),
            str(regular_claim), str(refunds_total), str(final_amount),
            json.dumps(payout_ids, ensure_ascii=False), str(model['rate']),
            digest, finalized_at, file_bytes, file_digest,
            json.dumps(line_items, ensure_ascii=False)))
        db.commit()
        record = dict(db.execute('SELECT * FROM partner_round_snapshots WHERE round_id=? AND partner=?',
                                  (round_id, partner)).fetchone())
    return record, True


def final_file(round_id, partner):
    """The exact stored bytes for an already-finalized pair, or None if it
    was never finalized - repeated downloads always return this, never a
    fresh recomputation."""
    with core.ledger() as db:
        initialize(db)
        row = db.execute('SELECT file_bytes FROM partner_round_snapshots WHERE round_id=? AND partner=?',
                          (round_id, partner)).fetchone()
        return row['file_bytes'] if row else None
