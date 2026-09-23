"""Read-only presentation models using the existing settlement/export calculations."""
import json
import pandas as pd
import core
import api_holds
import position_workflow
from decimal import Decimal
from partner_export import prepare_partner_export, cents


# Lexware stores its technical document UUID in the payout lock.  The confirmed
# voucher number is presentation metadata; keeping it here does not alter the
# immutable payout-to-invoice binding.
LEXWARE_DOCUMENT_NUMBERS = {
    'ec17afe6-c236-4691-bfd3-996ecd604328': 'RE0090',
}


def local_datetime(values):
    """Presentation-only conversion of UTC register timestamps to local minutes."""
    parsed = pd.to_datetime(values, utc=True, errors='coerce')
    return parsed.dt.tz_convert('Europe/Berlin').dt.strftime('%d.%m.%Y %H:%M').fillna('nicht bekannt')


def project_totals(master):
    """Cumulative settled revenue and actual net commission, including credits.

    Patricks Anteil ist ausdruecklich KEINE pauschalen 3 % auf ganz Gruppe B
    mehr: item['discount'] ist der Partnerabzug, den prepare_partner_export
    aus der zentralen Konditionsquelle (partner_conditions) je Partner zieht -
    3,5 % Standard-Gruppe-B, 2,5 % fuer PM, 0,5 % fuer Gruppe A. Evelyns
    unveraenderte 0,5 % werden davon abgezogen, sodass sich pro Partner genau
    der richtige Satz ergibt (3,5-0,5 = 3,0 %; 2,5-0,5 = 2,0 %; Gruppe A
    traegt strukturell gar nichts bei). Damit steht der Satz weiterhin an
    genau einer Stelle und wird hier nicht ein zweites Mal hartkodiert.

    001/002 werden dadurch nicht rueckwirkend neu gerechnet: die historischen
    Partner behalten ihre unveraenderten Standardsaetze, und PM (der einzige
    Partner mit abweichender Kondition) existiert erst ab 2026-003.
    """
    totals = dict(ebay=Decimal(0), evelyn=Decimal(0), patrick=Decimal(0))
    if master.empty:
        return totals
    relevant = master[master.Gruppe.isin(['Gruppe A','Gruppe B']) & ~master['Prüfhinweis'].astype(bool) & master.Art.isin(['Bestellung','Erstattung']) & ~api_holds.mask(master)]
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
    # A payout-level review status is informational. Only a durable invoice
    # reservation locks the whole payout; business gates are evaluated per row.
    unlocked = set(states.loc[states.Sperre.isna() & states.Entwurf.isna(), 'Auszahlung'])
    paid = {payout for payout, block in master.groupby('Auszahlung Nr.') if core.payout_receipt_confirmed(block)}
    business = position_workflow.positions(master, states)
    # Positionen einer 2026-003+-Runde werden direkt Partner -> Evelyn
    # abgerechnet und sind damit nie "neu fuer Lexware bereit" (siehe
    # neutral_round_keys) - sonst droht Doppelabrechnung.
    return business[business['Auszahlung Nr.'].isin(unlocked & paid) & (business['Erlös_Brutto'] > 0) & (business.Art == 'Bestellung') & ~business['Prüfhinweis'].astype(bool) & ~business.get('Neutralisiert',False) & ~business['closed_at'].astype(bool) & ~business.Quellenpruefung.astype(bool) & ~api_holds.mask(business) & ~business.position_key.isin(neutral_round_keys())].copy()


def lexware_create_ready(selected, totals, api_key, confirmations):
    """Pure UI gate; durable invoice locks are still enforced in core."""
    return bool(selected and totals and str(api_key).strip() and all(confirmations))


def partner_rows(business):
    """Open partner settlement rows, with Group-B refunds applied once.

    Group B uses variant B: an eligible original sale remains positive and
    every linked refund remains a separate negative event.  The sale is never
    reduced or removed because of that same refund.  Evelyn selection keeps
    using core.apply_open_refunds independently and is deliberately untouched.

    Group A retains its established behavior; its special cases are outside
    this Group-B correction.
    """
    if business.empty:
        return business.copy()
    ready_a = business[(business.Gruppe == 'Gruppe A') & business.partner_ready].copy()
    committed_a = ready_a.reviewed_at.astype(bool) if not ready_a.empty else pd.Series(False, index=ready_a.index)
    fresh_a, settled_a = ready_a[~committed_a], ready_a[committed_a]
    open_a = core.apply_open_refunds(fresh_a) if not fresh_a.empty else fresh_a
    refunds_a = core.linked_refunds(business, fresh_a.index) if not fresh_a.empty else business.iloc[0:0]

    valid_b = (
        (business.Gruppe == 'Gruppe B') & (business.Art == 'Bestellung')
        & (business['Erlös_Brutto'] > 0) & ~business['Prüfhinweis'].astype(bool)
        & ~business.Quellenpruefung.astype(bool) & ~api_holds.mask(business)
        & ~business.closed_at.astype(bool) & ~business.paid_at.astype(bool)
        # A position documented as paid_without_invoice_at (e.g. MH's historical
        # bulk case) is already settled - Group A's own partner_ready column
        # already excludes it (position_workflow.positions()'s own definition
        # of "ready"), but this Group-B filter is deliberately its own,
        # narrower reimplementation (it must still include a fully neutralized
        # sale/refund pair, which partner_ready itself excludes) and had never
        # picked up this specific exclusion - the one still-missing condition
        # from the manuscript's own "must never become payable again" list.
        & ~business[position_workflow.PAID_WITHOUT_INVOICE].astype(bool)
    )
    sales_b = business[valid_b].copy()
    linked = core.refund_links(business)
    normal_refund_indices = {refund for refund, sale in linked.items() if sale in set(sales_b.index)}
    committed_b = business[
        (business.Gruppe == 'Gruppe B') & (business.Art == 'Bestellung')
        & (business.reviewed_at.astype(bool) | business.paid_at.astype(bool) | business.closed_at.astype(bool)
           | business[position_workflow.PAID_WITHOUT_INVOICE].astype(bool))
    ]
    historical_refund_indices = {refund for refund, sale in linked.items() if sale in set(committed_b.index)}
    refund_indices = sorted(normal_refund_indices | historical_refund_indices)
    refunds_b = business.loc[refund_indices].copy() if refund_indices else business.iloc[0:0].copy()
    if not refunds_b.empty:
        open_event = (
            ~refunds_b['Prüfhinweis'].astype(bool) & ~refunds_b.Quellenpruefung.astype(bool)
            & ~api_holds.mask(refunds_b) & ~refunds_b.reviewed_at.astype(bool)
            & ~refunds_b.paid_at.astype(bool) & ~refunds_b.closed_at.astype(bool)
        )
        refunds_b = refunds_b[open_event].copy()
        identities = refunds_b.apply(
            lambda row: core.clean(row.get('Transaktionsnummer')) or row.position_key, axis=1)
        if identities.duplicated().any():
            raise ValueError('Refund-ID mehrfach im offenen Partner-Settlement vorhanden.')
        refunds_b['Refund_ID'] = identities
        invoice_by_position = {}
        invoice_by_payout = {}
        with core.ledger() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='partner_invoice_positions'").fetchone() \
                    and db.execute("SELECT 1 FROM sqlite_master WHERE name='partner_invoices'").fetchone():
                records = {row['id']: json.loads(row['record']) for row in db.execute('SELECT id,record FROM partner_invoices')}
                for row in db.execute('SELECT position_key,invoice_id FROM partner_invoice_positions'):
                    record = records.get(row['invoice_id'], {})
                    invoice_by_position[row['position_key']] = record.get('invoice_number') or record.get('file_name') or row['invoice_id']
            for row in db.execute('SELECT id,invoice_id FROM payouts WHERE invoice_id IS NOT NULL'):
                invoice_by_payout[row['id']] = LEXWARE_DOCUMENT_NUMBERS.get(row['invoice_id'], row['invoice_id'])
        origins = [business.loc[linked[index]] for index in refunds_b.index]
        refunds_b['Ursprungs_Payout'] = [str(row['Auszahlung Nr.']) for row in origins]
        refunds_b['Refund_Payout'] = refunds_b['Auszahlung Nr.'].astype(str)
        refunds_b['Ursprungs_Abrechnung'] = [
            invoice_by_position.get(row.position_key)
            or (invoice_by_payout.get(str(row['Auszahlung Nr.'])) if row.Lexware_uebertragen else None)
            or 'offene Partnerabrechnung'
            for row in origins
        ]
        already_paid = [bool(row.reviewed_at or row.paid_at or row.closed_at
                              or row[position_workflow.PAID_WITHOUT_INVOICE]) for row in origins]
        refunds_b['Refund_Status'] = [
            'Später Refund · historische Partnerrechnung unverändert' if paid
            else 'Offener Refund · einmalig im nächsten Partner-Settlement'
            for paid in already_paid
        ]
        # Exposed for prepare_partner_export: whether the *original sale's*
        # own reviewed/paid/closed status is already true, i.e. the partner
        # was already paid for it in an earlier run - not derived from mere
        # absence of that sale row in the export slice, so it stays correct
        # even when prepare_partner_export is called with an arbitrary subset.
        refunds_b['Bereits_An_Partner_Bezahlt'] = already_paid

    return pd.concat([open_a, settled_a, refunds_a, sales_b, refunds_b]).sort_index()


def partner_refund_cases(business):
    """Refunds linked to an already reviewed/paid/closed position, for every partner.

    These must not be silently netted against new positions; history (the
    already-committed sale row, and any invoice already matched against it)
    stays untouched. Returned separately so they can be surfaced as their
    own repayment/credit case.
    """
    if business.empty or not {'Art', 'reviewed_at', 'paid_at', 'closed_at'}.issubset(business.columns):
        return business.iloc[0:0].copy()
    committed = business[(business.Art == 'Bestellung')
                          & (business.reviewed_at.astype(bool) | business.paid_at.astype(bool) | business.closed_at.astype(bool))]
    if committed.empty:
        return business.iloc[0:0].copy()
    return core.linked_refunds(business, committed.index)


def partner_summary(rows):
    records = []
    if rows.empty:
        return pd.DataFrame(records)
    for partner, block in rows.groupby('Partner'):
        sales = block[block.Art == 'Bestellung'] if 'Art' in block else block
        totals = prepare_partner_export(block)['totals']['Rechnung']
        refund_totals = prepare_partner_export(block)['totals']['Gutschriften']
        if 'reviewed_at' in block and block.reviewed_at.astype(bool).any():
            import partner_invoices
            reviewed = block[block.reviewed_at.astype(bool)]
            pending = block[~block.reviewed_at.astype(bool)]
            totals['gross'] = partner_invoices.confirmed_payment_total(reviewed)
            if not pending.empty:
                totals['gross'] += prepare_partner_export(pending)['totals']['Rechnung']['gross']
        records.append({'Partner': partner, 'Positionen': len(sales), 'eBay-Brutto': float(totals['ebay']),
                        'Rabatt netto': float(totals['discount']), 'Rechnungsbetrag': float(totals['gross']),
                        'Refunds': float(refund_totals['gross']),
                        'Verbleibender Anspruch': float(totals['gross'] + refund_totals['gross'])})
    return pd.DataFrame(records)


def open_positions(raw, all_orders=None):
    all_orders = core.read_master(core.ORDERS_DB_PATH) if all_orders is None else all_orders
    orders = all_orders[all_orders.SKU.str.split('/').str[0].str.strip() != ''].copy()
    order_index = core.order_match_index(all_orders)
    records = []
    for _, row in raw[raw['Auszahlung Nr.'] == ''].iterrows():
        if row.Typ.strip().casefold() == 'einbehalten':
            continue
        match, issue = core.match_order(row, all_orders, order_index)
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


def order_catalogue(raw, business, all_orders=None):
    """Union of order-report positions and unmatched order transactions; no invented payouts."""
    all_orders = core.read_master(core.ORDERS_DB_PATH) if all_orders is None else all_orders
    orders = all_orders[all_orders.SKU.str.split('/').str[0].str.strip() != ''].copy()
    order_index = core.order_match_index(all_orders)
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
        match, issue = core.match_order(row, all_orders, order_index)
        if match is not None and not issue and not match.SKU.split('/')[0].strip():
            continue
        key = ('order',match.name) if match is not None and not issue else ('raw',index)
        entry = records.setdefault(key, dict(Bestellnummer=row['Bestellnummer'], Datum=row.Datum, SKU='', Produkttitel='Bestellbericht noch nicht zugeordnet', payout=False,closed=False,keys=[]))
        entry['payout'] = entry['payout'] or bool(row['Auszahlung Nr.'])
    if not business.empty:
        for _, row in business[business.Art!='Gebühr'].iterrows():
            match, issue = core.match_order(row, all_orders, order_index)
            if match is not None and not issue:
                records[('order',match.name)]['keys'].append(bool(row['closed_at']))
        for entry in records.values():
            entry['closed'] = bool(entry['payout'] and entry['keys'] and all(entry['keys']))
    held_orders = set()
    for _, row in raw[raw.Typ.str.strip().str.casefold() == 'einbehalten'].iterrows():
        match, issue = core.match_order(row, all_orders, order_index)
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


def _snapshot_total(payload):
    """Reproduce the existing Lexware column calculation from its snapshot."""
    total_after = previous_tax = Decimal(0)
    for item in payload.get('lineItems', []):
        unit = item.get('unitPrice', {})
        rate = Decimal(str(item.get('discountPercentage', 0))) / Decimal(100)
        quantity = Decimal(str(item.get('quantity', 1)))
        if 'grossAmount' in unit:
            gross = Decimal(str(unit['grossAmount'])) * quantity
            total_after += cents(gross * (Decimal(1) - rate))
            continue
        net = Decimal(str(unit.get('netAmount', 0))) * quantity
        after = cents(net * (Decimal(1) - rate))
        total_after += after
        previous_tax = cents(total_after * Decimal(str(unit.get('taxRatePercentage', 0))) / Decimal(100))
    return cents(total_after + previous_tax)


def invoice_history():
    """Return Evelyn vouchers while preserving the established no-argument API."""
    business = position_workflow.positions()
    with core.ledger() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM payouts WHERE invoice_id IS NOT NULL ORDER BY id')]
        discarded = [dict(row) for row in db.execute('SELECT * FROM discarded_invoices ORDER BY discarded_at')]
    grouped = {}
    for row in rows:
        item = grouped.setdefault(row['invoice_id'], {
            'Belegnummer': LEXWARE_DOCUMENT_NUMBERS.get(row['invoice_id'], row['invoice_id']),
            'Payouts': [], 'Positionen': None, 'Datum': '', 'Betrag': None,
            'discarded': False, 'Status': 'fakturiert',
            'Zahlungsstatus': 'Zahlung von Evelyn offen', 'Abschlussstatus': 'offen',
        })
        item['Payouts'].append(row['id'])
        if row['snapshot']:
            payload = json.loads(row['snapshot'])
            item['Positionen'] = len(payload.get('lineItems', []))
            item['Datum'] = payload.get('voucherDate', '')
            item['Betrag'] = _snapshot_total(payload)
    for row in discarded:
        previous = json.loads(row['snapshot'])
        payload = json.loads(previous[0]['snapshot'])
        grouped[row['invoice_id']] = {
            'Belegnummer': row['label'], 'Payouts':[r['id'] for r in previous],
            'Positionen':len(payload['lineItems']), 'Datum':payload.get('voucherDate', ''),
            'Betrag':_snapshot_total(payload), 'discarded':True,
            'Status':row['label']+' · verworfen am '+row['discarded_at'],
            'Zahlungsstatus':'nicht zutreffend', 'Abschlussstatus':'verworfen',
        }
    if business is not None and not business.empty:
        for item in grouped.values():
            if item['discarded']:
                continue
            bound = business[
                business['Auszahlung Nr.'].isin(item['Payouts'])
                & business.Lexware_uebertragen
                & (business.Art == 'Bestellung')
                & (business['Erlös_Brutto'] > 0)
            ]
            complete = len(bound) == item['Positionen'] and not bound.empty
            received = complete and bound.received_at.astype(bool).all()
            closed = complete and bound.closed_at.astype(bool).all()
            item['Zahlungsstatus'] = 'Zahlung von Evelyn erhalten' if received else 'Zahlung von Evelyn offen'
            item['Abschlussstatus'] = 'abgeschlossen' if closed else 'offen'
    return grouped


def neutral_round_keys():
    """Position keys already claimed by a 2026-003+ round (source_kind=
    'neutral_weekly'). Those positions are settled directly partner -> Evelyn
    and must never reappear in the historical Lexware/Evelyn bulk-invoice
    flow - otherwise the same revenue could be billed twice. core.ledger()
    always runs group_b_rounds.initialize(), so both tables exist."""
    with core.ledger() as db:
        return {row[0] for row in db.execute(
            "SELECT gbp.position_key FROM group_b_round_positions gbp "
            "JOIN group_b_rounds gr ON gr.id = gbp.round_id "
            "WHERE gr.source_kind = 'neutral_weekly'")}


def evelyn_overview(business, eligible, invoices):
    """Disjoint read-only buckets for the next Group-B Evelyn settlement.

    Refunds are matched automatically to their order line (core.refund_offset).
    A pending position that a matching refund has fully cancelled before any
    Evelyn payment carries no open claim and is dropped from this run; a
    partial refund reduces its amount instead. Positions already transferred
    to Lexware (bound) are never changed retroactively - a refund arriving
    after payment only surfaces as a separate 'refund_cases' entry for manual
    follow-up (repayment/credit note), leaving history untouched.
    """
    if business.empty or not {'Gruppe','Art','Erlös_Brutto','closed_at','Lexware_uebertragen','position_key'}.issubset(business.columns):
        empty = business.iloc[0:0].copy()
        return {'bound':empty, 'ready':empty, 'review':empty, 'held':empty,
                'new_ready':empty, 'new_review':empty, 'new_held':empty,
                'prior_held':empty, 'refund_cases':empty, 'new_payouts':[], 'total':Decimal(0)}
    group_b = business[
        (business.Gruppe == 'Gruppe B') & (business.Art == 'Bestellung')
        & (business['Erlös_Brutto'] > 0) & ~business.get('Neutralisiert',False)
    ].copy()
    bound = group_b[group_b.Lexware_uebertragen].copy()
    erstattet_bound = bound.get('Erstattet_Brutto')
    if erstattet_bound is None or bound.empty:
        refund_cases = bound.iloc[0:0].copy()
    else:
        refund_cases = bound[erstattet_bound.map(lambda value: Decimal(str(value)) < Decimal('0.00'))].copy()
    # Anti-join against the 2026-003+ rounds: a position a neutral weekly
    # round already claims is settled via the direct partner->Evelyn invoice
    # and is therefore not "new/eligible" here any more - excluded at the
    # data level, not merely by a disabled button. `bound` (already
    # transmitted historical positions) is deliberately untouched.
    pending_all = group_b[~group_b.Lexware_uebertragen & ~group_b.closed_at.astype(bool)
                          & ~group_b.position_key.isin(neutral_round_keys())].copy()
    pending = core.apply_open_refunds(pending_all)
    held_mask = api_holds.mask(pending)
    held = pending[held_mask].copy()
    eligible_keys = set(eligible.position_key) if not eligible.empty else set()
    ready = pending[pending.position_key.isin(eligible_keys) & ~held_mask].copy()
    review = pending[~pending.position_key.isin(eligible_keys) & ~held_mask].copy()
    invoice_payouts = {payout for item in invoices.values() if not item['discarded'] for payout in item['Payouts']}
    new_payouts = sorted(set(pending['Auszahlung Nr.']) - invoice_payouts)
    new_mask = pending['Auszahlung Nr.'].isin(new_payouts)
    new_keys = set(pending.loc[new_mask, 'position_key'])
    new_ready = ready[ready.position_key.isin(new_keys)].copy()
    new_review = review[review.position_key.isin(new_keys)].copy()
    new_held = held[held.position_key.isin(new_keys)].copy()
    prior_held = held[~held.position_key.isin(new_keys)].copy()
    total = Decimal(0)
    if not new_ready.empty:
        total = prepare_partner_export(new_ready, statement_type='group_b_evelyn')['totals']['Rechnung']['gross']
    return {'bound':bound, 'ready':ready, 'review':review, 'held':held,
            'new_ready':new_ready, 'new_review':new_review, 'new_held':new_held,
            'prior_held':prior_held, 'refund_cases':refund_cases, 'new_payouts':new_payouts, 'total':total}
