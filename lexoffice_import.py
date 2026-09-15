"""Lexoffice Bestell-Import: eBay-Bestellberichte als Rechnungsentwuerfe anlegen.

Isoliert von Supabase und der Live-Auszahlungslogik. Liest ausschliesslich
tatsaechliche Bestelldaten (Artikelname, Menge, Preis) aus einem hochgeladenen
eBay-Bestellbericht (CSV oder Excel) und legt darauf basierend einen
Rechnungsentwurf (draft) ueber die offizielle Lexoffice API an.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import pandas as pd
import requests

LEXOFFICE_INVOICES_URL = 'https://api.lexware.io/v1/invoices?finalize=false'
LEXOFFICE_CONTACTS_URL = 'https://api.lexware.io/v1/contacts'
DEFAULT_CUSTOMER_NUMBER = 16335
VAT_FACTOR = Decimal('1.19')
MAX_PLAUSIBLE_OFFER_PRICE = Decimal('1000000.00')
RECENT_POSITIONS_API = 3
ACTIVE_OFFER_BATCH_SIZE = 300

ORDER_DATE_ALIASES = (
    'Verkauft am', 'Sold on', 'Sale date', 'Bestelldatum', 'Order date',
    'Datum', 'Datum der Transaktionserstellung', 'Transaction creation date',
)


def _parse_order_date(value):
    text = '' if value is None else str(value).strip()
    iso_style = bool(re.match(r'^\d{4}-\d{2}-\d{2}', text))
    return pd.to_datetime(text, dayfirst=not iso_style, errors='coerce', utc=True)

TITLE_ALIASES = ['Artikelbezeichnung', 'Artikelname', 'Title', 'Artikel', 'Bezeichnung']
QTY_ALIASES = ['Menge', 'Anzahl', 'Quantity', 'Stückzahl', 'Stueckzahl']
SKU_ALIASES = ['SKU', 'Custom label', 'Custom Label', 'Benutzerdefiniertes Etikett', 'Bestandseinheit']
PRICE_ALIASES = [
    'Verkauft für', 'Verkauft fuer', 'Verkaufspreis', 'Gesamtpreis', 'Preis',
    'Sold For', 'Price', 'Item Price', 'Einzelpreis',
]


class OrderReportError(ValueError):
    pass


def recent_processed_positions(orders: pd.DataFrame, days: int = 30, now=None) -> pd.DataFrame:
    """Return validated rows directly from the imported order-line master.

    No payout, settlement, partner or invoice view is used here. Every unique
    transaction/item identity stays one row and no result limit is applied.
    """
    if orders.empty:
        return orders.copy()
    if int(days) < 1:
        raise OrderReportError('Der Zeitraum muss mindestens einen Tag umfassen.')
    required = {'Bestellnummer', 'Transaktionsnummer', 'Artikelnummer', 'SKU', 'Angebotstitel'}
    if not required.issubset(orders.columns):
        raise OrderReportError('Verarbeiteter Bestand enthält nicht alle benötigten Positionsfelder.')
    clean = lambda values: values.fillna('').astype(str).str.strip()
    order_ids = clean(orders['Bestellnummer'])
    transaction_ids = clean(orders['Transaktionsnummer'])
    item_ids = clean(orders['Artikelnummer'])
    skus = clean(orders['SKU'])
    titles = clean(orders['Angebotstitel'])
    identities = transaction_ids.where(
        transaction_ids != '', order_ids + '\x1f' + item_ids
    )
    complete = (order_ids != '') & ((transaction_ids != '') | (item_ids != ''))
    if identities.loc[complete].duplicated().any():
        raise OrderReportError('Bestellpositionen sind nicht eindeutig; Export bleibt gesperrt.')
    reference = pd.Timestamp.now(tz='Europe/Berlin') if now is None else pd.Timestamp(now)
    if reference.tzinfo is None:
        reference = reference.tz_localize('Europe/Berlin')
    else:
        reference = reference.tz_convert('Europe/Berlin')
    end = reference.normalize()
    start = end - pd.Timedelta(days=int(days) - 1)
    date_values = pd.Series('', index=orders.index, dtype=object)
    date_sources = pd.Series('', index=orders.index, dtype=object)
    for alias in ORDER_DATE_ALIASES:
        column = _find_column(orders.columns, [alias])
        if column is None:
            continue
        candidate = clean(orders[column])
        use = (date_values == '') & (candidate != '')
        date_values.loc[use] = candidate.loc[use]
        date_sources.loc[use] = str(column)
    if not date_values.astype(bool).any():
        raise OrderReportError(
            'Kein befülltes Bestell-/Verkaufsdatum gefunden. '
            f'Gefundene Spalten: {list(orders.columns)}'
        )
    dates = date_values.map(_parse_order_date).dt.tz_convert('Europe/Berlin')
    valid = complete & dates.between(start, end + pd.Timedelta(days=1), inclusive='left')
    result = orders.loc[valid].copy()
    result['Datum'] = date_values.loc[valid]
    result['Line_Item_ID'] = identities.loc[valid]
    result['_Importdatum'] = dates.loc[valid]
    result = result.sort_values(
        ['_Importdatum', 'Bestellnummer', 'Line_Item_ID'], ascending=[False, True, True]
    ).drop(columns=['_Importdatum'])
    result.attrs['date_sources'] = date_sources.loc[valid].value_counts().to_dict()
    result.attrs['missing_sku_rows'] = int((skus.loc[valid] == '').sum())
    result.attrs['missing_title_rows'] = int((titles.loc[valid] == '').sum())
    return result


def _find_column(columns, aliases):
    lower = {str(c).lstrip('\ufeff').strip().lower(): c for c in columns}
    for alias in aliases:
        hit = lower.get(alias.strip().lower())
        if hit is not None:
            return hit
    return None


def _uploaded_bytes(uploaded_file):
    if hasattr(uploaded_file, 'getvalue'):
        return uploaded_file.getvalue()
    raw = uploaded_file.read()
    try:
        uploaded_file.seek(0)
    except (AttributeError, OSError):
        pass
    return raw


def _money(value):
    if value is None or isinstance(value, (date, datetime, pd.Timestamp)):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    text = str(value).replace('\u00a0', '').replace('€', '').replace('EUR', '').strip()
    if not text:
        return None
    if ',' in text and '.' in text:
        text = text.replace('.', '').replace(',', '.') if text.rfind(',') > text.rfind('.') else text.replace(',', '')
    elif ',' in text:
        text = text.replace('.', '').replace(',', '.')
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


_MONTH_TOKEN = re.compile(
    r'(?i)(?:^|[^a-zäöü])(?:jan|feb|mar|mär|apr|may|mai|jun|jul|aug|sep|oct|okt|nov|dec|dez)(?:[^a-zäöü]|$)'
)


def _offer_price(value):
    """Parse an eBay price while rejecting spreadsheet date conversions."""
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return None
    text = '' if value is None else str(value).strip()
    if not text or _MONTH_TOKEN.search(text):
        return None
    price = _money(value)
    if price is None or not price.is_finite() or price <= 0 or price > MAX_PLAUSIBLE_OFFER_PRICE:
        return None
    return price


def read_order_report(uploaded_file) -> pd.DataFrame:
    """Liest einen eBay-Bestellbericht (CSV oder Excel) in ein DataFrame mit
    den Spalten Artikelname, Menge, Preis. Wirft OrderReportError bei
    fehlenden Pflichtspalten."""
    name = getattr(uploaded_file, 'name', '') or ''
    raw = _uploaded_bytes(uploaded_file)
    if name.lower().endswith(('.xlsx', '.xls')):
        df = pd.read_excel(io.BytesIO(raw), dtype=str)
    else:
        try:
            df = pd.read_csv(io.BytesIO(raw), sep=';', dtype=str, engine='python')
        except Exception:
            df = pd.read_csv(io.BytesIO(raw), sep=',', dtype=str, engine='python')
        if df.shape[1] == 1:
            df = pd.read_csv(io.BytesIO(raw), sep=',', dtype=str, engine='python')

    title_col = _find_column(df.columns, TITLE_ALIASES)
    qty_col = _find_column(df.columns, QTY_ALIASES)
    price_col = _find_column(df.columns, PRICE_ALIASES)

    if title_col is None or price_col is None:
        raise OrderReportError(
            'Bestellbericht enthält keine erkennbaren Spalten für Artikelname/Preis. '
            f'Gefundene Spalten: {list(df.columns)}'
        )

    out = pd.DataFrame()
    out['Artikelname'] = df[title_col].astype(str).str.strip()
    out['Menge'] = pd.to_numeric(df[qty_col], errors='coerce').fillna(1).astype(int) if qty_col else 1
    out['Menge'] = out['Menge'].where(out['Menge'] > 0, 1)
    out['Preis'] = (
        df[price_col].astype(str)
        .str.replace('€', '', regex=False)
        .str.replace('EUR', '', regex=False)
        .str.strip()
        .str.replace('.', '', regex=False)
        .str.replace(',', '.', regex=False)
    )
    out['Preis'] = pd.to_numeric(out['Preis'], errors='coerce')
    out = out.dropna(subset=['Artikelname', 'Preis'])
    out = out[out['Artikelname'] != '']
    return out.reset_index(drop=True)


def read_active_offers(uploaded_file) -> pd.DataFrame:
    """Read eBay offers and derive gross/net inventory values at 19% VAT."""
    name = getattr(uploaded_file, 'name', '') or ''
    raw = _uploaded_bytes(uploaded_file)
    if not raw:
        raise OrderReportError('Die Angebotsdatei ist leer.')
    if name.lower().endswith('.xlsx'):
        df = pd.read_excel(io.BytesIO(raw), dtype=str)
    elif name.lower().endswith('.csv'):
        attempts = []
        for separator in (';', ',', '\t'):
            try:
                candidate = pd.read_csv(io.BytesIO(raw), sep=separator, dtype=str, engine='python')
                attempts.append(candidate)
            except Exception:
                continue
        df = max(attempts, key=lambda frame: frame.shape[1]) if attempts else pd.DataFrame()
    else:
        raise OrderReportError('Aktive Angebote bitte als CSV- oder XLSX-Datei hochladen.')
    title_col = _find_column(df.columns, ['Title'])
    current_price_col = _find_column(df.columns, ['Current price'])
    start_price_col = _find_column(df.columns, ['Start price'])
    qty_col = _find_column(df.columns, QTY_ALIASES + ['Verfügbare Menge', 'Verfuegbare Menge'])
    sku_col = _find_column(df.columns, SKU_ALIASES)
    if title_col is None or (current_price_col is None and start_price_col is None):
        raise OrderReportError(
            'Angebotsdatei benötigt „Title“ sowie „Current price“ oder „Start price“. '
            f'Gefundene Spalten: {list(df.columns)}'
        )
    rows = []
    skipped_rows = 0
    fallback_rows = 0
    for _, source in df.iterrows():
        title = str(source.get(title_col, '')).strip()
        current_price = _offer_price(source.get(current_price_col, '')) if current_price_col else None
        start_price = _offer_price(source.get(start_price_col, '')) if start_price_col else None
        price = current_price if current_price is not None else start_price
        if not title or title.lower() == 'nan' or price is None:
            skipped_rows += 1
            continue
        price_source = 'Current price' if current_price is not None else 'Start price'
        if price_source == 'Start price' and current_price_col is not None:
            fallback_rows += 1
        quantity = _money(source.get(qty_col, 1)) if qty_col else Decimal(1)
        if quantity is None or quantity <= 0 or quantity != quantity.to_integral_value():
            skipped_rows += 1
            continue
        inventory_gross = (price / Decimal(3)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        inventory_net = (inventory_gross / VAT_FACTOR).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        vat_amount = inventory_gross - inventory_net
        rows.append({'Artikelname': title, 'SKU': '' if sku_col is None else str(source.get(sku_col, '')).strip(),
                     'Menge': int(quantity), 'Angebotspreis': price.quantize(Decimal('.01')),
                     'Preisquelle': price_source, 'Bestandswert': inventory_gross,
                     'Bestandswert Netto': inventory_net, 'MwSt 19 %': vat_amount})
    if not rows:
        raise OrderReportError('Keine gültigen Angebotspositionen mit Preis gefunden.')
    result = pd.DataFrame(rows)
    result.attrs['skipped_rows'] = skipped_rows
    result.attrs['fallback_rows'] = fallback_rows
    return result


@dataclass
class InvoiceDraftResult:
    ok: bool
    invoice_id: str | None = None
    status_code: int | None = None
    message: str = ''
    invoice_ids: tuple[str, ...] = ()
    batch_count: int = 0
    completed_batches: int = 0


def build_line_items(df: pd.DataFrame) -> list[dict]:
    items = []
    for _, row in df.iterrows():
        items.append({
            'type': 'custom',
            'name': str(row['Artikelname'])[:250],
            'quantity': float(row['Menge']),
            'unitName': 'Stück',
            'unitPrice': {
                'currency': 'EUR',
                'netAmount': round(float(row['Preis']), 2),
                'taxRatePercentage': 19,
            },
        })
    return items


def build_active_offer_line_items(df: pd.DataFrame) -> list[dict]:
    items = []
    for _, row in df.iterrows():
        item = {
            'type': 'custom', 'name': str(row['Artikelname'])[:250],
            'quantity': int(row['Menge']), 'unitName': 'Stück',
            'unitPrice': {'currency': 'EUR', 'grossAmount': float(Decimal(row['Bestandswert'])),
                          'taxRatePercentage': 19},
        }
        sku = str(row.get('SKU', '')).strip()
        if sku and sku.lower() != 'nan':
            item['description'] = 'SKU: ' + sku[:250]
        items.append(item)
    return items


def find_customer(api_key: str, customer_number=DEFAULT_CUSTOMER_NUMBER, http=requests) -> str:
    headers = {'Authorization': f'Bearer {api_key}', 'Accept': 'application/json'}
    try:
        response = http.get(LEXOFFICE_CONTACTS_URL,
                            params={'number': customer_number, 'customer': 'true'},
                            headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise OrderReportError(f'Kundensuche nicht erreichbar: {exc}') from None
    if response.status_code != 200:
        raise OrderReportError(f'Kundensuche fehlgeschlagen (HTTP {response.status_code}).')
    contacts = [contact for contact in response.json().get('content', [])
                if str(contact.get('roles', {}).get('customer', {}).get('number')) == str(customer_number)]
    if len(contacts) != 1 or not contacts[0].get('id'):
        raise OrderReportError(f'Kundennummer {customer_number} wurde nicht eindeutig gefunden.')
    return contacts[0]['id']


def create_draft_invoice(api_key: str, contact_id: str, line_items: list[dict], title: str = 'Bestell-Import') -> InvoiceDraftResult:
    if not api_key or not contact_id:
        return InvoiceDraftResult(ok=False, message='API-Key oder Kontakt-ID fehlt in st.secrets.')
    if not line_items:
        return InvoiceDraftResult(ok=False, message='Keine Positionen zum Übertragen vorhanden.')

    payload = {
        'archived': False,
        'voucherDate': pd.Timestamp.now().strftime('%Y-%m-%dT00:00:00.000+02:00'),
        'address': {'contactId': contact_id},
        'lineItems': line_items,
        'totalPrice': {'currency': 'EUR'},
        'taxConditions': {'taxType': 'net'},
        'title': title,
        'introduction': title,
        'remark': '',
    }
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    try:
        resp = requests.post(LEXOFFICE_INVOICES_URL, json=payload, headers=headers, timeout=30)
    except requests.RequestException as exc:
        return InvoiceDraftResult(ok=False, message=f'Netzwerkfehler: {exc}')

    if resp.status_code in (200, 201):
        data = resp.json()
        return InvoiceDraftResult(ok=True, invoice_id=data.get('id'), status_code=resp.status_code)
    return InvoiceDraftResult(ok=False, status_code=resp.status_code, message=resp.text[:500])


def create_active_offers_draft(api_key: str, offers: pd.DataFrame, http=requests) -> InvoiceDraftResult:
    """Create one non-finalized draft per block of at most 300 offer rows."""
    if not api_key:
        return InvoiceDraftResult(ok=False, message='API-Key fehlt.')
    if offers is None or offers.empty:
        return InvoiceDraftResult(ok=False, message='Keine Positionen zum Übertragen vorhanden.')
    try:
        contact_id = find_customer(api_key, http=http)
    except OrderReportError as exc:
        return InvoiceDraftResult(ok=False, message=str(exc))
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json', 'Accept': 'application/json'}
    batch_count = (len(offers) + ACTIVE_OFFER_BATCH_SIZE - 1) // ACTIVE_OFFER_BATCH_SIZE
    invoice_ids = []
    last_status = None
    for batch_index, start in enumerate(range(0, len(offers), ACTIVE_OFFER_BATCH_SIZE), start=1):
        block = offers.iloc[start:start + ACTIVE_OFFER_BATCH_SIZE]
        suffix = f' – Teil {batch_index}/{batch_count}' if batch_count > 1 else ''
        payload = {
            'archived': False,
            'voucherDate': pd.Timestamp.now().strftime('%Y-%m-%dT00:00:00.000+02:00'),
            'address': {'contactId': contact_id},
            'lineItems': build_active_offer_line_items(block),
            'totalPrice': {'currency': 'EUR'},
            'taxConditions': {'taxType': 'gross'},
            'title': 'Aktive Angebote – Bestandswert' + suffix,
            'introduction': 'Aktive Angebote – interner Einstands-/Bestandswert (Angebotspreis / 3)' + suffix,
            'remark': f'Automatisch aus der hochgeladenen Datei erstellt · Block {batch_index} von {batch_count}.',
        }
        try:
            response = http.post(LEXOFFICE_INVOICES_URL, json=payload, headers=headers, timeout=30)
        except requests.RequestException as exc:
            return InvoiceDraftResult(
                ok=False, invoice_id=invoice_ids[0] if invoice_ids else None,
                message=f'Netzwerkfehler in Block {batch_index}/{batch_count}: {exc}',
                invoice_ids=tuple(invoice_ids), batch_count=batch_count,
                completed_batches=batch_index - 1,
            )
        last_status = response.status_code
        if response.status_code not in (200, 201):
            return InvoiceDraftResult(
                ok=False, invoice_id=invoice_ids[0] if invoice_ids else None,
                status_code=response.status_code,
                message=(f'Block {batch_index}/{batch_count} fehlgeschlagen. '
                         f'{len(invoice_ids)} Entwurf/Entwürfe wurden zuvor erstellt. {response.text[:400]}'),
                invoice_ids=tuple(invoice_ids), batch_count=batch_count,
                completed_batches=batch_index - 1,
            )
        invoice_id = response.json().get('id')
        if invoice_id:
            invoice_ids.append(invoice_id)
    return InvoiceDraftResult(
        ok=True, invoice_id=invoice_ids[0] if invoice_ids else None,
        status_code=last_status, invoice_ids=tuple(invoice_ids),
        batch_count=batch_count, completed_batches=batch_count,
    )
