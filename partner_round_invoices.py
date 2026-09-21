"""Incoming partner invoices and payment, for 2026-003+ (neutral round model).

The one-step Patrick-collects-then-invoices-Evelyn detour (and the old
Gruppe-B-Gesamtrechnung it needed) is gone from 003 onward: every partner,
Gruppe A or B alike, invoices Evelyn directly. Checking that invoice is
deliberately never done against live data - only ever against the exact,
immutable partner_snapshot.finalize() result for (round_id, partner), so a
later live-data correction or newly-arriving refund can never retroactively
change what an already-reviewed/paid invoice was checked against.

Independent of the historical partner_invoices.py / position_workflow.py
machinery (001/002, MH's paid-without-invoice case): this module never
imports or calls into either, and neither of them is changed here.
"""
import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import core
import invoice_parser
import partner_snapshot

TOLERANCE = Decimal('0.01')


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS partner_round_invoices (
        round_id TEXT NOT NULL, partner TEXT NOT NULL,
        invoice_number TEXT, invoice_date TEXT,
        file_hash TEXT NOT NULL, file_name TEXT NOT NULL, file_bytes BLOB NOT NULL,
        uploaded_at TEXT NOT NULL, amount TEXT NOT NULL,
        snapshot_hash TEXT NOT NULL, reviewed_at TEXT NOT NULL,
        paid_at TEXT, paid_amount TEXT, paid_note TEXT, paid_position_count INTEGER,
        PRIMARY KEY(round_id, partner))''')
    db.execute('''CREATE TABLE IF NOT EXISTS partner_round_invoice_rejections (
        id INTEGER PRIMARY KEY AUTOINCREMENT, round_id TEXT NOT NULL, partner TEXT NOT NULL,
        at TEXT NOT NULL, reasons TEXT NOT NULL, invoice_number TEXT)''')


def _money(value):
    return core.parse_money(str(value).replace('%', '').strip())


def reconcile(extracted, line_items, final_amount):
    """Check an uploaded invoice against a frozen snapshot's own stored Tab-1
    line items and Rechnungsendbetrag - never against live business data.

    line_items: partner_snapshot's stored, immutable per-position
    expectations (order/sku/gross/rate). final_amount: the snapshot's own
    frozen FINALER RECHNUNGSBETRAG (regular claim already netted with Tab-2
    Erstattungen/Abzüge) - the one number the partner may actually invoice.
    """
    errors, warnings = [], []
    if not extracted.get('number'):
        warnings.append('Rechnungsnummer nicht sicher erkannt.')
    remaining = list(line_items)
    for index, item in enumerate(extracted.get('items', []), 1):
        order = (item.get('order') or '').strip()
        sku = (item.get('sku') or '').strip()
        prefix = f'Position {index} / Bestellnummer {order or "nicht erkannt"}: '
        if not order:
            warnings.append(prefix + 'Bestellnummer fehlt.')
            continue
        candidates = [row for row in remaining if row['order'] == order and (not sku or row['sku'] == sku)]
        if not candidates:
            if any(row['order'] == order for row in line_items):
                errors.append(prefix + 'SKU stimmt nicht mit dem finalen Snapshot überein.')
            else:
                errors.append(prefix + 'Zusätzliche/unbekannte Position; gehört nicht zum finalen Partner-Snapshot.')
            continue
        wanted = candidates[0]
        remaining.remove(wanted)
        if item.get('gross'):
            try:
                actual = _money(item['gross'])
                if abs(actual - Decimal(wanted['gross'])) > TOLERANCE:
                    errors.append(prefix + f"Betrag: Erwartet {wanted['gross']} €, Rechnung {actual} €.")
            except ValueError:
                warnings.append(prefix + 'Positionsbetrag nicht sicher lesbar.')
        if item.get('rate'):
            try:
                actual_rate = Decimal(str(item['rate']).replace('%', '').strip().replace(',', '.'))
                if actual_rate != Decimal(wanted['rate']):
                    errors.append(prefix + f"Rabatt: Erwartet {wanted['rate']} %, Rechnung {actual_rate} %.")
            except InvalidOperation:
                warnings.append(prefix + 'Rabatt nicht sicher lesbar.')
    for row in remaining:
        errors.append('Bestellnummer ' + row['order'] + ' / SKU ' + row['sku']
                       + ' fehlt auf der Rechnung (im finalen Snapshot enthalten).')
    if not extracted.get('total'):
        warnings.append('Gesamtbetrag brutto nicht sicher erkannt.')
    else:
        try:
            actual_total = _money(extracted['total'])
            if abs(actual_total - final_amount) > TOLERANCE:
                errors.append(f'Rechnungsendbetrag: Erwartet {final_amount} €, Rechnung {actual_total} €.')
        except ValueError:
            warnings.append('Gesamtbetrag nicht sicher lesbar.')
    return dict(status='deviation' if errors else 'matched', errors=errors, warnings=warnings)


def check_and_review(round_id, partner, filename, content, invoice_number=None):
    """Upload + reconcile against the frozen snapshot; on success, store the
    invoice (file + record) and mark it reviewed - atomically, one shot.

    Requires an existing partner_snapshot.finalize() result for (round_id,
    partner); raises otherwise ("finale Einzelabrechnung zuerst erstellen").
    A pair that was already successfully reviewed can never be re-uploaded
    (no second review, ever). A failed check never stores the file - only a
    lightweight rejection audit entry (timestamp, reasons) is kept.

    Returns (record_or_None, report). record is None when the check failed;
    report always carries status/errors/warnings.
    """
    with core.ledger() as db:
        partner_snapshot.initialize(db)
        initialize(db)
        snap = db.execute('SELECT * FROM partner_round_snapshots WHERE round_id=? AND partner=?',
                           (round_id, partner)).fetchone()
        if not snap:
            raise ValueError(f'Noch kein finaler Partner-Snapshot für {partner} in {round_id}; '
                              f'zuerst finale Einzelabrechnung erstellen.')
        if db.execute('SELECT 1 FROM partner_round_invoices WHERE round_id=? AND partner=?',
                       (round_id, partner)).fetchone():
            raise ValueError(f'Rechnung für {partner} in {round_id} bereits erfolgreich geprüft.')

        extracted = invoice_parser.extract(content, filename)
        line_items = json.loads(snap['line_items'])
        final_amount = Decimal(snap['final_amount'])
        report = reconcile(extracted, line_items, final_amount)
        number = (invoice_number or extracted.get('number') or '').strip()
        now_iso = datetime.now(timezone.utc).isoformat(timespec='milliseconds')

        if report['status'] != 'matched':
            db.execute('BEGIN IMMEDIATE')
            db.execute('''INSERT INTO partner_round_invoice_rejections(round_id,partner,at,reasons,invoice_number)
                VALUES(?,?,?,?,?)''', (round_id, partner, now_iso,
                                        json.dumps(report['errors'] or report['warnings'], ensure_ascii=False), number))
            db.commit()
            return None, report

        digest = hashlib.sha256(content).hexdigest()
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM partner_round_invoices WHERE round_id=? AND partner=?',
                       (round_id, partner)).fetchone():
            db.rollback()
            raise ValueError(f'Rechnung für {partner} in {round_id} bereits erfolgreich geprüft.')
        db.execute('''INSERT INTO partner_round_invoices VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
            round_id, partner, number, extracted.get('invoice_date') or '',
            digest, filename, content, now_iso, str(final_amount),
            snap['snapshot_hash'], now_iso, None, None, None, None))
        db.commit()
        record = dict(db.execute('SELECT * FROM partner_round_invoices WHERE round_id=? AND partner=?',
                                  (round_id, partner)).fetchone())
    return record, report


def confirm_payment(round_id, partner, paid_date=None, note=''):
    """'Zahlung überwiesen': records the payment for an already-reviewed
    invoice. Idempotent - a second call for an already-paid pair is a true
    no-op (returns the existing record, created=False), never a second
    payment. paid_date defaults to today, editable by the caller."""
    with core.ledger() as db:
        initialize(db)
        row = db.execute('SELECT * FROM partner_round_invoices WHERE round_id=? AND partner=?',
                          (round_id, partner)).fetchone()
        if not row:
            raise ValueError(f'Rechnung für {partner} in {round_id} noch nicht erfolgreich geprüft; '
                              f'Zahlung nicht möglich.')
        if row['paid_at']:
            return dict(row), False

        value = date.fromisoformat(str(paid_date)) if paid_date else date.today()
        if value > date.today():
            raise ValueError('Ein zukünftiges Zahlungsdatum ist nicht zulässig.')

        snap = db.execute('SELECT position_count FROM partner_round_snapshots WHERE round_id=? AND partner=?',
                           (round_id, partner)).fetchone()
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM partner_round_invoices WHERE round_id=? AND partner=?',
                          (round_id, partner)).fetchone()
        if row['paid_at']:
            db.rollback()
            return dict(row), False
        db.execute('''UPDATE partner_round_invoices SET paid_at=?, paid_amount=?, paid_note=?, paid_position_count=?
            WHERE round_id=? AND partner=?''',
            (value.isoformat(), row['amount'], note or '', snap['position_count'] if snap else None,
             round_id, partner))
        db.commit()
        record = dict(db.execute('SELECT * FROM partner_round_invoices WHERE round_id=? AND partner=?',
                                  (round_id, partner)).fetchone())
    return record, True


def status(round_id, partner):
    """One of: 'kein_snapshot' (finale Einzelabrechnung fehlt noch),
    'rechnung_ausstehend' (Snapshot vorhanden, keine geprüfte Rechnung),
    'zahlung_ausstehend' (geprüft, noch nicht bezahlt), 'abgeschlossen'
    (geprüft und bezahlt)."""
    with core.ledger() as db:
        partner_snapshot.initialize(db)
        initialize(db)
        snap = db.execute('SELECT 1 FROM partner_round_snapshots WHERE round_id=? AND partner=?',
                           (round_id, partner)).fetchone()
        invoice = db.execute('SELECT paid_at FROM partner_round_invoices WHERE round_id=? AND partner=?',
                              (round_id, partner)).fetchone()
    if not snap:
        return 'kein_snapshot'
    if not invoice:
        return 'rechnung_ausstehend'
    if not invoice['paid_at']:
        return 'zahlung_ausstehend'
    return 'abgeschlossen'
