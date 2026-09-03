import os
import glob
import pandas as pd
import io
import csv
import re
import json
import tempfile
from pathlib import Path
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from filelock import FileLock
import sqlite3
import hashlib
import requests
import zipfile
from contextlib import contextmanager

ORDERS_DB_PATH = str(Path(os.environ.get('PAYMENT_DATA_DIR', '.')) / 'Master_Orders.csv')
PAYOUTS_DB_PATH = str(Path(os.environ.get('PAYMENT_DATA_DIR', '.')) / 'Master_Payouts.csv')

FIELDS = {
    'Bestellnummer': ['bestellnummer', 'order number', 'order id'],
    'Transaktionsnummer': ['transaktionsnummer', 'transaktions id', 'transaktions-id', 'transaction id'],
    'Artikelnummer': ['artikelnummer', 'artikelnr.', 'item id', 'item number'],
    'SKU': ['bestandseinheit', 'custom label', 'customlabel', 'sku', 'eigene sku'],
    'Angebotstitel': ['angebotstitel', 'artikelbezeichnung', 'artikeltitel', 'artikelname', 'item title', 'title'],
    'Auszahlung Nr.': ['auszahlung nr.', 'auszahlungsnummer', 'payout id', 'payout number'],
    'Betrag abzügl. Kosten': ['betrag abzügl. kosten', 'betrag abzüglich kosten', 'net amount', 'nettobetrag', 'netto_betrag'],
    'Typ': ['typ', 'type', 'transaktionstyp'],
    'Datum': ['datum der transaktionserstellung', 'datum', 'transaction creation date'],
}


def normalized(value):
    return re.sub(r'[^a-z0-9äöüß]', '', str(value).strip().lower())


def clean(value):
    if pd.isna(value) or str(value).strip().lower() in ('', 'nan', 'none', '--', '-'):
        return ''
    return str(value).strip()


def canonicalize(frame):
    result = frame.copy().fillna('')
    for field, aliases in FIELDS.items():
        candidates = [c for c in result if normalized(c) in {normalized(a) for a in [field, *aliases]}]
        if len(candidates) > 1:
            # Older master files may have both an original alias and an empty
            # generated canonical column. Coalesce only non-conflicting values.
            values = result[candidates].map(clean)
            if values.apply(lambda row: len(set(v for v in row if v)), axis=1).gt(1).any():
                raise ValueError(f'Mehrdeutige Spalten für {field}: {candidates}')
            merged = values.apply(lambda row: next((v for v in row if v), ''), axis=1)
            result = result.drop(columns=candidates)
            result[field] = merged
        elif candidates:
            result = result.rename(columns={candidates[0]: field})
    for field in FIELDS:
        if field not in result:
            result[field] = ''
        result[field] = result[field].map(clean)
    return result


def parse_money(value):
    text = clean(value).replace('EUR', '').replace('€', '').replace('\u00a0', '').replace(' ', '').replace('−', '-')
    if ',' in text:
        text = text.replace('.', '').replace(',', '.')
    if not re.fullmatch(r'-?\d+(\.\d{1,2})?', text):
        raise ValueError(f'Ungültiger Geldbetrag: {value!r}; keine Umwandlung in 0 EUR.')
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError('Geldbetrag nicht lesbar.') from exc


def read_report(file, kind='payout'):
    """Read metadata-prefixed CSV/XLSX; malformed financial rows fail closed."""
    if kind not in ('payout', 'orders'):
        raise ValueError('Unbekannter Berichtstyp.')
    raw = file.getvalue() if hasattr(file, 'getvalue') else file.read()
    filename = getattr(file, 'name', '')
    key_sets = [{normalized(a) for a in [field, *aliases]} for field, aliases in FIELDS.items()]

    def header_score(row):
        values = {normalized(c) for c in row}
        return sum(bool(values & keys) for keys in key_sets)

    if filename.lower().endswith('.xlsx'):
        sheet = pd.read_excel(io.BytesIO(raw), header=None, dtype=str, keep_default_na=False, engine='openpyxl')
        rows = sheet.values.tolist()
        if not rows:
            raise ValueError('Leerer Bericht.')
        index = max(range(len(rows)), key=lambda i: header_score(rows[i]))
        frame = pd.DataFrame(rows[index + 1:], columns=[clean(c) for c in rows[index]])
        frame = frame.loc[:, frame.columns != '']
    else:
        text = None
        for encoding in ('utf-8-sig', 'cp1252'):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError('Nicht unterstützte CSV-Kodierung.')
        lines = text.splitlines(keepends=True)
        candidates = []
        for separator in (';', ','):
            for i, line in enumerate(lines):
                cells = next(csv.reader([line], delimiter=separator))
                score = header_score(cells)
                if score >= 2:
                    candidates.append((score, -i, separator, i))
        if not candidates:
            raise ValueError('Keine eindeutige Berichtskopfzeile erkannt.')
        _, _, separator, index = max(candidates)
        reader = csv.reader(io.StringIO(''.join(lines[index:])), delimiter=separator, strict=True)
        header = next(reader)
        rows = []
        for row in reader:
            if not row or not any(clean(c) for c in row):
                continue
            # eBay order exports end with a record count and seller label.
            # Match those exact footer shapes, never skip arbitrary short rows.
            if kind == 'orders' and (
                (len(row) == 3 and row[0].strip().isdigit()
                 and row[1].strip() == 'Verkaufsprotokoll(e) heruntergeladen' and not row[2].strip())
                or (len(row) == 1 and row[0].strip().startswith('Verkäufername :'))
            ):
                continue
            if len(row) != len(header):
                raise ValueError(f'Unregelmäßige CSV-Zeile {index + reader.line_num}; Import abgebrochen statt Beträge zu verlieren.')
            rows.append(row)
        frame = pd.DataFrame(rows, columns=[c.strip() for c in header])
    frame = canonicalize(frame)
    if kind == 'payout':
        unidentified = (frame['Auszahlung Nr.'] == '') & (
            (frame['Bestellnummer'] != '') | (frame['Transaktionsnummer'] != '')
        )
        frame = frame[(frame['Auszahlung Nr.'] != '') | unidentified].copy()
        if frame.empty:
            raise ValueError('Keine Transaktionspositionen gefunden.')
        # Financial validation is performed per payout during import so that one
        # rejected payout cannot prevent independent payouts from being imported.
    else:
        # Summary/footer rows without an item identity are not article positions.
        frame = frame[(frame['Bestellnummer'] != '') & (
            (frame['Transaktionsnummer'] != '') | (frame['Artikelnummer'] != '')
        )].copy()
        if frame.empty:
            raise ValueError('Keine Bestellartikel mit Transaktions- oder Artikelnummer gefunden.')
    return frame.drop_duplicates().reset_index(drop=True)


def read_master(path):
    if not os.path.exists(path):
        return canonicalize(pd.DataFrame())
    # Never convert a corrupt existing database into an empty database.
    return canonicalize(pd.read_csv(path, sep=';', dtype=str, keep_default_na=False))


def import_reports(frames, path, kind, details=None):
    """Append atomically, preserving old records; reject conflicting payout copies."""
    if not frames:
        return 0
    # Initialize/check the register before the first CSV is written.
    with ledger():
        pass
    with FileLock(str(path) + '.lock'):
        existing = read_master(path)
        incoming = canonicalize(pd.concat(frames, ignore_index=True)).drop_duplicates()
        if kind == 'payout':
            from payout_import import merge_transactions
            with ledger() as db:
                locked = {row['id'] for row in db.execute('SELECT * FROM payouts') if row['attempt'] or row['invoice_id']}
            merged = existing.copy()
            counters = dict(new_paid=0, known_paid=0, new_open=0, still_open=0, assigned_open=0, warnings=[], payouts={})
            # Paid groups precede open rows to prevent stale open copies from
            # downgrading transactions. Each group is accepted or rejected alone.
            payout_ids = sorted(set(incoming['Auszahlung Nr.']) - {''})
            if (incoming['Auszahlung Nr.'] == '').any():
                payout_ids.append('')
            for payout_id in payout_ids:
                block = incoming[incoming['Auszahlung Nr.'] == payout_id]
                try:
                    if payout_id:
                        for value in block['Betrag abzügl. Kosten']:
                            parse_money(value)
                    candidate, counts = merge_transactions(merged, block, locked)
                except ValueError as exc:
                    warning = {'payout': payout_id, 'reason': str(exc), 'positions': len(block)}
                    counters['warnings'].append(warning)
                    with ledger() as db:
                        db.execute('INSERT INTO import_warnings(payout,at,reason,snapshot) VALUES(?,?,?,?)',
                                   (payout_id, datetime.now(timezone.utc).isoformat(), str(exc), block.to_json(orient='records',force_ascii=False)))
                        audit(db, payout_id, 'Import zur manuellen Prüfung: ' + str(exc))
                        db.commit()
                    continue
                merged = candidate
                counters['payouts'][payout_id] = counts
                for key, value in counts.items():
                    counters[key] += value
            if details is not None:
                details.update(counters)
        else:
            merged = pd.concat([existing, incoming], ignore_index=True).fillna('').drop_duplicates()
        if kind == 'orders':
            consolidated = []
            for _, incoming_row in merged.iterrows():
                require_identity = incoming_row['Bestellnummer'] and (incoming_row['Transaktionsnummer'] or incoming_row['Artikelnummer'])
                if not require_identity:
                    raise ValueError('Bestellposition ohne verwertbare Identität; keine Daten übernommen.')
                matches = []
                for record in consolidated:
                    same_transaction = incoming_row['Transaktionsnummer'] and incoming_row['Transaktionsnummer'] == record['Transaktionsnummer']
                    same_item = (incoming_row['Bestellnummer'] == record['Bestellnummer'] and incoming_row['Artikelnummer'] and incoming_row['Artikelnummer'] == record['Artikelnummer']
                                 and not (incoming_row['Transaktionsnummer'] and record['Transaktionsnummer']))
                    if same_transaction or same_item:
                        matches.append(record)
                if len(matches) > 1:
                    raise ValueError('Uneindeutige Bestellposition; keine Daten übernommen.')
                if not matches:
                    consolidated.append(incoming_row.copy())
                    continue
                record = matches[0]
                for col in merged.columns:
                    old, new = clean(record[col]), clean(incoming_row[col])
                    if old and new and old != new and col in ('Bestellnummer', 'Transaktionsnummer', 'Artikelnummer', 'SKU', 'Angebotstitel'):
                        raise ValueError(f'Widersprüchliche Bestellposition {incoming_row["Bestellnummer"]}, Spalte {col}; vorhandene Daten bleiben erhalten.')
                    record[col] = old or new
            merged = pd.DataFrame(consolidated, columns=merged.columns)
        count = len(merged) - len(existing)
        if merged.equals(existing.reset_index(drop=True)):
            return 0
        destination = Path(path)
        fd, temporary = tempfile.mkstemp(prefix='.import-', suffix='.csv', dir=destination.parent)
        os.close(fd)
        try:
            merged.to_csv(temporary, sep=';', index=False, encoding='utf-8-sig')
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return max(0, count)


def match_order(row, orders):
    for keys in [('Transaktionsnummer',), ('Bestellnummer', 'Artikelnummer'), ('Bestellnummer',)]:
        if not all(row[key] for key in keys):
            continue
        matches = orders
        for key in keys:
            matches = matches[matches[key] == row[key]]
        if len(matches) > 1:
            return None, 'Mehrdeutige Bestellzuordnung'
        if len(matches) == 1:
            match = matches.iloc[0]
            if any(row[k] and match[k] and row[k] != match[k] for k in ('Bestellnummer', 'Artikelnummer')):
                return None, 'Widersprüchliche Bestell-/Artikelnummer'
            return match, ''
    return None, 'Keine eindeutige Bestellzuordnung'

def load_master_data():
    """Use order report as the authoritative product source, never the payout."""
    payouts = read_master(PAYOUTS_DB_PATH)
    # Open transactions are persisted in the source store, never in settlement data.
    payouts = payouts[payouts['Auszahlung Nr.'] != '']
    orders = read_master(ORDERS_DB_PATH)
    processed = []
    for _, row in payouts.iterrows():
        if row['Typ'].strip().casefold() == 'einbehalten':
            continue  # Retain raw references; a hold is neither a sale nor a credit.
        amount = float(parse_money(row['Betrag abzügl. Kosten']))
        order_id = row['Bestellnummer']
        fee = not order_id and any(word in row['Typ'].lower() for word in ('gebühr', 'fee', 'belastung'))
        sku, title = '', ''
        issue = ''
        if not fee:
            match, issue = match_order(row, orders)
            if match is not None:
                sku = match['SKU']
                title = match['Angebotstitel']
                # Historical orders from before partner SKUs existed remain in
                # the source archive, but can never be assigned or settled.
                if not sku:
                    continue
        if not fee and (not order_id or not sku or not title):
            issue = issue or 'Bestellnummer, SKU oder Produktname im Bestellbericht fehlt'
        if issue:
            issue = 'Zuordnung fehlt: ' + issue
        partner = sku.split('/')[0].strip().upper()
        if not fee and not re.fullmatch(r'[A-Z0-9]+', partner):
            issue = issue or 'Zuordnung fehlt: SKU ohne verwertbaren Partner vor dem ersten Slash'
        if not fee and partner and not (partner.startswith(('PP', 'BA', 'MK', '001', 'MH')) or partner in known_group_b_partners()):
            issue = issue or 'Zuordnung fehlt: unbekannter Partner ' + partner
        if partner.startswith('MH'):
            partner = 'MH'
        if fee:
            partner, sku, title = '', '', title or 'Sonstige eBay-Gebühr'
        group = ('Gebühren' if fee else 'Ohne Zuordnung' if issue else
                 'Gruppe A' if partner.startswith(('PP', 'BA', 'MK', '001')) else 'Gruppe B')
        processed.append({
            'Datum': row['Datum'], 'Auszahlung Nr.': row['Auszahlung Nr.'],
            'Transaktionsnummer': row['Transaktionsnummer'], 'Artikelnummer': row['Artikelnummer'],
            'Bestellnummer': order_id, 'Partner': partner, 'SKU': sku,
            'Angebotstitel': title, 'Gruppe': group, 'Erlös_Brutto': amount,
            'Titelquelle': 'Bestellbericht' if not fee and not issue else '',
            'Payout-Angebotstitel': row['Angebotstitel'],
            'eBay-Auszahlungsstatus': clean(row.get('Auszahlungsstatus', '')),
            'eBay_Netto': round(amount / 1.19, 2),
            'Art': 'Gebühr' if fee else 'Erstattung' if amount < 0 else 'Bestellung',
            'Prüfhinweis': issue, 'Status': 'Prüfung erforderlich' if issue else 'vollständig zugeordnet',
        })
    return pd.DataFrame(processed)


def invoice_payout_remark(payout_ids):
    return 'eBay-Auszahlungsnummern: ' + ', '.join(sorted({str(value) for value in payout_ids}))


def known_group_b_partners():
    return set(json.loads(Path(__file__).with_name('partners.json').read_text(encoding='utf-8'))['group_b'])


def build_invoice_payload(master, payout_id, contact_id, money_received=False):
    """Dry-run builder: old per-transaction net calculation, no API side effects."""
    if not money_received:
        raise ValueError('Geldeingang des Payouts muss zuerst bestätigt werden.')
    payout = master[master['Auszahlung Nr.'] == str(payout_id)]
    if payout.empty or payout['Prüfhinweis'].astype(bool).any():
        raise ValueError('Payout fehlt oder enthält ungeklärte Zuordnungen.')
    related = payout[payout['Art'] != 'Gebühr']
    if not (related['Titelquelle'] == 'Bestellbericht').all():
        raise ValueError('Zuordnung fehlt: verbindlicher Bestellbericht-Titel fehlt.')
    sales = payout[(payout['Gruppe'] == 'Gruppe B') & (payout['Art'] == 'Bestellung') & (payout['Erlös_Brutto'] > 0)]
    if sales.empty:
        raise ValueError('Keine Gruppe-B-Bestellungen für diesen Payout.')
    now = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
    items = []
    for _, row in sales.iterrows():
        items.append({
            'type': 'custom', 'name': row['Angebotstitel'],
            'description': f"eBay-Bestellnummer: {row['Bestellnummer']}\nSKU: {row['SKU']}",
            'quantity': 1, 'unitName': 'Stück',
            'unitPrice': {'currency': 'EUR', 'netAmount': row['eBay_Netto'], 'taxRatePercentage': 19},
            'discountPercentage': 0.5,
        })
    return {
        'voucherDate': now, 'address': {'contactId': contact_id}, 'lineItems': items,
        'totalPrice': {'currency': 'EUR'}, 'taxConditions': {'taxType': 'net'},
        'shippingConditions': {'shippingDate': now, 'shippingType': 'service'},
        'remark': invoice_payout_remark(sales['Auszahlung Nr.']),
    }


API_URL = 'https://api.lexware.io/v1'
FOLLOWUP = {
    'Lexoffice-Entwurf erstellt': 'Partnerrechnung geprüft',
    'Partnerrechnung geprüft': 'Partner ausgezahlt',
    'Partner ausgezahlt': 'abgeschlossen',
}


@contextmanager
def ledger():
    """Mirror locks independently; rebuilding SQLite cannot release a reservation."""
    path = Path(PAYOUTS_DB_PATH).with_name('Settlement_State.sqlite3')
    guard = path.with_name('Settlement_Locks.json')
    workflow_guard = path.with_name('Settlement_Workflow.json')
    correction_guard = path.with_name('Settlement_Corrections.json')
    with FileLock(str(path) + '.guard.lock'):
        if not path.exists() and not guard.exists() and Path(PAYOUTS_DB_PATH).exists():
            raise ValueError('Rechnungsregister und Sperrensicherung fehlen bei vorhandenen Payouts. Vollständiges Backup wiederherstellen; Abrechnung gesperrt.')
        protected = json.loads(guard.read_text(encoding='utf-8')) if guard.exists() else []
        connection = sqlite3.connect(path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('CREATE TABLE IF NOT EXISTS payouts (id TEXT PRIMARY KEY, status TEXT NOT NULL, fingerprint TEXT, invoice_id TEXT, attempt TEXT, snapshot TEXT)')
            connection.execute('CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, payout TEXT, at TEXT, event TEXT)')
            connection.execute('CREATE TABLE IF NOT EXISTS imports (id INTEGER PRIMARY KEY, kind TEXT, filename TEXT, at TEXT, start TEXT, end TEXT, detected INTEGER, added INTEGER, present INTEGER, issues INTEGER, error TEXT)')
            connection.execute('CREATE TABLE IF NOT EXISTS import_warnings (id INTEGER PRIMARY KEY, payout TEXT, at TEXT, reason TEXT, snapshot TEXT)')
            connection.execute('CREATE TABLE IF NOT EXISTS position_workflow (position_key TEXT PRIMARY KEY, reviewed_at TEXT, paid_at TEXT, received_at TEXT, closed_at TEXT, source TEXT)')
            connection.execute('CREATE TABLE IF NOT EXISTS discarded_invoices (invoice_id TEXT PRIMARY KEY, label TEXT, discarded_at TEXT, snapshot TEXT)')
            if correction_guard.exists():
                for saved in json.loads(correction_guard.read_text(encoding='utf-8')):
                    connection.execute('INSERT OR IGNORE INTO discarded_invoices VALUES(?,?,?,?)', tuple(saved[k] for k in ('invoice_id','label','discarded_at','snapshot')))
            discarded = {r[0] for r in connection.execute('SELECT invoice_id FROM discarded_invoices')}
            for invoice_id in discarded:
                connection.execute("UPDATE payouts SET invoice_id=NULL, attempt=NULL, fingerprint=NULL, snapshot=NULL, status='vollständig zugeordnet' WHERE invoice_id=?", (invoice_id,))
            if workflow_guard.exists():
                for saved in json.loads(workflow_guard.read_text(encoding='utf-8')):
                    connection.execute('INSERT OR IGNORE INTO position_workflow(position_key,reviewed_at,paid_at,received_at,closed_at,source) VALUES(?,?,?,?,?,?)',
                                       tuple(saved[k] for k in ('position_key','reviewed_at','paid_at','received_at','closed_at','source')))
                    for field in ('reviewed_at','paid_at','received_at','closed_at'):
                        if saved[field]:
                            connection.execute(f'UPDATE position_workflow SET {field}=COALESCE({field},?) WHERE position_key=?', (saved[field], saved['position_key']))
            for row in protected:
                if row['invoice_id'] in discarded:
                    continue
                current = connection.execute('SELECT * FROM payouts WHERE id=?', (row['id'],)).fetchone()
                if not current or (row['attempt'] and not current['attempt']):
                    connection.execute('INSERT OR REPLACE INTO payouts(id,status,fingerprint,invoice_id,attempt,snapshot) VALUES(?,?,?,?,?,?)', tuple(row[k] for k in ('id','status','fingerprint','invoice_id','attempt','snapshot')))
                    audit(connection, row['id'], 'Status/Sperre aus unabhängiger Sperrensicherung wiederhergestellt')
            if not protected and not guard.exists() and Path(PAYOUTS_DB_PATH).exists():
                # Existing legacy rows migrate normally. Missing rows have unknown history.
                for payout in read_master(PAYOUTS_DB_PATH)['Auszahlung Nr.'].unique():
                    if not payout:
                        continue
                    connection.execute("INSERT OR IGNORE INTO payouts(id,status,attempt) VALUES(?, 'Prüfung erforderlich: Registerhistorie fehlt', 'unknown')", (str(payout),))
            connection.commit()
            yield connection
        finally:
            connection.rollback()  # never persist a caller's uncommitted partial operation
            records = [dict(row) for row in connection.execute('SELECT * FROM payouts ORDER BY id')]
            workflow_records = [dict(row) for row in connection.execute('SELECT * FROM position_workflow ORDER BY position_key')]
            corrections = [dict(row) for row in connection.execute('SELECT * FROM discarded_invoices ORDER BY invoice_id')]
            connection.close()
            temporary = correction_guard.with_suffix('.json.tmp')
            with temporary.open('w', encoding='utf-8') as output:
                json.dump(corrections, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, correction_guard)
            temporary = guard.with_suffix('.json.tmp')
            with temporary.open('w', encoding='utf-8') as output:
                json.dump(records, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, guard)
            temporary = workflow_guard.with_suffix('.json.tmp')
            with temporary.open('w', encoding='utf-8') as output:
                json.dump(workflow_records, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, workflow_guard)


def audit(db, payout_id, event):
    db.execute('INSERT INTO audit(payout,at,event) VALUES(?,?,?)',
               (str(payout_id), datetime.now(timezone.utc).isoformat(), event))


def payout_fingerprint(block):
    columns = ['Auszahlung Nr.', 'Bestellnummer', 'Transaktionsnummer', 'Artikelnummer',
               'SKU', 'Angebotstitel', 'Erlös_Brutto', 'eBay_Netto', 'Art', 'Gruppe', 'Prüfhinweis', 'Titelquelle']
    records = sorted(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in block[columns].to_dict('records'))
    return hashlib.sha256('\n'.join(records).encode()).hexdigest()


def sync_status(master):
    with ledger() as db:
        db.execute('BEGIN IMMEDIATE')
        if not master.empty:
            for payout_id, block in master.groupby('Auszahlung Nr.'):
                inserted = db.execute("INSERT OR IGNORE INTO payouts(id,status) VALUES(?, 'importiert')", (str(payout_id),))
                if inserted.rowcount:
                    audit(db, payout_id, 'importiert')
                row = db.execute('SELECT * FROM payouts WHERE id=?', (str(payout_id),)).fetchone()
                status = row['status']
                if row['fingerprint'] and row['fingerprint'] != payout_fingerprint(block):
                    status = 'Prüfung erforderlich: gesperrte Daten verändert'
                elif not row['attempt'] and status != 'Geld eingegangen':
                    status = 'Prüfung erforderlich' if block['Prüfhinweis'].astype(bool).any() else 'vollständig zugeordnet'
                elif not row['attempt'] and block['Prüfhinweis'].astype(bool).any():
                    status = 'Prüfung erforderlich'
                if status != row['status']:
                    db.execute('UPDATE payouts SET status=? WHERE id=?', (status, str(payout_id)))
                    audit(db, payout_id, status)
        db.commit()
        return pd.read_sql_query('SELECT id AS Auszahlung, status AS Status, invoice_id AS Entwurf, attempt AS Sperre FROM payouts ORDER BY id', db)


def confirm_received(payout_id):
    master = load_master_data()
    block = master[master['Auszahlung Nr.'] == str(payout_id)]
    if block.empty or block['Prüfhinweis'].astype(bool).any():
        raise ValueError('Zuordnung fehlt.')
    # Confirmation supplements, but cannot override, missing transfer evidence.
    if not (block['eBay-Auszahlungsstatus'] == 'Betrag überwiesen').all():
        raise ValueError('eBay-Nachweis „Betrag überwiesen“ fehlt.')
    sync_status(master)
    with ledger() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM payouts WHERE id=?', (str(payout_id),)).fetchone()
        if row['attempt']:
            raise ValueError('Payout bereits gesperrt.')
        db.execute("UPDATE payouts SET status='Geld eingegangen' WHERE id=?", (str(payout_id),))
        audit(db, payout_id, 'Geld eingegangen – manuell bestätigt mit eBay-Überweisungsnachweis')
        db.commit()


def advance_status(payout_id, target):
    raise ValueError('Pauschale Payout-Statusänderungen sind nicht mehr zulässig. Prüfung und Zahlung je Position bestätigen.')


def create_invoice_draft(api_key, payout_id, prior_invoices_checked=False, http=None, expected_fingerprints=None):
    """Single attempt per payout. Any uncertain POST outcome remains locked."""
    if not api_key or not prior_invoices_checked:
        raise ValueError('API-Key und Bestätigung der bisherigen Rechnungsprüfung erforderlich.')
    http = http or requests
    payout_ids = sorted({str(value) for value in payout_id}) if isinstance(payout_id, (list, tuple, set)) else [str(payout_id)]
    if not payout_ids:
        raise ValueError('Keine Payouts ausgewählt.')
    headers = {'Authorization': f'Bearer {api_key}', 'Accept': 'application/json'}
    # Protect against simultaneous imports while preparing the immutable snapshot.
    with FileLock(PAYOUTS_DB_PATH + '.lock'), FileLock(ORDERS_DB_PATH + '.lock'):
        master = load_master_data()
        sync_status(master)
        with ledger() as db:
            db.execute('BEGIN IMMEDIATE')
            for payout_id in payout_ids:
                row = db.execute('SELECT * FROM payouts WHERE id=?', (payout_id,)).fetchone()
                if not row or row['attempt'] or row['invoice_id'] or row['status'] != 'Geld eingegangen':
                    raise ValueError('Payout gesperrt oder Geldeingang/Zuordnung nicht bestätigt.')
                if expected_fingerprints is not None and expected_fingerprints.get(payout_id) != payout_fingerprint(master[master['Auszahlung Nr.'] == payout_id]):
                    raise ValueError('Datenstand geändert. Übersicht aktualisieren und erneut prüfen; kein Entwurf erstellt.')
            try:
                response = http.get(API_URL + '/contacts', params={'number': 16335, 'customer': 'true'}, headers=headers, timeout=20)
                if response.status_code != 200:
                    raise ValueError('Kontaktabfrage fehlgeschlagen.')
                contacts = [c for c in response.json().get('content', []) if
                            str(c.get('roles', {}).get('customer', {}).get('number')) == '16335']
                if len(contacts) != 1 or not contacts[0].get('id'):
                    raise ValueError('Kundennummer 16335 nicht eindeutig gefunden.')
                parts = [build_invoice_payload(master, pid, contacts[0]['id'], True) for pid in payout_ids]
                payload = dict(parts[0])
                payload['lineItems'] = [item for part in parts for item in part['lineItems']]
                payload['remark'] = invoice_payout_remark(payout_ids)
            except Exception:
                raise ValueError('Kontakt/Payload-Prüfung fehlgeschlagen; kein Rechnungsaufruf erfolgt.') from None
            if len(payload['lineItems']) > 300:
                raise ValueError('Mehr als 300 Positionen; manuelle Prüfung erforderlich.')
            for payout_id in payout_ids:
                block = master[master['Auszahlung Nr.'] == payout_id]
                db.execute("UPDATE payouts SET attempt='pending', fingerprint=?, snapshot=? WHERE id=?",
                           (payout_fingerprint(block), json.dumps(payload, ensure_ascii=False), payout_id))
                audit(db, payout_id, 'Entwurfsversuch reserviert; Altbestand manuell geprüft')
            db.commit()  # durable BEFORE the network write; never automatically retry
    try:
        response = http.post(API_URL + '/invoices', params={'finalize': 'false'}, headers=headers, json=payload, timeout=30, allow_redirects=False)
        if response.status_code not in (200, 201):
            raise ValueError('Rechnungsantwort nicht erfolgreich.')
        invoice_id = response.json().get('id')
        if not isinstance(invoice_id, str) or not invoice_id:
            raise ValueError('Entwurfs-ID fehlt.')
    except Exception:
        with ledger() as db:
            for payout_id in payout_ids:
                db.execute("UPDATE payouts SET attempt='unknown', status='Prüfung erforderlich' WHERE id=?", (payout_id,))
                audit(db, payout_id, 'API-Ergebnis unklar; erneute Erstellung gesperrt, Lexoffice manuell prüfen')
            db.commit()
        raise ValueError('API-Ergebnis unklar. Payout bleibt gesperrt; in Lexoffice prüfen. Nicht erneut erstellen.') from None
    with ledger() as db:
        for payout_id in payout_ids:
            db.execute("UPDATE payouts SET attempt='created', invoice_id=?, status='Lexoffice-Entwurf erstellt' WHERE id=?", (invoice_id, payout_id))
            audit(db, payout_id, 'Lexoffice-Entwurf erstellt: ' + invoice_id)
        db.commit()
    return invoice_id


def backup_data():
    """Consistent CSV + SQLite snapshot; never export credentials."""
    output = io.BytesIO()
    with FileLock(PAYOUTS_DB_PATH + '.lock'), FileLock(ORDERS_DB_PATH + '.lock'):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / 'Settlement_State.sqlite3'
            with ledger() as db:
                copy = sqlite3.connect(snapshot)
                try:
                    db.backup(copy)
                finally:
                    copy.close()
            with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
                for filename in (PAYOUTS_DB_PATH, ORDERS_DB_PATH):
                    if Path(filename).exists():
                        archive.write(filename, Path(filename).name)
                archive.write(snapshot, snapshot.name)
                guard = Path(PAYOUTS_DB_PATH).with_name('Settlement_Locks.json')
                archive.write(guard, guard.name)
                workflow_guard = Path(PAYOUTS_DB_PATH).with_name('Settlement_Workflow.json')
                archive.write(workflow_guard, workflow_guard.name)
                correction_guard = Path(PAYOUTS_DB_PATH).with_name('Settlement_Corrections.json')
                archive.write(correction_guard, correction_guard.name)
    return output.getvalue()


def get_group_b_summary(df):
    if df.empty:
        return pd.DataFrame()
    
    df_b = df[(df['Gruppe'] == 'Gruppe B') & (df['Art'] == 'Bestellung')].copy()
    if df_b.empty:
        return pd.DataFrame()

    grouped = df_b.groupby('Partner').agg(
        Anzahl_Transaktionen=('Bestellnummer', 'count'),
        eBay_Brutto_Gesamt=('Erlös_Brutto', 'sum')
    ).reset_index()

    grouped['Evelyn_Provision_0_5'] = grouped['eBay_Brutto_Gesamt'] * 0.005
    grouped['Auszahlung_von_Evelyn_an_Dich'] = grouped['eBay_Brutto_Gesamt'] - grouped['Evelyn_Provision_0_5']
    grouped['Deine_Marge_3_0'] = grouped['eBay_Brutto_Gesamt'] * 0.030
    grouped['Partner_Auszahlung_96_5'] = grouped['eBay_Brutto_Gesamt'] * 0.965

    total_row = pd.DataFrame([{
        'Partner': 'GESAMTSUMME (Gruppe B)',
        'Anzahl_Transaktionen': grouped['Anzahl_Transaktionen'].sum(),
        'eBay_Brutto_Gesamt': grouped['eBay_Brutto_Gesamt'].sum(),
        'Evelyn_Provision_0_5': grouped['Evelyn_Provision_0_5'].sum(),
        'Auszahlung_von_Evelyn_an_Dich': grouped['Auszahlung_von_Evelyn_an_Dich'].sum(),
        'Deine_Marge_3_0': grouped['Deine_Marge_3_0'].sum(),
        'Partner_Auszahlung_96_5': grouped['Partner_Auszahlung_96_5'].sum()
    }])

    return pd.concat([grouped, total_row], ignore_index=True)


def get_group_a_summary(df):
    if df.empty:
        return pd.DataFrame()
    
    df_a = df[(df['Gruppe'] == 'Gruppe A') & (df['Art'] == 'Bestellung')].copy()
    if df_a.empty:
        return pd.DataFrame()

    grouped = df_a.groupby('Partner').agg(
        Anzahl_Transaktionen=('Bestellnummer', 'count'),
        eBay_Brutto_Gesamt=('Erlös_Brutto', 'sum')
    ).reset_index()

    grouped['Evelyn_Provision_0_5'] = grouped['eBay_Brutto_Gesamt'] * 0.005
    grouped['Direkt_Auszahlung_Evelyn'] = grouped['eBay_Brutto_Gesamt'] - grouped['Evelyn_Provision_0_5']

    total_row = pd.DataFrame([{
        'Partner': 'GESAMTSUMME (Gruppe A)',
        'Anzahl_Transaktionen': grouped['Anzahl_Transaktionen'].sum(),
        'eBay_Brutto_Gesamt': grouped['eBay_Brutto_Gesamt'].sum(),
        'Evelyn_Provision_0_5': grouped['Evelyn_Provision_0_5'].sum(),
        'Direkt_Auszahlung_Evelyn': grouped['Direkt_Auszahlung_Evelyn'].sum()
    }])

    return pd.concat([grouped, total_row], ignore_index=True)


def get_refunds_summary(df):
    if df.empty:
        return pd.DataFrame()

    df_ref = df[(df['Art'] == 'Erstattung') & df['Gruppe'].isin(['Gruppe A', 'Gruppe B'])].copy()
    if df_ref.empty:
        return pd.DataFrame()

    df_ref['Gutschrift_Brutto'] = df_ref['Erlös_Brutto']
    df_ref['Provision'] = df_ref.apply(lambda row: row['Gutschrift_Brutto'] * (0.005 if row['Gruppe'] == 'Gruppe A' else 0.035), axis=1)
    df_ref['Evelyn_Korrektur_0_5'] = df_ref['Gutschrift_Brutto'] * 0.005
    df_ref['Gutschrift_Netto_Auszahlung'] = df_ref['Gutschrift_Brutto'] - df_ref['Provision']

    return df_ref[['Datum', 'Auszahlung Nr.', 'Bestellnummer', 'Partner', 'SKU', 'Angebotstitel', 'Gutschrift_Brutto', 'Provision', 'Evelyn_Korrektur_0_5', 'Gutschrift_Netto_Auszahlung']]


def export_to_excel(df):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Abrechnung')
    return output.getvalue()
