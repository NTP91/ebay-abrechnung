"""Read-only presentation models using the existing settlement/export calculations."""
import json
import pandas as pd
import core
import position_workflow
from decimal import Decimal
from partner_export import prepare_partner_export, cents


def local_datetime(values):
    """Presentation-only conversion of UTC register timestamps to local minutes."""
    parsed = pd.to_datetime(values, utc=True, errors='coerce')
    return parsed.dt.tz_convert('Europe/Berlin').dt.strftime('%d.%m.%Y %H:%M').fillna('nicht bekannt')


def project_totals(master):
    """Cumulative settled revenue and actual net commission, including credits."""
    totals = dict(ebay=Decimal(0), evelyn=Decimal(0), patrick=Decimal(0))
    if master.empty:
        return totals
    relevant = master[master.Gruppe.isin(['Gruppe A','Gruppe B']) & ~master['Prüfhinweis'].astype(bool) & master.Art.isin(['Bestellung','Erstattung'])]
    for _, block in relevant.groupby('Partner'):
        model = prepare_partner_export(block)
        totals['ebay'] += sum(t['ebay'] for t in model['totals'].values())
        for name in ('Rechnung','Gutschriften'):
            for item in model[name]:
                evelyn = item['net'] - cents(item['net'] * Decimal('.995'))
                totals['evelyn'] += evelyn
                if block.iloc[0].Gruppe == 'Gruppe B':
                    totals['patrick'] += item['discount'] - evelyn
    return totals


def holds(raw):
    """Keep held funds and references visible, without guessing their resolution."""
    block = raw[raw.Typ.str.strip().str.casefold() == 'einbehalten'].copy()
    columns = ['Datum','Auszahlung Nr.','Bestellnummer','Transaktionsnummer','Artikelnummer','Betrag abzügl. Kosten']
    result = block[columns].copy()
    result['Status'] = 'Einbehalt · Folgebewegung abwarten'
    return result


def eligible_rows(master, states):
    if master.empty:
        return master.copy()
    unlocked = set(states.loc[states.Sperre.isna() & states.Entwurf.isna() & ~states.Status.str.contains('Prüfung', na=False), 'Auszahlung'])
    bad = set(master.loc[master['Prüfhinweis'].astype(bool), 'Auszahlung Nr.'])
    business = position_workflow.positions(master, states)
    return business[business['Auszahlung Nr.'].isin(unlocked - bad) & (business['Erlös_Brutto'] > 0) & (business.Art == 'Bestellung') & ~business['closed_at'].astype(bool) & ~business.Quellenpruefung.astype(bool)].copy()


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
    all_orders = core.read_master(core.ORDERS_DB_PATH)
    orders = all_orders[all_orders.SKU.str.split('/').str[0].str.strip() != ''].copy()
    records = []
    for _, row in raw[raw['Auszahlung Nr.'] == ''].iterrows():
        if row.Typ.strip().casefold() == 'einbehalten':
            continue
        match, issue = core.match_order(row, all_orders)
        if match is not None and not issue and not match.SKU.split('/')[0].strip():
            continue
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
    all_orders = core.read_master(core.ORDERS_DB_PATH)
    orders = all_orders[all_orders.SKU.str.split('/').str[0].str.strip() != ''].copy()
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
        match, issue = core.match_order(row, all_orders)
        if match is not None and not issue and not match.SKU.split('/')[0].strip():
            continue
        key = ('order',match.name) if match is not None and not issue else ('raw',index)
        entry = records.setdefault(key, dict(Bestellnummer=row['Bestellnummer'], Datum=row.Datum, SKU='', Produkttitel='Bestellbericht noch nicht zugeordnet', payout=False,closed=False,keys=[]))
        entry['payout'] = entry['payout'] or bool(row['Auszahlung Nr.'])
    if not business.empty:
        for _, row in business[business.Art!='Gebühr'].iterrows():
            match, issue = core.match_order(row, all_orders)
            if match is not None and not issue:
                records[('order',match.name)]['keys'].append(bool(row['closed_at']))
        for entry in records.values():
            entry['closed'] = bool(entry['payout'] and entry['keys'] and all(entry['keys']))
    held_orders = set()
    for _, row in raw[raw.Typ.str.strip().str.casefold() == 'einbehalten'].iterrows():
        match, issue = core.match_order(row, all_orders)
        if match is not None and not issue:
            held_orders.add(('order',match.name))
    for key, entry in records.items():
        entry['Partner'] = entry['SKU'].split('/')[0].strip().upper()
        if entry['Partner'].startswith('MH'):
            entry['Partner'] = 'MH'
        entry['Status'] = 'abgeschlossen' if entry['closed'] else 'Payout vorhanden' if entry['payout'] else 'Bestellung vorhanden · noch kein Payout'
        if key in held_orders and not entry['payout']:
            entry['Status'] = 'Einbehalt / Rücksendung in Klärung'
    return pd.DataFrame(records.values(), columns=['Bestellnummer','Datum','Partner','SKU','Produkttitel','Status','payout','closed','keys'])


def invoice_history():
    with core.ledger() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM payouts WHERE invoice_id IS NOT NULL ORDER BY id')]
        discarded = [dict(row) for row in db.execute('SELECT * FROM discarded_invoices ORDER BY discarded_at')]
    grouped = {}
    for row in rows:
        item = grouped.setdefault(row['invoice_id'], {'Payouts': [], 'Positionen': None, 'discarded': False, 'Status': 'Lexware-Entwurf erstellt · Zahlung separat bestätigen'})
        item['Payouts'].append(row['id'])
        if row['snapshot']:
            item['Positionen'] = len(json.loads(row['snapshot']).get('lineItems', []))
    for row in discarded:
        previous = json.loads(row['snapshot'])
        grouped[row['invoice_id']] = {'Payouts':[r['id'] for r in previous], 'Positionen':len(json.loads(previous[0]['snapshot'])['lineItems']),
                                     'discarded':True, 'Status':row['label']+' · verworfen am '+row['discarded_at']}
    return grouped
