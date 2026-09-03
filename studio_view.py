"""Read-only presentation models using the existing settlement/export calculations."""
import json
import pandas as pd
import core
import position_workflow
from partner_export import prepare_partner_export


def eligible_rows(master, states):
    if master.empty:
        return master.copy()
    unlocked = set(states.loc[states.Sperre.isna() & states.Entwurf.isna() & ~states.Status.str.contains('Prüfung', na=False), 'Auszahlung'])
    bad = set(master.loc[master['Prüfhinweis'].astype(bool), 'Auszahlung Nr.'])
    business = position_workflow.positions(master, states)
    return business[business['Auszahlung Nr.'].isin(unlocked - bad) & (business['Erlös_Brutto'] > 0) & (business.Art == 'Bestellung') & ~business['closed_at'].astype(bool)].copy()


def partner_rows(business):
    return business[business.partner_ready].copy() if not business.empty else business.copy()


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


def order_catalogue(raw, business):
    """Union of order-report positions and unmatched order transactions; no invented payouts."""
    orders = core.read_master(core.ORDERS_DB_PATH)
    records = {}
    for index, row in orders.iterrows():
        records[('order',index)] = dict(Bestellnummer=row['Bestellnummer'], Datum=next((core.clean(row.get(k,'')) for k in ('Verkauft am','Bestelldatum','Datum') if core.clean(row.get(k,''))),''),
                                     SKU=row.SKU, Produkttitel=row.Angebotstitel, payout=False, closed=False, keys=[])
    for index, row in raw.iterrows():
        if not row['Bestellnummer'] or any(word in row.Typ.lower() for word in ('erstattung','refund','gebühr','fee')):
            continue
        try:
            if core.parse_money(row['Betrag abzügl. Kosten']) < 0:
                continue
        except ValueError:
            pass
        match, issue = core.match_order(row, orders)
        key = ('order',match.name) if match is not None and not issue else ('raw',index)
        entry = records.setdefault(key, dict(Bestellnummer=row['Bestellnummer'], Datum=row.Datum, SKU='', Produkttitel='Bestellbericht noch nicht zugeordnet', payout=False,closed=False,keys=[]))
        entry['payout'] = entry['payout'] or bool(row['Auszahlung Nr.'])
    if not business.empty:
        for _, row in business[business.Art!='Gebühr'].iterrows():
            match, issue = core.match_order(row, orders)
            if match is not None and not issue:
                records[('order',match.name)]['keys'].append(bool(row['closed_at']))
        for entry in records.values():
            entry['closed'] = bool(entry['payout'] and entry['keys'] and all(entry['keys']))
    for entry in records.values():
        entry['Partner'] = entry['SKU'].split('/')[0].strip().upper()
        if entry['Partner'].startswith('MH'):
            entry['Partner'] = 'MH'
        entry['Status'] = 'abgeschlossen' if entry['closed'] else 'Payout vorhanden' if entry['payout'] else 'Bestellung vorhanden · noch kein Payout'
    return pd.DataFrame(records.values(), columns=['Bestellnummer','Datum','Partner','SKU','Produkttitel','Status','payout','closed','keys'])


def invoice_history():
    with core.ledger() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM payouts WHERE invoice_id IS NOT NULL ORDER BY id')]
    grouped = {}
    for row in rows:
        item = grouped.setdefault(row['invoice_id'], {'Payouts': [], 'Positionen': None, 'Status': 'Lexware-Entwurf erstellt · Zahlung separat bestätigen'})
        item['Payouts'].append(row['id'])
        if row['snapshot']:
            item['Positionen'] = len(json.loads(row['snapshot']).get('lineItems', []))
    return grouped
