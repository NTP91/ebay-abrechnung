"""Lexoffice Bestell-Import: eBay-Bestellberichte als Rechnungsentwuerfe anlegen.

Isoliert von Supabase und der Live-Auszahlungslogik. Liest ausschliesslich
tatsaechliche Bestelldaten (Artikelname, Menge, Preis) aus einem hochgeladenen
eBay-Bestellbericht (CSV oder Excel) und legt darauf basierend einen
Rechnungsentwurf (draft) ueber die offizielle Lexoffice API an.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

import pandas as pd
import requests

LEXOFFICE_INVOICES_URL = 'https://api.lexoffice.io/v1/invoices?finalize=false'

TITLE_ALIASES = ['Artikelbezeichnung', 'Artikelname', 'Title', 'Artikel', 'Bezeichnung']
QTY_ALIASES = ['Menge', 'Anzahl', 'Quantity', 'Stückzahl', 'Stueckzahl']
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


def read_order_report(uploaded_file) -> pd.DataFrame:
    """Liest einen eBay-Bestellbericht (CSV oder Excel) in ein DataFrame mit
    den Spalten Artikelname, Menge, Preis. Wirft OrderReportError bei
    fehlenden Pflichtspalten."""
    name = getattr(uploaded_file, 'name', '') or ''
    raw = uploaded_file.read()
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
