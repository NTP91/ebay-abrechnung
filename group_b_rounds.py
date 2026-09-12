"""Immutable Group-B settlement rounds and read-only reserve views."""
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd

import api_holds
import core
import position_workflow
from partner_export import prepare_partner_export


ROUND_ONE = 'GB-2026-001'
ROUND_TWO = 'GB-2026-002'


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS group_b_rounds (
        id TEXT PRIMARY KEY, year INTEGER NOT NULL, sequence INTEGER NOT NULL,
        source_kind TEXT NOT NULL, evelyn_invoice_id TEXT,
        evelyn_document_number TEXT, evelyn_amount TEXT NOT NULL,
        snapshot_hash TEXT NOT NULL UNIQUE, snapshot TEXT NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(year, sequence))''')
    db.execute('''CREATE TABLE IF NOT EXISTS group_b_round_positions (
        position_key TEXT PRIMARY KEY, round_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('evelyn_invoice','zero_pair','hold_reserve')),
        source TEXT NOT NULL, FOREIGN KEY(round_id) REFERENCES group_b_rounds(id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS group_b_round_refunds (
        refund_key TEXT PRIMARY KEY, origin_position_key TEXT NOT NULL,
        origin_round_id TEXT NOT NULL,
        settlement_round_id TEXT, source TEXT NOT NULL,
        FOREIGN KEY(origin_position_key) REFERENCES group_b_round_positions(position_key),
        FOREIGN KEY(origin_round_id) REFERENCES group_b_rounds(id),
        FOREIGN KEY(settlement_round_id) REFERENCES group_b_rounds(id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS partner_invoice_rounds (
        invoice_id TEXT NOT NULL, round_id TEXT NOT NULL,
        PRIMARY KEY(invoice_id, round_id),
        FOREIGN KEY(invoice_id) REFERENCES partner_invoices(id),
        FOREIGN KEY(round_id) REFERENCES group_b_rounds(id))''')


def next_round_id(db, year):
    """Return the next globally unique annual Group-B sequence."""
    row = db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM group_b_rounds WHERE year=?',
                     (int(year),)).fetchone()
    return f'GB-{int(year):04d}-{int(row[0]):03d}'


def _stable(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _invoice_description(row):
    return f"eBay-Bestellnummer: {row['Bestellnummer']}\nSKU: {row.SKU}"


def _insert_round(db, round_id, sequence, source_kind, invoice_id, document_number,
                  amount, snapshot, positions, roles):
    serialized = snapshot if isinstance(snapshot, str) else _stable(snapshot)
    digest = _hash(serialized)
    existing = db.execute('SELECT * FROM group_b_rounds WHERE id=?', (round_id,)).fetchone()
    if existing:
        if existing['snapshot_hash'] != digest or Decimal(existing['evelyn_amount']) != Decimal(amount):
            raise ValueError(f'{round_id} ist bereits mit einem anderen unveränderlichen Snapshot gespeichert.')
        return False
    now = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
    db.execute('INSERT INTO group_b_rounds VALUES(?,?,?,?,?,?,?,?,?,?)',
               (round_id, 2026, sequence, source_kind, invoice_id, document_number,
                str(amount), digest, serialized, now))
    for _, row in positions.iterrows():
        role = roles.get(row.position_key, 'evelyn_invoice')
        db.execute('INSERT INTO group_b_round_positions VALUES(?,?,?,?)',
                   (row.position_key, round_id, role, position_workflow.source_snapshot(row)))
    return True


def _re0090(db, business, invoices):
    matches = [(invoice_id, item) for invoice_id, item in invoices.items()
               if item.get('Belegnummer') == 'RE0090' and not item.get('discarded')]
    if len(matches) != 1:
        raise ValueError('RE0090-Snapshot ist nicht eindeutig vorhanden.')
    invoice_id, history = matches[0]
    rows = list(db.execute('SELECT id,snapshot FROM payouts WHERE invoice_id=? ORDER BY id', (invoice_id,)))
    snapshots = {row['snapshot'] for row in rows if row['snapshot']}
    if len(snapshots) != 1:
        raise ValueError('RE0090 besitzt keinen eindeutigen unveränderlichen Snapshot.')
    raw_snapshot = snapshots.pop()
    payload = json.loads(raw_snapshot)
    descriptions = [item.get('description') for item in payload.get('lineItems', [])]
    if len(descriptions) != len(set(descriptions)):
        raise ValueError('RE0090 enthält doppelte Positionsidentitäten.')
    candidates = business[(business.Gruppe == 'Gruppe B') & (business.Art == 'Bestellung')].copy()
    candidates['_description'] = candidates.apply(_invoice_description, axis=1)
    positions = candidates[candidates._description.isin(descriptions)].copy()
    if len(positions) != len(descriptions) or set(positions._description) != set(descriptions):
        raise ValueError('RE0090-Positionen lassen sich nicht eindeutig auf den Bestand abbilden.')
    return invoice_id, history, raw_snapshot, positions.drop(columns=['_description'])


def bootstrap(business, current_ready, invoices):
    """Create the two reconstructed rounds once; never enlarge them later."""
    if business.empty:
        return
    with core.ledger() as db:
        db.execute('BEGIN IMMEDIATE')
        invoice_id, history, snapshot, r1 = _re0090(db, business, invoices)
        r1_keys = set(r1.position_key)
        r1_holds = business[(business.Gruppe == 'Gruppe B') & (business.Art == 'Bestellung')
                            & business['Auszahlung Nr.'].astype(str).isin(set(history['Payouts']))
                            & ~business.position_key.isin(r1_keys) & api_holds.mask(business)]
        r1_positions = pd.concat([r1, r1_holds]).drop_duplicates('position_key')
        r1_roles = {key: 'hold_reserve' for key in r1_holds.position_key}
        _insert_round(db, ROUND_ONE, 1, 'immutable_evelyn_invoice', invoice_id, 'RE0090',
                      Decimal(str(history['Betrag'])), snapshot, r1_positions, r1_roles)

        has_r2 = db.execute('SELECT 1 FROM group_b_rounds WHERE id=?', (ROUND_TWO,)).fetchone()
        if not has_r2:
            if current_ready.empty:
                raise ValueError('GB-2026-002 benötigt den aktuellen abrechnungsfähigen Evelyn-Snapshot.')
            invoice_model = prepare_partner_export(current_ready, statement_type='group_b_evelyn')
            payout_ids = sorted(current_ready['Auszahlung Nr.'].astype(str).unique())
            assigned = {row['position_key'] for row in db.execute('SELECT position_key FROM group_b_round_positions')}
            links = core.refund_links(business)
            supplemental = []
            for refund_index, sale_index in links.items():
                sale, refund = business.loc[sale_index], business.loc[refund_index]
                if (sale.position_key not in assigned and sale.Gruppe == 'Gruppe B'
                        and str(sale['Auszahlung Nr.']) in payout_ids
                        and not sale['Prüfhinweis'] and not sale.Quellenpruefung
                        and not bool(api_holds.mask(business.loc[[sale_index, refund_index]]).any())
                        and Decimal(str(sale['Erlös_Brutto'])) + Decimal(str(refund['Erlös_Brutto'])) == 0):
                    supplemental.append(sale_index)
            extras = business.loc[sorted(set(supplemental))].copy() if supplemental else business.iloc[0:0].copy()
            r2_holds = business[(business.Gruppe == 'Gruppe B') & (business.Art == 'Bestellung')
                                & business['Auszahlung Nr.'].astype(str).isin(payout_ids)
                                & api_holds.mask(business)]
            positions = pd.concat([current_ready, extras, r2_holds]).drop_duplicates('position_key')
            roles = {key: 'zero_pair' for key in extras.position_key}
            roles.update({key: 'hold_reserve' for key in r2_holds.position_key})
            round_snapshot = {
                'source': 'current_eligible_snapshot',
                'payouts': payout_ids,
                'evelyn_amount': str(invoice_model['totals']['Rechnung']['gross']),
                'invoice_positions': sorted(position_workflow.source_snapshot(row)
                                            for _, row in current_ready.iterrows()),
            }
            _insert_round(db, ROUND_TWO, 2, 'current_eligible_snapshot', None, None,
                          invoice_model['totals']['Rechnung']['gross'], round_snapshot, positions, roles)

        assignments = {row['position_key']: row['round_id']
                       for row in db.execute('SELECT position_key,round_id FROM group_b_round_positions')}
        links = core.refund_links(business)
        r2_snapshot = json.loads(db.execute('SELECT snapshot FROM group_b_rounds WHERE id=?', (ROUND_TWO,)).fetchone()[0])
        r2_payouts = set(r2_snapshot.get('payouts', []))
        for refund_index, sale_index in links.items():
            sale, refund = business.loc[sale_index], business.loc[refund_index]
            origin = assignments.get(sale.position_key)
            if not origin:
                continue
            held = bool(api_holds.mask(business.loc[[sale_index, refund_index]]).any())
            settlement = (ROUND_TWO if not held and str(refund['Auszahlung Nr.']) in r2_payouts
                          and not refund.reviewed_at and not refund.paid_at and not refund.closed_at else None)
            source = position_workflow.source_snapshot(refund)
            existing = db.execute('SELECT * FROM group_b_round_refunds WHERE refund_key=?',
                                  (refund.position_key,)).fetchone()
            if existing and (existing['origin_position_key'] != sale.position_key
                             or existing['origin_round_id'] != origin or existing['source'] != source):
                raise ValueError('Refund-Rundenzuordnung widerspricht dem gespeicherten Ursprung.')
            if not existing:
                db.execute('INSERT INTO group_b_round_refunds VALUES(?,?,?,?,?)',
                           (refund.position_key, sale.position_key, origin, settlement, source))

        # Existing partner invoices gain relations only; their immutable records
        # and original files are deliberately not changed.
        db.execute('''INSERT OR IGNORE INTO partner_invoice_rounds(invoice_id,round_id)
            SELECT DISTINCT p.invoice_id, r.round_id
            FROM partner_invoice_positions p
            JOIN group_b_round_positions r ON r.position_key=p.position_key''')
        db.execute('''INSERT OR IGNORE INTO partner_invoice_rounds(invoice_id,round_id)
            SELECT DISTINCT p.invoice_id, r.origin_round_id
            FROM partner_invoice_positions p
            JOIN group_b_round_refunds r ON r.refund_key=p.position_key''')
        db.commit()


def link_partner_invoice(db, invoice_id, chosen):
    """Persist all round relations for one approved multi-round partner invoice."""
    initialize(db)
    if chosen.empty or not (chosen.Gruppe == 'Gruppe B').any():
        return
    if not db.execute('SELECT 1 FROM group_b_rounds LIMIT 1').fetchone():
        return
    rounds = set()
    for key in chosen.loc[chosen.Gruppe == 'Gruppe B', 'position_key']:
        row = db.execute('SELECT round_id FROM group_b_round_positions WHERE position_key=?', (key,)).fetchone()
        if row:
            rounds.add(row[0])
            continue
        row = db.execute('SELECT origin_round_id FROM group_b_round_refunds WHERE refund_key=?', (key,)).fetchone()
        if not row:
            raise ValueError('Gruppe-B-Position besitzt keine eindeutige Ursprungsrunde.')
        rounds.add(row[0])
    for round_id in rounds:
        db.execute('INSERT INTO partner_invoice_rounds VALUES(?,?)', (invoice_id, round_id))


def _invoice_amounts(db):
    result = {}
    for row in db.execute('SELECT record FROM partner_invoices'):
        record = json.loads(row[0])
        if record.get('approved_at'):
            for item in record['expected']['items']:
                if item['key'] in result:
                    raise ValueError('Partnerposition ist mehreren bestätigten Rechnungen zugeordnet.')
                result[item['key']] = Decimal(item['gross'])
    return result


def overview(business):
    """Return round, partner, reserve and unassigned-hold rows without writes."""
    if business.empty or not {'Gruppe', 'position_key'}.issubset(business.columns):
        return {'rounds': [], 'partners': [], 'unassigned_holds': business.iloc[0:0].copy()}
    with core.ledger() as db:
        rounds = [dict(row) for row in db.execute('SELECT * FROM group_b_rounds ORDER BY year,sequence')]
        positions = [dict(row) for row in db.execute('SELECT * FROM group_b_round_positions')]
        refunds = [dict(row) for row in db.execute('SELECT * FROM group_b_round_refunds')]
        invoice_amounts = _invoice_amounts(db)
    by_key = {row.position_key: row for _, row in business.iterrows()}
    position_round = {row['position_key']: row for row in positions}
    refund_round = {row['refund_key']: row for row in refunds}
    calculated_sales = dict(invoice_amounts)
    mapped_active_sales = [by_key[row['position_key']] for row in positions
                           if row['position_key'] in by_key
                           and row['position_key'] not in invoice_amounts
                           and not bool(api_holds.mask(business.loc[[by_key[row['position_key']].name]]).any())]
    mapped_active_refunds = [by_key[row['refund_key']] for row in refunds
                             if row['refund_key'] in by_key
                             and row['origin_position_key'] in {sale.position_key for sale in mapped_active_sales}]
    for partner in sorted({row.Partner for row in mapped_active_sales}):
        sales = sorted((row for row in mapped_active_sales if row.Partner == partner), key=lambda row: row.name)
        credits = sorted((row for row in mapped_active_refunds if row.Partner == partner), key=lambda row: row.name)
        block = pd.DataFrame(sales + credits); block.index = [row.name for row in sales + credits]
        model = prepare_partner_export(block)
        for row, item in zip(sales, model['Rechnung']):
            calculated_sales[row.position_key] = item['gross']
    result_rounds, result_partners = [], []
    for round_record in rounds:
        rid = round_record['id']
        assigned = [by_key[row['position_key']] for row in positions
                    if row['round_id'] == rid and row['position_key'] in by_key]
        invoice_assigned = [by_key[row['position_key']] for row in positions
                            if row['round_id'] == rid and row['role'] == 'evelyn_invoice'
                            and row['position_key'] in by_key]
        active_keys = {row.position_key for row in assigned
                       if not bool(api_holds.mask(business.loc[[row.name]]).any())}
        active_sales = [row for row in assigned if row.position_key in active_keys]
        held_sales = [row for row in assigned if row.position_key not in active_keys]
        origin_refunds = [by_key[row['refund_key']] for row in refunds
                          if row['origin_round_id'] == rid and row['refund_key'] in by_key
                          and row['origin_position_key'] in active_keys]
        partners = sorted({row.Partner for row in assigned + origin_refunds})
        partner_rows = []
        for partner in partners:
            sales = [row for row in active_sales if row.Partner == partner]
            partner_holds = [row for row in held_sales if row.Partner == partner]
            credits = [row for row in origin_refunds if row.Partner == partner]
            block = pd.DataFrame(sales + credits)
            if block.empty:
                corrections = Decimal(0)
            else:
                block.index = [row.name for row in sales + credits]
                model = prepare_partner_export(block)
                corrections = model['totals']['Gutschriften']['gross']
            positive = sum((calculated_sales[row.position_key] for row in sales), Decimal(0))
            paid = sum((invoice_amounts[row.position_key] for row in sales
                        if row.position_key in invoice_amounts and bool(row.paid_at)), Decimal(0))
            funded_holds = [row for row in partner_holds
                            if position_round[row.position_key]['role'] == 'evelyn_invoice']
            unfunded_holds = [row for row in partner_holds
                              if position_round[row.position_key]['role'] == 'hold_reserve']
            def hold_amount(hold_rows):
                if not hold_rows:
                    return Decimal(0)
                hold_block = pd.DataFrame(hold_rows); hold_block.index = [row.name for row in hold_rows]
                return prepare_partner_export(hold_block)['totals']['Rechnung']['gross']
            funded_hold = hold_amount(funded_holds)
            unfunded_hold = hold_amount(unfunded_holds)
            current = positive + corrections
            open_amount = current - paid
            item = dict(round_id=rid, partner=partner, positions=len(sales), positive=positive,
                        corrections=corrections, current=current, paid=paid, open=open_amount,
                        prior_corrections=Decimal(0), payable=open_amount,
                        funded_hold_reserve=funded_hold, unfunded_hold_reserve=unfunded_hold)
            partner_rows.append(item); result_partners.append(item)
        invoice_frame = pd.DataFrame(invoice_assigned)
        partner_basis = Decimal(0)
        if not invoice_frame.empty:
            for _, block in invoice_frame.groupby('Partner'):
                partner_basis += prepare_partner_export(block)['totals']['Rechnung']['gross']
        evelyn_invoiced = Decimal(round_record['evelyn_amount'])
        received = bool(invoice_assigned) and all(bool(row.received_at) for row in invoice_assigned)
        partner_paid = sum((row['paid'] for row in partner_rows), Decimal(0))
        partner_open = sum((max(row['open'], Decimal(0)) for row in partner_rows), Decimal(0))
        corrections = sum((row['corrections'] for row in partner_rows), Decimal(0))
        prior_corrections = sum((row['prior_corrections'] for row in partner_rows), Decimal(0))
        funded_hold_reserve = sum((row['funded_hold_reserve'] for row in partner_rows), Decimal(0))
        unfunded_hold_reserve = sum((row['unfunded_hold_reserve'] for row in partner_rows), Decimal(0))
        result_rounds.append(dict(
            round_id=rid, evelyn_invoiced=evelyn_invoiced,
            evelyn_paid=evelyn_invoiced if received else Decimal(0),
            partner_claims=sum((row['current'] for row in partner_rows), Decimal(0)),
            partner_paid=partner_paid, partner_open=partner_open,
            reserve_from_evelyn=partner_open + funded_hold_reserve,
            patrick_margin=evelyn_invoiced - partner_basis,
            corrections=corrections + prior_corrections,
            holds=len(held_sales), funded_hold_reserve=funded_hold_reserve,
            unfunded_hold_reserve=unfunded_hold_reserve, partners=partner_rows,
        ))
    partner_lookup = {(row['round_id'], row['partner']): row for row in result_partners}
    for mapping in refunds:
        if not mapping['settlement_round_id'] or mapping['settlement_round_id'] == mapping['origin_round_id']:
            continue
        correction = by_key.get(mapping['refund_key'])
        if correction is None:
            continue
        origin = partner_lookup.get((mapping['origin_round_id'], correction.Partner))
        target = partner_lookup.get((mapping['settlement_round_id'], correction.Partner))
        if not origin or not target or origin['positive'] - origin['paid'] > 0:
            continue
        one = pd.DataFrame([correction]); one.index = [correction.name]
        effect = prepare_partner_export(one)['totals']['Gutschriften']['gross']
        target['prior_corrections'] += effect
        target['payable'] += effect
    for settlement in result_rounds:
        settlement['corrections'] += sum((row['prior_corrections'] for row in settlement['partners']), Decimal(0))
    mapped = set(position_round) | set(refund_round)
    unassigned_holds = business[(business.Gruppe == 'Gruppe B')
                                & ~business.position_key.isin(mapped)
                                & api_holds.mask(business)].copy()
    return {'rounds': result_rounds, 'partners': result_partners,
            'unassigned_holds': unassigned_holds}
