"""Lexoffice Bestell-Import: eBay-Bestellberichte als Rechnungsentwuerfe anlegen.

Isoliert von Supabase und der Live-Auszahlungslogik. Liest ausschliesslich
tatsaechliche Bestelldaten (Artikelname, Menge, Preis) aus einem hochgeladenen
eBay-Bestellbericht (CSV oder Excel) und legt darauf basierend einen
Rechnungsentwurf (draft) ueber die offizielle Lexoffice API an.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import pandas as pd
import requests

LEXOFFICE_INVOICES_URL = 'https://api.lexware.io/v1/invoices?finalize=false'
LEXOFFICE_CONTACTS_URL = 'https://api.lexware.io/v1/contacts'
DEFAULT_CUSTOMER_NUMBER = 16335

TITLE_ALIASES = ['Artikelbezeichnung', 'Artikelname', 'Title', 'Artikel', 'Bezeichnung']
QTY_ALIASES = ['Menge', 'Anzahl', 'Quantity', 'Stückzahl', 'Stueckzahl']
SKU_ALIASES = ['SKU', 'Custom label', 'Custom Label', 'Benutzerdefiniertes Etikett', 'Bestandseinheit']
PRICE_ALIASES = [
    'Verkauft für', 'Verkauft fuer', 'Verkaufspreis', 'Gesamtpreis', 'Preis',
    'Sold For', 'Price', 'Item Price', 'Einzelpreis',
]


class OrderReportError(ValueError):
    pass


def _find_column(columns, aliases):
    lower = {str(c).strip().lower(): c for c in columns}
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
    text = str(value).replace('\u00a0', '').replace('€', '').replace('EUR', '').strip()
    if not text:
        return None
    if ',' in text and '.' in text:
        text = text.replace('.', '').replace(',', '.') if text.rfind(',') > text.rfind('.') else text.replace(',', '')
    elif ',' in text:
        text = text.replace('.', '').replace(',', '.')
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


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
    """Read active eBay offers and derive a cent-rounded internal value (price / 3)."""
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
    title_col = _find_column(df.columns, TITLE_ALIASES + ['Titel', 'Angebotstitel'])
    price_col = _find_column(df.columns, PRICE_ALIASES + ['Aktueller Preis', 'Startpreis'])
    qty_col = _find_column(df.columns, QTY_ALIASES + ['Verfügbare Menge', 'Verfuegbare Menge'])
    sku_col = _find_column(df.columns, SKU_ALIASES)
    if title_col is None or price_col is None:
        raise OrderReportError('Angebotsdatei benötigt erkennbare Spalten für Artikelname und Preis.')
    rows = []
    for _, source in df.iterrows():
        title = str(source.get(title_col, '')).strip()
        price = _money(source.get(price_col, ''))
        if not title or title.lower() == 'nan' or price is None:
            continue
        if price <= 0:
            raise OrderReportError(f'Ungültiger Preis für „{title}“: Der Preis muss größer als 0 sein.')
        quantity = _money(source.get(qty_col, 1)) if qty_col else Decimal(1)
        if quantity is None or quantity <= 0 or quantity != quantity.to_integral_value():
            raise OrderReportError(f'Ungültige Menge für „{title}“.')
        internal = (price / Decimal(3)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        rows.append({'Artikelname': title, 'SKU': '' if sku_col is None else str(source.get(sku_col, '')).strip(),
                     'Menge': int(quantity), 'Angebotspreis': price.quantize(Decimal('.01')),
                     'Bestandswert': internal})
    if not rows:
        raise OrderReportError('Keine gültigen Angebotspositionen mit Preis gefunden.')
    if len(rows) > 300:
        raise OrderReportError('Mehr als 300 Positionen; Datei bitte auf mehrere Entwürfe aufteilen.')
    return pd.DataFrame(rows)


@dataclass
class InvoiceDraftResult:
    ok: bool
    invoice_id: str | None = None
    status_code: int | None = None
    message: str = ''


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
            'unitPrice': {'currency': 'EUR', 'netAmount': float(Decimal(row['Bestandswert'])),
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
    """Search the established customer and create one non-finalized inventory-value draft."""
    if not api_key:
        return InvoiceDraftResult(ok=False, message='API-Key fehlt.')
    try:
        contact_id = find_customer(api_key, http=http)
    except OrderReportError as exc:
        return InvoiceDraftResult(ok=False, message=str(exc))
    line_items = build_active_offer_line_items(offers)
    payload = {
        'archived': False,
        'voucherDate': pd.Timestamp.now().strftime('%Y-%m-%dT00:00:00.000+02:00'),
        'address': {'contactId': contact_id},
        'lineItems': line_items,
        'totalPrice': {'currency': 'EUR'},
        'taxConditions': {'taxType': 'net'},
        'title': 'Aktive Angebote – Bestandswert',
        'introduction': 'Aktive Angebote – interner Einstands-/Bestandswert (Angebotspreis / 3)',
        'remark': 'Automatisch aus der hochgeladenen Datei der aktiven Angebote erstellt.',
    }
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json', 'Accept': 'application/json'}
    try:
        response = http.post(LEXOFFICE_INVOICES_URL, json=payload, headers=headers, timeout=30)
    except requests.RequestException as exc:
        return InvoiceDraftResult(ok=False, message=f'Netzwerkfehler: {exc}')
    if response.status_code in (200, 201):
        return InvoiceDraftResult(ok=True, invoice_id=response.json().get('id'), status_code=response.status_code)
    return InvoiceDraftResult(ok=False, status_code=response.status_code, message=response.text[:500])
