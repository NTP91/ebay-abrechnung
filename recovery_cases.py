"""Recovery cases: eBay refunds an order line whose original sale was
already paid out to the partner (Fall B). The old round and its frozen
partner snapshot/invoice/payment are never touched - a new, separate
recovery case is opened in whatever round is currently open, and that round
cannot be considered complete for the partner until the case is resolved.

Fall A (refund known before finalize()) needs no case at all - it is simply
part of the frozen Tab-2 "Erstattungen-Abzüge" already, via
partner_snapshot._partner_round_rows()'s own core.refund_links() stitching.
Fall C (a refund already counted in some earlier snapshot's own membership,
e.g. the historical MH/RE0090 model's 7 already-verrechnete refunds) is
excluded by construction here: detect() only ever considers a sale that is
itself assigned to a 2026-0xx neutral round (source_kind='neutral_weekly')
in group_b_round_positions - a position living only in the historical
GB-2026-001/002 rounds is never found there, so it can never spawn a new
case regardless of how many refunds it already carries.
"""
import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import core
import invoice_parser
import partner_snapshot
import position_workflow

TOLERANCE = Decimal('0.01')


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS recovery_cases (
        id TEXT PRIMARY KEY, partner TEXT NOT NULL, partner_group TEXT NOT NULL,
        position_key TEXT NOT NULL, order_number TEXT NOT NULL, sku TEXT NOT NULL,
        origin_round_id TEXT NOT NULL, origin_snapshot_hash TEXT NOT NULL,
        origin_invoice_number TEXT, origin_paid_at TEXT,
        refund_position_key TEXT NOT NULL UNIQUE, refund_transaction TEXT, refund_payout_id TEXT,
        refund_amount TEXT NOT NULL, detected_at TEXT NOT NULL,
        current_round_id TEXT NOT NULL, status TEXT NOT NULL,
        credit_received_at TEXT, credit_reviewed_at TEXT, resolved_at TEXT,
        credit_file_hash TEXT, credit_file_name TEXT, credit_file_bytes BLOB)''')
    db.execute('''CREATE TABLE IF NOT EXISTS recovery_case_rejections (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recovery_id TEXT NOT NULL,
        at TEXT NOT NULL, reasons TEXT NOT NULL)''')


def _money(value):
    return core.parse_money(str(value).replace('%', '').strip())


def _current_round_id(db):
    row = db.execute("SELECT id FROM group_b_rounds WHERE source_kind='neutral_weekly' "
                      "ORDER BY sequence DESC LIMIT 1").fetchone()
    if not row:
        raise ValueError('Keine aktuelle Wochenrunde vorhanden; zuerst den Wochenwechsel ausführen.')
    return row[0]


def detect(business=None, dry_run=False):
    """Scan for new Fall-B recovery cases and create exactly one per unique
    refund position - a true no-op on repeat (refund_position_key is UNIQUE,
    and already-seen keys are skipped before any insert is attempted, so a
    second sync of the same refund transaction never creates a duplicate).

    A refund is only ever turned into a case when every condition holds
    unambiguously:
      - it links 1:1 to exactly one original sale (core.refund_links(); an
        ambiguous or unmatched refund is left for the existing
        Prüfhinweis/hold machinery elsewhere - never guessed here),
      - refund and sale both carry a confirmed partner and no API-Hold
        (a hold is not a refund; a partnerless fee never reaches here since
        core.refund_links() only pairs Art in ('Bestellung','Erstattung')),
      - the sale is assigned to an existing 2026-0xx neutral round,
      - that round already has a finalized partner_snapshot for this partner
        (Fall C guard: any refund already inside an existing snapshot's own
        position_keys is skipped - already counted, in Tab 2 or a prior
        Tab-3 case, exactly once),
      - the partner has already been paid for that round (Fall B's own
        defining condition; not yet paid is not yet Fall B).

    dry_run=True computes and returns the exact same list of would-be case
    ids without writing anything (rolled back instead of committed) - for
    previewing against live/production data before a real run.

    Returns the list of newly created (or, if dry_run, would-be) recovery_case ids.
    """
    import group_b_rounds
    import partner_round_invoices
    import round_planner

    business = position_workflow.positions() if business is None else business
    if business.empty:
        return []
    confirmed_names = {name for name, _ in round_planner.confirmed_partners(business)}
    links = core.refund_links(business)
    created = []

    with core.ledger() as db:
        group_b_rounds.initialize(db)
        partner_snapshot.initialize(db)
        partner_round_invoices.initialize(db)
        initialize(db)

        neutral_assignment = {r[0]: r[1] for r in db.execute(
            "SELECT gbp.position_key, gbp.round_id FROM group_b_round_positions gbp "
            "JOIN group_b_rounds gr ON gr.id = gbp.round_id WHERE gr.source_kind='neutral_weekly'")}
        already_counted = set()
        for row in db.execute('SELECT position_keys FROM partner_round_snapshots'):
            already_counted.update(json.loads(row[0]))
        already_cases = {r[0] for r in db.execute('SELECT refund_position_key FROM recovery_cases')}
        current_round = _current_round_id(db)

        db.execute('BEGIN IMMEDIATE')
        for refund_index, sale_index in links.items():
            refund = business.loc[refund_index]
            sale = business.loc[sale_index]
            refund_key = refund.position_key
            if refund_key in already_counted or refund_key in already_cases:
                continue
            if refund.Partner not in confirmed_names or sale.Partner not in confirmed_names:
                continue
            if bool(refund.get('API_Hold', False)) or bool(sale.get('API_Hold', False)):
                continue
            sale_round = neutral_assignment.get(sale.position_key)
            if not sale_round:
                continue
            snap = db.execute('SELECT snapshot_hash FROM partner_round_snapshots WHERE round_id=? AND partner=?',
                               (sale_round, sale.Partner)).fetchone()
            if not snap:
                continue
            invoice = db.execute('SELECT invoice_number, paid_at FROM partner_round_invoices '
                                  'WHERE round_id=? AND partner=?', (sale_round, sale.Partner)).fetchone()
            if not invoice or not invoice['paid_at']:
                continue

            case_id = str(uuid.uuid4())
            now_iso = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
            db.execute('''INSERT INTO recovery_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                case_id, sale.Partner, sale.Gruppe, sale.position_key, str(sale.Bestellnummer), str(sale.SKU),
                sale_round, snap['snapshot_hash'], invoice['invoice_number'], invoice['paid_at'],
                refund_key, str(refund.get('Transaktionsnummer', '')), str(refund.get('Auszahlung Nr.', '')),
                str(refund['Erlös_Brutto']), now_iso, current_round, 'offen',
                None, None, None, None, None, None))
            already_cases.add(refund_key)
            created.append(case_id)
        if dry_run:
            db.rollback()
        else:
            db.commit()
    return created


def list_cases(round_id=None, partner=None):
    with core.ledger() as db:
        initialize(db)
        clauses, params = [], []
        if round_id is not None:
            clauses.append('current_round_id=?'); params.append(round_id)
        if partner is not None:
            clauses.append('partner=?'); params.append(partner)
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        rows = db.execute(f'SELECT * FROM recovery_cases{where} ORDER BY detected_at', params).fetchall()
    return [dict(row) for row in rows]


def status(round_id, partner):
    """'nicht_erforderlich' | 'fehlt' | 'erledigt' for the partner card in
    this round. Once any case has ever existed here, the result is
    permanently 'fehlt' or 'erledigt' - never 'nicht_erforderlich' again,
    even after resolution (the historical record of "was required, now
    done" must stay visible, per the manuscript's own rule)."""
    cases = list_cases(round_id=round_id, partner=partner)
    if not cases:
        return 'nicht_erforderlich'
    if any(case['status'] == 'offen' for case in cases):
        return 'fehlt'
    return 'erledigt'


def resolve(recovery_id, filename, content):
    """Upload a partner credit/repayment document for one open case and
    check it against exactly that case's expected order/amount - never
    against live data. A mismatch keeps no file, only a small rejection
    audit entry (timestamp + reasons); a match freezes the credit file and
    marks the case 'erledigt' once and permanently (an already-resolved case
    can never be reopened or re-checked).

    Returns (record_or_None, report) - report always carries status/errors.
    """
    with core.ledger() as db:
        initialize(db)
        case = db.execute('SELECT * FROM recovery_cases WHERE id=?', (recovery_id,)).fetchone()
        if not case:
            raise ValueError('Rückforderungsfall nicht vorhanden.')
        if case['status'] == 'erledigt':
            raise ValueError('Fall bereits erledigt; keine erneute Bearbeitung.')

        extracted = invoice_parser.extract(content, filename)
        expected_amount = abs(Decimal(case['refund_amount']))
        errors = []
        order_seen = any((item.get('order') or '').strip() == case['order_number']
                          for item in extracted.get('items', []))
        if not order_seen and case['order_number'] not in (extracted.get('text') or ''):
            errors.append(f"Bestellnummer {case['order_number']} nicht auf der Gutschrift erkannt.")

        actual_total = None
        if extracted.get('total'):
            try:
                actual_total = abs(_money(extracted['total']))
            except ValueError:
                errors.append('Gutschriftbetrag nicht sicher lesbar.')
        else:
            amounts = [item.get('gross') for item in extracted.get('items', []) if item.get('gross')]
            if amounts:
                try:
                    actual_total = abs(sum((_money(value) for value in amounts), Decimal(0)))
                except (ValueError, InvalidOperation):
                    pass
        if actual_total is None:
            errors.append('Gutschriftbetrag nicht erkennbar.')
        elif abs(actual_total - expected_amount) > TOLERANCE:
            errors.append(f'Gutschriftbetrag: Erwartet {expected_amount} €, Beleg {actual_total} €.')

        now_iso = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        if errors:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO recovery_case_rejections(recovery_id, at, reasons) VALUES(?,?,?)',
                       (recovery_id, now_iso, json.dumps(errors, ensure_ascii=False)))
            db.commit()
            return None, dict(status='deviation', errors=errors)

        digest = hashlib.sha256(content).hexdigest()
        db.execute('BEGIN IMMEDIATE')
        case = db.execute('SELECT status FROM recovery_cases WHERE id=?', (recovery_id,)).fetchone()
        if case['status'] == 'erledigt':
            db.rollback()
            raise ValueError('Fall bereits erledigt; keine erneute Bearbeitung.')
        db.execute('''UPDATE recovery_cases SET status='erledigt', credit_received_at=?, credit_reviewed_at=?,
            resolved_at=?, credit_file_hash=?, credit_file_name=?, credit_file_bytes=? WHERE id=?''',
            (now_iso, now_iso, now_iso, digest, filename, content, recovery_id))
        db.commit()
        record = dict(db.execute('SELECT * FROM recovery_cases WHERE id=?', (recovery_id,)).fetchone())
    return record, dict(status='matched', errors=[])


def partner_round_status(round_id, partner):
    """The combined completion state for one partner in one round: invoice
    review + payment (partner_round_invoices.status()) layered with this
    module's own recovery-case status. A partner is only ever 'abgeschlossen'
    once both are satisfied; an open recovery case downgrades an otherwise-
    complete partner to 'gutschrift_fehlt' without touching anything else."""
    import partner_round_invoices

    base = partner_round_invoices.status(round_id, partner)
    if base != 'abgeschlossen':
        return base
    if status(round_id, partner) == 'fehlt':
        return 'gutschrift_fehlt'
    return 'abgeschlossen'
