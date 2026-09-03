"""Read-only presentation models using the existing settlement/export calculations."""
import json
import pandas as pd
import core
from partner_export import prepare_partner_export


def eligible_rows(master, states):
    if master.empty:
        return master.copy()
    unlocked = set(states.loc[states.Sperre.isna() & states.Entwurf.isna() & ~states.Status.str.contains('Prüfung', na=False), 'Auszahlung'])
    bad = set(master.loc[master['Prüfhinweis'].astype(bool), 'Auszahlung Nr.'])
    return master[master['Auszahlung Nr.'].isin(unlocked - bad) & (master['Erlös_Brutto'] > 0) & (master.Art == 'Bestellung')].copy()


def partner_summary(rows):
    records = []
    if rows.empty:
        return pd.DataFrame(records)
    for partner, block in rows.groupby('Partner'):
        totals = prepare_partner_export(block)['totals']['Rechnung']
        records.append({'Partner': partner, 'Positionen': len(block), 'eBay-Brutto': float(totals['ebay']),
                        'Rabatt netto': float(totals['discount']), 'Rechnungsbetrag': float(totals['gross'])})
    return pd.DataFrame(records)


def open_positions(raw):
    orders = core.read_master(core.ORDERS_DB_PATH)
    records = []
    for _, row in raw[raw['Auszahlung Nr.'] == ''].iterrows():
        match, issue = core.match_order(row, orders)
        sku = match['SKU'] if match is not None and not issue else ''
        partner = sku.split('/')[0].strip().upper()
        if partner.startswith('MH'):
            partner = 'MH'
        records.append({'Bestellnummer': row['Bestellnummer'], 'Datum': row['Datum'], 'Partner': partner,
                        'SKU': sku, 'Produkttitel': match['Angebotstitel'] if match is not None and not issue else 'Bestellbericht noch nicht zugeordnet',
                        'Status': 'Noch kein Payout'})
    return pd.DataFrame(records)


def order_metrics(raw):
    def is_order(row):
        if not row['Bestellnummer'] or any(x in row['Typ'].lower() for x in ('erstattung', 'refund', 'gebühr', 'fee')):
            return False
        try:
            return core.parse_money(row['Betrag abzügl. Kosten']) >= 0
        except ValueError:
            return not row['Auszahlung Nr.']
    orders = raw[raw.apply(is_order, axis=1)] if not raw.empty else raw
    assigned = int((orders['Auszahlung Nr.'] != '').sum())
    return len(orders), assigned, len(orders) - assigned


def invoice_history():
    with core.ledger() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM payouts WHERE invoice_id IS NOT NULL ORDER BY id')]
    grouped = {}
    for row in rows:
        item = grouped.setdefault(row['invoice_id'], {'Payouts': [], 'Positionen': None, 'Status': row['status']})
        item['Payouts'].append(row['id'])
        if row['snapshot']:
            item['Positionen'] = len(json.loads(row['snapshot']).get('lineItems', []))
    return grouped
