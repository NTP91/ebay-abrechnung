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

ORDERS_DB_PATH = "Master_Orders.csv"
PAYOUTS_DB_PATH = "Master_Payouts.csv"

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
        if unidentified.any():
            raise ValueError('Transaktionszeilen ohne Auszahlungsnummer: Import zur Prüfung angehalten.')
        frame = frame[frame['Auszahlung Nr.'] != ''].copy()
        if frame.empty:
            raise ValueError('Keine Auszahlung mit Auszahlungsnummer gefunden.')
        for value in frame['Betrag abzügl. Kosten']:
            parse_money(value)
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


def import_reports(frames, path, kind):
    """Append atomically, preserving old records; reject conflicting payout copies."""
    if not frames:
        return 0
    with FileLock(str(path) + '.lock'):
        existing = read_master(path)
        incoming = canonicalize(pd.concat(frames, ignore_index=True)).drop_duplicates()
        if kind == 'payout':
            for value in incoming['Betrag abzügl. Kosten']:
                parse_money(value)
            all_columns = sorted(set(existing.columns) | set(incoming.columns))

            def fingerprints(frame):
                return set(map(tuple, frame.reindex(columns=all_columns, fill_value='').fillna('').astype(str).values.tolist()))

            for payout_id, block in incoming.groupby('Auszahlung Nr.'):
                if not payout_id:
                    raise ValueError('Auszahlungsnummer fehlt.')
                old = existing[existing['Auszahlung Nr.'] == payout_id]
                if not old.empty and fingerprints(old) != fingerprints(block):
                    raise ValueError(f'Auszahlung {payout_id} existiert mit abweichenden Daten. Manuelle Korrektur nötig.')
        merged = pd.concat([existing, incoming], ignore_index=True).fillna('').drop_duplicates()
        if kind == 'orders':
            keys = ['Bestellnummer', 'Transaktionsnummer', 'Artikelnummer']
            consolidated = []
            for identity, block in merged.groupby(keys, dropna=False, sort=False):
                record = block.iloc[0].copy()
                for col in merged.columns:
                    values = list(dict.fromkeys(clean(v) for v in block[col] if clean(v)))
                    if len(values) > 1:
                        raise ValueError(f'Widersprüchliche Bestellposition {identity}, Spalte {col}; vorhandene Daten bleiben erhalten.')
                    record[col] = values[0] if values else ''
                consolidated.append(record)
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
            return matches.iloc[0], ''
    return None, 'Keine eindeutige Bestellzuordnung'

def load_master_data():
    """Preserve payout product data and enrich only through unambiguous matches."""
    payouts = read_master(PAYOUTS_DB_PATH)
    orders = read_master(ORDERS_DB_PATH)
    processed = []
    for _, row in payouts.iterrows():
        amount = float(parse_money(row['Betrag abzügl. Kosten']))
        order_id = row['Bestellnummer']
        fee = not order_id and any(word in row['Typ'].lower() for word in ('gebühr', 'fee', 'belastung'))
        sku, title = row['SKU'], row['Angebotstitel']
        issue = ''
        if not fee and (not sku or not title):
            match, issue = match_order(row, orders)
            if match is not None:
                sku = sku or match['SKU']
                title = title or match['Angebotstitel']
        if not fee and (not order_id or not sku or not title):
            issue = issue or 'Bestellnummer, SKU oder Produktname fehlt'
        partner = sku.split('/')[0].strip().upper()
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
            'eBay_Netto': round(amount / 1.19, 2),
            'Art': 'Gebühr' if fee else 'Erstattung' if amount < 0 else 'Bestellung',
            'Prüfhinweis': issue, 'Status': 'Prüfung erforderlich' if issue else 'vollständig zugeordnet',
        })
    return pd.DataFrame(processed)


def build_invoice_payload(master, payout_id, contact_id, money_received=False):
    """Dry-run builder: old per-transaction net calculation, no API side effects."""
    if not money_received:
        raise ValueError('Geldeingang des Payouts muss zuerst bestätigt werden.')
    payout = master[master['Auszahlung Nr.'] == str(payout_id)]
    if payout.empty or payout['Prüfhinweis'].astype(bool).any():
        raise ValueError('Payout fehlt oder enthält ungeklärte Zuordnungen.')
    sales = payout[(payout['Gruppe'] == 'Gruppe B') & (payout['Art'] == 'Bestellung')]
    if sales.empty:
        raise ValueError('Keine Gruppe-B-Bestellungen für diesen Payout.')
    now = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
    items = []
    for _, row in sales.iterrows():
        items.append({
            'type': 'custom', 'name': row['Angebotstitel'],
            'description': f"SKU: {row['SKU']}\n eBay-Bestellnummer: {row['Bestellnummer']}\n eBay-Auszahlungsnummer: {payout_id}",
            'quantity': 1, 'unitName': 'Stück',
            'unitPrice': {'currency': 'EUR', 'netAmount': row['eBay_Netto'], 'taxRatePercentage': 19},
            'discountPercentage': 0.5,
        })
    return {
        'voucherDate': now, 'address': {'contactId': contact_id}, 'lineItems': items,
        'totalPrice': {'currency': 'EUR'}, 'taxConditions': {'taxType': 'net'},
        'shippingConditions': {'shippingDate': now, 'shippingType': 'service'},
        'remark': f'eBay-Auszahlung {payout_id}; Erstattungen werden getrennt abgerechnet.',
    }


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
