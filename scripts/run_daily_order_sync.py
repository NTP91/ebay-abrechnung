"""Daily production order sync: eBay Fulfillment API -> Supabase source/orders.csv.

Runs the existing, unmodified core.import_reports(kind='orders') path -- same
identity rule (Transaktionsnummer primary, Bestellnummer+Artikelnummer
fallback), same conflict/dedup handling, same canonical order schema, same
Supabase write channel as the manual order-report upload. No changes to
payout, rounds, invoice, Lexware or completion logic. Reuses the same eBay/
Supabase secrets as the payout sync -- no new credentials.

Trigger mapping: GitHub's schedule event -> automatic daily run; workflow_dispatch
(manual test run) -> same import, for controlled testing. core.import_reports
already refuses to write on a conflicting/ambiguous position (raises instead of
guessing), so a repeated run cannot create duplicates.
"""
import json
import os
import sys
from datetime import timedelta, timezone, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def ebay_provider():
    """Build the ebay_readonly secrets_config() shape from GitHub Actions secrets.

    The Fulfillment orders endpoint needs no RFC 9421 signature (unlike the
    Finances endpoints), so only the plain OAuth credentials are required here.
    """
    from ebay_readonly import EbayError
    values = {
        'client_id': os.environ.get('EBAY_CLIENT_ID', ''),
        'client_secret': os.environ.get('EBAY_CLIENT_SECRET', ''),
        'ru_name': os.environ.get('EBAY_RU_NAME', ''),
        'refresh_token': os.environ.get('EBAY_REFRESH_TOKEN', ''),
    }
    if not all(values.values()):
        raise EbayError('Zugangsdaten nicht verfuegbar: EBAY_CLIENT_ID/EBAY_CLIENT_SECRET/EBAY_RU_NAME/EBAY_REFRESH_TOKEN erforderlich.')
    return values


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def to_de_date(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc).strftime('%d.%m.%Y')
    except (ValueError, TypeError):
        return ''


def build_frame(orders):
    """Map eBay order + line items onto the existing canonical order-row schema."""
    import pandas as pd
    rows = []
    for order in orders:
        order_id = order.get('orderId', '')
        creation = to_de_date(order.get('creationDate', ''))
        for line in order.get('lineItems', []):
            rows.append({
                'Bestellnummer': order_id,
                'Transaktionsnummer': str(line.get('lineItemId', '')),
                'Artikelnummer': str(line.get('legacyItemId', '')),
                'SKU': line.get('sku', '') or '',
                'Angebotstitel': line.get('title', '') or '',
                'Auszahlung Nr.': '',
                'Betrag abzügl. Kosten': '',
                'Typ': 'Bestellung',
                'Datum': '',
                'Verkauft am': creation,
                'Anzahl': str(line.get('quantity', '')),
                'Verkauft für': (line.get('lineItemCost') or {}).get('value', ''),
                'Verpackung und Versand': ((line.get('deliveryCost') or {}).get('shippingCost') or {}).get('value', ''),
                'Gesamtbetrag': (line.get('total') or {}).get('value', ''),
            })
    return pd.DataFrame(rows)


def main():
    os.environ.setdefault('PAYMENT_BACKEND', 'supabase')
    event = os.environ.get('GITHUB_EVENT_NAME', 'workflow_dispatch')
    print(f'Ausloeser: {event}')

    import core
    from ebay_readonly import Client

    client = Client(provider=ebay_provider)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=90)
    print(f'Fenster: {iso(start)} .. {iso(end)}')

    result = client.pages('orders', 'orders', {'filter': f'creationdate:[{iso(start)}..{iso(end)}]'})
    orders = result['items']
    print(f'API-Bestellungen: {len(orders)}')

    frame = build_frame(orders)
    print(f'API-Positionen: {len(frame)}')

    added = core.import_reports([frame], core.ORDERS_DB_PATH, 'orders')
    print(json.dumps({'status': 'success', 'orders': len(orders), 'positions': len(frame), 'added': added}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
