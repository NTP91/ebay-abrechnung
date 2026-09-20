"""Partner-only exports; no changes to import, settlement, or invoice API logic.

The versioned template is authored with artifact-tool. At runtime only the
standard-library OpenXML writer below fills it, without a Node dependency.
"""
import copy
import io
import json
import math
import os
import re
import textwrap
import zipfile
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from xml.etree import ElementTree as ET

import core

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
ET.register_namespace('', NS)
TAG = lambda name: f'{{{NS}}}{name}'
TEMPLATE = Path(__file__).with_name('templates') / 'partner.xlsx'
HEADER_ROW = 14
FIRST_ROW = 15
TAX = Decimal('.19')
CENT = Decimal('.01')
# Internal sheet keys ('Rechnung'/'Gutschriften'/'HistorischeGutschriften') drive
# the data model and are never renamed - only the visible sheet tab/title text
# changes, so a merchant does not read "Gutschriften" (which implies a formal
# credit note already issued) for refunds on a position they may not have been
# paid for yet. HistorischeGutschriften holds refunds whose original sale was
# already paid out to the partner in an earlier run (Fall B): the internal
# netting used by Gutschriften no longer applies once the money is already
# with the partner, so these stay their own open-repayment tab, always present
# (even empty) so the export shape never depends on whether such a case exists.
DISPLAY_NAMES = {'Rechnung': 'Rechnung', 'Gutschriften': 'Erstattungen-Abzüge',
                  'HistorischeGutschriften': 'Offene Rückforderungen'}
# GESAMTABRECHNUNG (Rechnung sheet only): sales basis, partner discount, regular
# claim, and the already-known refunds/deductions from Gutschriften (Tab 2,
# refunds on a not-yet-paid sale) netted into one FINALER RECHNUNGSBETRAG - the
# exact amount the partner may invoice, so nobody has to subtract Tab 1 and
# Tab 2 by hand. HistorischeGutschriften (Tab 3: refunds on a sale already paid
# out in an earlier run) is a separate, not-yet-settled repayment case and is
# deliberately never part of this sum. Shared by _closing_statement_rows and
# _fill_sheet so their row math can never drift.
FINALE_LINE_COUNT = 4
MONTHS = {
    'jan': 1, 'feb': 2, 'mär': 3, 'märz': 3, 'mar': 3, 'mrz': 3,
    'apr': 4, 'mai': 5, 'may': 5, 'jun': 6, 'jul': 7, 'aug': 8,
    'sep': 9, 'sept': 9, 'okt': 10, 'oct': 10, 'nov': 11, 'dez': 12, 'dec': 12,
}


def report_date(value):
    """Parse German/English eBay dates without relying on the machine locale."""
    text = core.clean(value)
    if not text:
        return None
    match = re.fullmatch(r'(\d{1,2})[.\s/-]+([A-Za-zÄÖÜäöü]+)[.\s/-]+(\d{2}|\d{4})', text)
    if match:
        day, month, year = match.groups()
        if month.lower() in MONTHS:
            year = int(year) + (2000 if len(year) == 2 else 0)
            return datetime(year, MONTHS[month.lower()], int(day))
    for fmt in ('%d.%m.%Y', '%d-%m-%Y', '%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise ValueError(f'Datum im Quellbericht nicht lesbar: {text}')


def cents(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def format_euro(value):
    """German-formatted currency text for header/summary cells that mix a label and value."""
    return f'{value:,.2f} €'.replace(',', 'X').replace('.', ',').replace('X', '.')


def recipient_details(key):
    """Editable recipient master data; rates are deliberately not configurable."""
    path = Path(os.environ.get('PAYMENT_RECIPIENTS_PATH', Path(__file__).with_name('billing_recipients.json')))
    try:
        import supabase_store
        config = (supabase_store.get_json('config/billing_recipients.json')[0] if supabase_store.enabled()
                  else json.loads(path.read_text(encoding='utf-8-sig')))
        if config['schema_version'] != 1:
            raise ValueError('Nicht unterstützte Empfänger-Stammdatenversion.')
        recipient = config['recipients'][key]
        address = recipient['address']
        lines = [address.get('name_addition', ''), address.get('street', ''),
                 ' '.join(filter(None, [address.get('postal_code', ''), address.get('city', '')])),
                 address.get('country', '')]
        return recipient['name'], '\n'.join(line for line in lines if line) or 'Rechnungsadresse noch nicht hinterlegt'
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('Empfänger-Stammdaten fehlen oder sind ungültig.') from exc


def calculate_sheet(items, rate):
    """Net line amounts rounded to cents; VAT on their sum (Lexware column method).

    Gross per row includes the change in cumulative VAT. This allocates the
    column tax in cents so displayed row gross and invoice gross sum identically.
    No amount is derived from the eBay control column.
    """
    total_net = total_after = previous_tax = total_ebay = Decimal(0)
    for item in items:
        after = cents(item['net'] * (1 - rate))
        total_after += after
        tax_to_date = cents(total_after * TAX)
        item['net_after'] = after
        item['discount'] = item['net'] - after
        item['gross'] = after + tax_to_date - previous_tax
        item['_running_net'] = total_after
        item['_tax_to_date'] = tax_to_date
        total_net += item['net']
        total_ebay += item['ebay']
        previous_tax = tax_to_date
    gross = total_after + previous_tax
    return dict(net=cents(total_net), discount=cents(total_net-total_after),
                net_after=cents(total_after), tax=previous_tax, gross=cents(gross),
                ebay=cents(total_ebay), gross_discount=cents(total_ebay-gross))


def calculate_partner_variant_b(items, rate, refunds=False):
    """Apply the existing calculation once per independent refund event.

    Unrefunded sales retain the established combined-column calculation.
    A sale participating in a refund pair and every refund event are calculated
    independently with that same formula, so a full sale/refund pair cancels
    cent-for-cent and cannot acquire a second batch-rounding effect.
    """
    regular = [] if refunds else [item for item in items if not item.get('_refund_pair')]
    independent = items if refunds else [item for item in items if item.get('_refund_pair')]
    parts = []
    if regular:
        for item in regular:
            item['_independent'] = False
        parts.append(calculate_sheet(regular, rate))
    for item in independent:
        item['_independent'] = True
        parts.append(calculate_sheet([item], rate))
    if not parts:
        return calculate_sheet([], rate)
    keys = ('net', 'discount', 'net_after', 'tax', 'gross', 'ebay', 'gross_discount')
    return {key: cents(sum((part[key] for part in parts), Decimal(0))) for key in keys}


def prepare_partner_export(rows, payouts=None, orders=None, statement_type='partner'):
    """Enrich only the export, resolving original transaction and order fields."""
    if statement_type not in ('partner', 'group_b_evelyn'):
        raise ValueError('Unbekannte Abrechnungsart.')
    if rows.empty or rows['Gruppe'].nunique() != 1 or (statement_type == 'partner' and rows['Partner'].nunique() != 1):
        raise ValueError('Partnerexport benötigt genau einen Partner und eine Gruppe.')
    partner, group = rows.iloc[0]['Partner'], rows.iloc[0]['Gruppe']
    if 'Neutralisiert' in rows and rows.Neutralisiert.astype(bool).any():
        links = core.refund_links(rows)
        paired = set(links) | set(links.values())
        neutralized = set(rows.index[rows.Neutralisiert.astype(bool)])
        if statement_type != 'partner' or group != 'Gruppe B' or not neutralized.issubset(paired):
            raise ValueError('Vollständig neutralisierte/stornierte Positionen dürfen nur als vollständiges Verkauf-/Refund-Paar in die Gruppe-B-Partnerabrechnung.')
    if statement_type == 'group_b_evelyn' and group != 'Gruppe B':
        raise ValueError('Die Gesamtübersicht an Evelyn darf nur Gruppe B enthalten.')
    if group not in ('Gruppe A', 'Gruppe B') or rows['Prüfhinweis'].astype(bool).any():
        raise ValueError('Partnerexport enthält ungeklärte Zuordnungen.')
    payouts = core.read_master(core.PAYOUTS_DB_PATH) if payouts is None else payouts
    orders = core.read_master(core.ORDERS_DB_PATH) if orders is None else orders
    rate = Decimal('.005') if group == 'Gruppe A' or statement_type == 'group_b_evelyn' else Decimal('.035')
    recipient, address = recipient_details('evelyn' if rate == Decimal('.005') else 'patrick')
    if statement_type == 'group_b_evelyn':
        partner = 'Alle Gruppe-B-Partner: ' + ', '.join(sorted(rows['Partner'].unique()))
    result = {'partner': partner, 'group': group, 'rate': rate, 'payouts': {},
              'recipient': recipient, 'address': address, 'statement_type': statement_type,
              'Rechnung': [], 'Gutschriften': [], 'HistorischeGutschriften': []}
    refund_links = core.refund_links(rows)
    refund_sales = set(refund_links.values())
    # Precomputed once (O(m) groupby) instead of re-scanning the full orders/
    # payouts tables per row (O(n*m)) - both tables only grow with history, so
    # the per-row scan cost was the export's dominant cost on large accounts.
    order_index = core.order_match_index(orders)
    payout_groups = payouts.groupby(
        ['Auszahlung Nr.', 'Bestellnummer', 'Transaktionsnummer', 'Artikelnummer'], sort=False, dropna=False)
    for row_index, row in rows.iterrows():
        if row['Art'] not in ('Bestellung', 'Erstattung'):
            continue
        match, issue = core.match_order(row, orders, order_index)
        if issue or match is None or not core.clean(match['Angebotstitel']):
            raise ValueError('Partnerexport benötigt den eindeutig zugeordneten Bestellbericht-Titel.')
        payout_group_key = (row['Auszahlung Nr.'], row['Bestellnummer'], row['Transaktionsnummer'], row['Artikelnummer'])
        candidates = (payout_groups.get_group(payout_group_key)
                      if payout_group_key in payout_groups.groups else payouts.iloc[0:0])
        base = Decimal(str(row.get('Erlös_Brutto_Original', row['Erlös_Brutto'])))
        candidates = candidates[candidates['Betrag abzügl. Kosten'].map(core.parse_money) == base]
        metadata = set()
        for _, raw in candidates.iterrows():
            gross = core.clean(raw.get('Transaktionsbetrag (inkl. Kosten)', ''))
            if not gross or gross == '--':
                raise ValueError('Ursprünglicher eBay-Bruttobetrag fehlt im Payout-Bericht.')
            metadata.add((report_date(raw.get('Auszahlungsdatum', '')), core.parse_money(gross)))
        if len(metadata) != 1:
            raise ValueError('Ursprüngliche eBay-Abrechnungstransaktion nicht eindeutig zugeordnet.')
        payout_date, original_gross = metadata.pop()
        payout_id = str(row['Auszahlung Nr.'])
        if payout_id in result['payouts'] and result['payouts'][payout_id] != payout_date:
            raise ValueError('Widersprüchliche Auszahlungsdaten im Payout-Bericht.')
        result['payouts'][payout_id] = payout_date
        order_date = next((core.clean(match.get(key, '')) for key in ('Verkauft am', 'Bestelldatum', 'Datum')
                           if core.clean(match.get(key, ''))), '')
        net = Decimal(str(row['eBay_Netto']))
        item = {
            'date': report_date(order_date), 'order': str(row['Bestellnummer']),
            'article': str(match['Angebotstitel']),
            # Bestellnummer is deliberately repeated here (own column too):
            # every export position must be independently verifiable against
            # eBay order and payout from the Zusatztext alone.
            'extra': 'eBay-Bestellnummer: ' + str(row['Bestellnummer']) + '\nSKU: ' + str(match['SKU']),
            'net': net, 'ebay': original_gross,
        }
        is_refund = base < 0 or row['Art'] == 'Erstattung'
        item['finance_id'] = str(row['Transaktionsnummer'])
        item['payout_id'] = payout_id
        item['_refund_pair'] = row_index in refund_sales
        if is_refund:
            item['refund_date'] = report_date(row.get('Datum'))
            # Fall B: studio_view.partner_rows already determined (from the
            # original sale's own reviewed/paid/closed status, not from
            # whether that sale happens to be included in this call's rows)
            # that the partner was already paid for it in an earlier run.
            # Default False - callers that never computed this (Gruppe A,
            # group_b_evelyn, ad-hoc slices) keep the original single-bucket
            # behavior unchanged. Fall A nets internally as before.
            # NaN-safe: a row sliced out of studio_view.partner_rows()'s
            # concatenated frame (e.g. a Gruppe-A refund, which never gets
            # this column set) reads back as float('nan'), and bool(nan) is
            # True in Python - the x==x check below is False only for NaN,
            # so an unset value still defaults to False as documented above.
            paid_out_value = row.get('Bereits_An_Partner_Bezahlt', False)
            item['_paid_out'] = bool(paid_out_value) and paid_out_value == paid_out_value
            bucket = 'HistorischeGutschriften' if item['_paid_out'] else 'Gutschriften'
        else:
            item['_paid_out'] = False
            bucket = 'Rechnung'
        result[bucket].append(item)
    if statement_type == 'partner' and group == 'Gruppe B':
        result['totals'] = {
            'Rechnung': calculate_partner_variant_b(result['Rechnung'], rate),
            'Gutschriften': calculate_partner_variant_b(result['Gutschriften'], rate, refunds=True),
            'HistorischeGutschriften': calculate_partner_variant_b(result['HistorischeGutschriften'], rate, refunds=True),
        }
    else:
        result['totals'] = {name: calculate_sheet(result[name], rate)
                             for name in ('Rechnung', 'Gutschriften', 'HistorischeGutschriften')}
    # Sales keep the compact reference text; refunds add the audit metadata that
    # makes each separate negative event traceable to its original settlement.
    for item in result['Rechnung']:
        item['extra'] += '\nPayout: ' + item['payout_id']
    for item in result['Gutschriften'] + result['HistorischeGutschriften']:
        # Bestellnummer is deliberately repeated (see above); Bestelldatum and
        # internal workflow/status/amount bookkeeping stay out of the visible
        # partner export - for every group. The paid-out flag is the one new
        # line: it is what lets the partner see, per position, whether the
        # amount was still with them (Nein) or already paid out to them in an
        # earlier run (Ja, so no internal netting happens for it here).
        refund_date_text = item['refund_date'].strftime('%d.%m.%Y') if item.get('refund_date') else 'nicht angegeben'
        item['extra'] = '\n'.join([
            'eBay-Bestellnummer: ' + item['order'],
            'SKU: ' + item['extra'].split('\nSKU: ', 1)[-1],
            'Refund-Datum: ' + refund_date_text,
            'Refund-Payout: ' + item['payout_id'],
            'Bereits an Partner bezahlt: ' + ('Ja' if item['_paid_out'] else 'Nein'),
        ])
    return result


def _closing_statement_rows(name, item_count):
    """Row numbers for the note and (Rechnung sheet only) the closing summary block.

    Kept as one function so _fill_sheet and export_partner_excel's print-area
    computation can never drift apart from each other.
    """
    last = HEADER_ROW + max(1, item_count)
    start = last + 2
    note_row = start + 8
    if name != 'Rechnung':
        return dict(start=start, note_row=note_row, visible_last=note_row)
    section_header_row = note_row + 2
    finale_start = section_header_row + 1
    final_row = finale_start + FINALE_LINE_COUNT + 1
    return dict(start=start, note_row=note_row, section_header_row=section_header_row,
                finale_start=finale_start, final_row=final_row, visible_last=final_row)


def _set_cell(cell, value, formula=None):
    for child in list(cell):
        cell.remove(child)
    cell.attrib.pop('t', None)
    if formula is not None:
        ET.SubElement(cell, TAG('f')).text = formula
    if value is None:
        return
    if isinstance(value, datetime):
        value = (value - datetime(1899, 12, 30)).days
    if isinstance(value, (int, float, Decimal)):
        if not math.isfinite(float(value)):
            raise ValueError('Ungültiger Betrag im Partnerexport.')
        ET.SubElement(cell, TAG('v')).text = str(value)
    else:
        cell.set('t', 'inlineStr')
        node = ET.SubElement(ET.SubElement(cell, TAG('is')), TAG('t'))
        node.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        # Inline text also prevents spreadsheet formula injection from CSV titles.
        node.text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value))


def _fill_sheet(xml, model, name):
    sheet = ET.fromstring(xml)
    data = sheet.find(TAG('sheetData'))
    prototype = {int(row.get('r')): row for row in data}
    data.clear()

    def row_from(source, number, values=None, formulas=None):
        row = copy.deepcopy(prototype[source])
        row.set('r', str(number))
        cells = {re.sub(r'\d+', '', cell.get('r')): cell for cell in row}
        for col, cell in cells.items():
            cell.set('r', f'{col}{number}')
        for col, value in (values or {}).items():
            cell = cells.get(col)
            if cell is None:
                cell = ET.SubElement(row, TAG('c'), {'r': f'{col}{number}'})
            _set_cell(cell, value, (formulas or {}).get(col))
        row[:] = sorted(row, key=lambda cell: (len(re.sub(r'\d+', '', cell.get('r'))), cell.get('r')))
        data.append(row)
        return row

    payout_ids = sorted(model['payouts'])
    dates = [date for date in model['payouts'].values() if date]
    if len(dates) != len(payout_ids):
        period = 'Auszahlungsdatum nicht vollständig im Bericht angegeben'
    elif min(dates) == max(dates):
        period = 'Auszahlungsdatum: ' + min(dates).strftime('%d.%m.%Y')
    else:
        period = 'Auszahlungszeitraum: ' + min(dates).strftime('%d.%m.%Y') + ' – ' + max(dates).strftime('%d.%m.%Y')
    items = model[name]
    last = HEADER_ROW + max(1, len(items))
    layout = _closing_statement_rows(name, len(items))
    start = layout['start']
    helper_first = layout['visible_last'] + 3
    rechnung_totals = model['totals']['Rechnung']
    sheet_totals = model['totals'][name]
    created_on = datetime.now().strftime('%d.%m.%Y')
    is_historical_sheet = name == 'HistorischeGutschriften'
    is_refund_sheet = name != 'Rechnung'
    title = (('GUTSCHRIFTEN / OFFENE RÜCKFORDERUNGEN – ' if is_historical_sheet else 'ERSTATTUNGEN / ABZÜGE – ')
             if is_refund_sheet else 'PARTNERABRECHNUNG – ') + str(model['partner'])
    metadata = {
        1: {'A': title},
        2: {'A': f'Abrechnungszeitraum: {period} · Erstellt am {created_on}'},
        4: {'A': model['partner'], 'C': model['group'], 'E': model['recipient'], 'G': model['rate'], 'I': TAX},
        6: {'A': model['address'], 'E': ', '.join(payout_ids)},
        7: {'E': period},
        # Rows 8/10/11 previously carried internal Lexoffice field-mapping notes;
        # the header only needs to be compact now, so they stay blank.
        8: {},
        10: {'A': None, 'G': None},
        11: {'A': None, 'G': None},
        12: ({'A': f'Historische Rückforderungen: {len(items)}',
              'G': 'Refund brutto: ' + format_euro(sheet_totals['ebay'])} if is_historical_sheet
             else {'A': f'Erstattungen: {len(model["Gutschriften"])}',
                   'G': 'Refund brutto: ' + format_euro(sheet_totals['ebay'])} if is_refund_sheet
             else {'A': f'Reguläre Positionen: {len(model["Rechnung"])}', 'G': f'Erstattungen / Abzüge: {len(model["Gutschriften"])}'}),
        13: ({'A': 'Auswirkung auf Partneranspruch: ' + format_euro(sheet_totals['gross'])} if is_refund_sheet
             else {'A': None}),
    }
    for number in sorted(n for n in prototype if n <= HEADER_ROW):
        row = row_from(number, number, metadata.get(number))
        if number == 6:
            row.set('ht', str(max(28, 18 * math.ceil(len(', '.join(payout_ids))/90))))
        if number == 4:
            row.set('ht', str(max(30, 17 * math.ceil(len(model['partner'])/35))))
        if number == 1:
            row.set('ht', str(max(42, 22 * math.ceil(len(title)/45))))
        row.set('customHeight', '1')
    previous_cumulative_helper = None
    for offset, item in enumerate(items):
        number = FIRST_ROW + offset
        helper = helper_first + offset
        independent = bool(item.get('_independent'))
        previous_tax = '0' if independent or previous_cumulative_helper is None else f'J{previous_cumulative_helper}'
        gross_formula = f'H{helper}+J{helper}-{previous_tax}'
        if not independent:
            previous_cumulative_helper = helper
        row = row_from(FIRST_ROW + offset % 2, number, {
            'A': item['date'] or 'Nicht angegeben', 'B': item['order'], 'C': item['article'],
            'D': item['extra'], 'E': 1, 'F': 'Stück', 'G': item['net'],
            'H': model['rate'], 'I': TAX, 'J': item['gross'], 'K': item['ebay'],
        }, {'H': '$G$4', 'I': '$I$4', 'J': gross_formula})
        lines = max(sum(max(1, len(textwrap.wrap(line, width=width))) for line in text.split('\n'))
                    for text, width in [(item['article'], 43), (item['extra'], 37)])
        row.set('ht', str(max(60, lines * 16 + 12)))
        row.set('customHeight', '1')
    if not items:
        values = {col: None for col in 'ABCDEFGHIJK'}
        values['C'] = ('Keine offenen Rückforderungen vorhanden.' if is_historical_sheet
                        else 'Keine Erstattungen vorhanden.' if is_refund_sheet
                        else 'Keine Rechnungspositionen vorhanden.')
        row_from(FIRST_ROW, FIRST_ROW, values)
    totals = model['totals'][name]
    helper_last = helper_first + max(1, len(items)) - 1
    formulas = [f'SUM(G{helper_first}:G{helper_last})',
                f'K{start}-K{start+2}', f'SUM(H{helper_first}:H{helper_last})',
                f'SUM(J{FIRST_ROW}:J{last})-K{start+2}', f'SUM(J{FIRST_ROW}:J{last})',
                f'SUM(K{FIRST_ROW}:K{last})', f'K{start+5}-K{start+4}']
    for offset, key in enumerate(['net', 'discount', 'net_after', 'tax', 'gross', 'ebay', 'gross_discount']):
        row_from(19 + offset, start + offset, {'K': totals[key]}, {'K': formulas[offset]})
    note = ('Rechenweg: VK netto × Menge, danach Positionsrabatt; jede Nettoposition auf Cent runden. '
            '19 % Umsatzsteuer auf die Nettosumme. Die Steuer wird centgenau auf die Positionsbruttos verteilt. '
            'eBay-Beträge dienen nur zur Kontrolle.')
    if is_refund_sheet:
        note += ' Erstattungen sind als negative Korrekturen dargestellt.'
    if is_historical_sheet:
        note += (' Diese Positionen wurden dem Partner bereits in einem früheren Zahlungslauf '
                  'ausgezahlt und werden hier separat als offene Rückforderung/Gutschrift geführt.')
    row_from(27, layout['note_row'], {'A': note})
    if name == 'Rechnung':
        # Closing statement for the merchant: only already-computed
        # calculate_sheet totals, nothing recalculated with a new rule. The
        # regular claim is netted with Gutschriften (Tab 2: refunds already
        # known before this claim is paid) so FINALER RECHNUNGSBETRAG is the
        # one number the partner may actually invoice - never with
        # HistorischeGutschriften (Tab 3), which is its own, not-yet-settled
        # repayment case and must never reduce a still-open claim.
        gutschriften_totals = model['totals']['Gutschriften']
        final_amount = rechnung_totals['gross'] + gutschriften_totals['gross']
        row_from(9, layout['section_header_row'], {'A': 'GESAMTABRECHNUNG'})
        rate_pct = f"{model['rate']*100:.1f}".replace('.', ',') + ' %'
        finale_lines = [
            ('Verkaufs-/Abrechnungsbasis brutto', rechnung_totals['ebay']),
            (f'abzgl. Partnerabzug {rate_pct} auf Netto', -rechnung_totals['discount']),
            ('Regulärer Abrechnungsbetrag', rechnung_totals['gross']),
            ('bereits berücksichtigte Erstattungen/Abzüge (siehe Tab „Erstattungen-Abzüge")', gutschriften_totals['gross']),
        ]
        assert len(finale_lines) == FINALE_LINE_COUNT
        for offset, (label, value) in enumerate(finale_lines):
            row_from(19, layout['finale_start'] + offset, {'A': label, 'K': value})
        regular_row = layout['finale_start'] + 2
        refunds_row = layout['finale_start'] + 3
        row_from(23, layout['final_row'], {'A': 'FINALER RECHNUNGSBETRAG', 'K': final_amount},
                 {'K': f'K{regular_row}+K{refunds_row}'})
    # Formula-only calculation rows, outside the print area and hidden. This
    # keeps exactly eleven visible columns and avoids fragile array formulas.
    # G: undiscounted net; H: rounded line net; I: running net; J: running VAT.
    previous_cumulative_helper = None
    for offset, item in enumerate(items or [None]):
        helper = helper_first + offset
        number = FIRST_ROW + offset
        values = {col: None for col in 'ABCDEFGHIJK'}
        if item:
            independent = bool(item.get('_independent'))
            values.update(G=item['net'], H=item['net_after'], I=item['_running_net'], J=item['_tax_to_date'])
            running_formula = (f'H{helper}' if independent or previous_cumulative_helper is None
                               else f'I{previous_cumulative_helper}+H{helper}')
            helper_formulas = {'G': f'E{number}*G{number}', 'H': f'ROUND(G{helper}*(1-H{number}),2)',
                               'I': running_formula, 'J': f'ROUND(I{helper}*I{number},2)'}
            if not independent:
                previous_cumulative_helper = helper
        else:
            values.update(G=0, H=0, I=0, J=0)
            helper_formulas = {}
        row = row_from(FIRST_ROW, helper, values, helper_formulas)
        row.set('hidden', '1')
    merges = sheet.find(TAG('mergeCells'))
    for merge in list(merges):
        if int(re.search(r'\d+', merge.get('ref')).group()) > HEADER_ROW:
            merges.remove(merge)
    for number in range(start, start + 7):
        ET.SubElement(merges, TAG('mergeCell'), {'ref': f'A{number}:J{number}'})
    ET.SubElement(merges, TAG('mergeCell'), {'ref': f'A{layout["note_row"]}:K{layout["note_row"]}'})
    if name == 'Rechnung':
        ET.SubElement(merges, TAG('mergeCell'), {'ref': f'A{layout["section_header_row"]}:K{layout["section_header_row"]}'})
        for number in range(layout['finale_start'], layout['finale_start'] + FINALE_LINE_COUNT):
            ET.SubElement(merges, TAG('mergeCell'), {'ref': f'A{number}:J{number}'})
        ET.SubElement(merges, TAG('mergeCell'), {'ref': f'A{layout["final_row"]}:J{layout["final_row"]}'})
    merges.set('count', str(len(merges)))
    dimension = sheet.find(TAG('dimension'))
    if dimension is not None:
        dimension.set('ref', f'A1:K{helper_last}')
    view = sheet.find(f'{TAG("sheetViews")}/{TAG("sheetView")}')
    for child in list(view):
        view.remove(child)
    if name != 'Rechnung':
        # The main sheet stays fully scrollable (no frozen header); the refund
        # detail sheet keeps the header frozen while scrolling its rows.
        ET.SubElement(view, TAG('pane'), {'ySplit': str(HEADER_ROW), 'topLeftCell': f'A{FIRST_ROW}',
                                         'activePane': 'bottomLeft', 'state': 'frozen'})
        ET.SubElement(view, TAG('selection'), {'pane': 'bottomLeft', 'activeCell': f'A{FIRST_ROW}', 'sqref': f'A{FIRST_ROW}'})
    auto_filter = ET.Element(TAG('autoFilter'), {'ref': f'A{HEADER_ROW}:K{last}'})
    sheet.insert(list(sheet).index(merges), auto_filter)
    return ET.tostring(sheet, encoding='utf-8', xml_declaration=True)


# Third worksheet part added at export time (the template itself, authored
# with an external tool per the module docstring, only ever shipped sheet1/2).
# Fixed synthetic ids for the new relationship/content-type/sheet entries -
# only required to be unique within this workbook, never read back anywhere.
THIRD_SHEET_TARGET = 'xl/worksheets/sheet3.xml'
THIRD_SHEET_RID = 'Rhist0f3a9c7d5e21'
SHEET_NAMES = ('Rechnung', 'Gutschriften', 'HistorischeGutschriften')
REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT_NS = 'http://schemas.openxmlformats.org/package/2006/content-types'
WORKSHEET_CONTENT_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'
R_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def export_partner_excel(rows, payouts=None, orders=None, statement_type='partner'):
    model = prepare_partner_export(rows, payouts, orders, statement_type)
    output = io.BytesIO()
    with zipfile.ZipFile(TEMPLATE) as source, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as target:
        # Rechnung is the one and only layout source. All three worksheet XML
        # entries are built from this same template, so column widths, styles,
        # merges and the visible table shape cannot drift apart between tabs -
        # sheet2/sheet3's own template bytes are never used as a structural source.
        master_template = source.read('xl/worksheets/sheet1.xml')
        third_sheet_content = _fill_sheet(master_template, model, 'HistorischeGutschriften')
        for entry in source.infolist():
            content = source.read(entry.filename)
            if entry.filename in ('xl/worksheets/sheet1.xml', 'xl/worksheets/sheet2.xml'):
                name = 'Rechnung' if entry.filename.endswith('sheet1.xml') else 'Gutschriften'
                content = _fill_sheet(master_template, model, name)
            elif entry.filename == 'xl/_rels/workbook.xml.rels':
                rels = ET.fromstring(content)
                ET.SubElement(rels, f'{{{REL_NS}}}Relationship', {
                    'Type': f'{R_NS}/worksheet', 'Target': '/' + THIRD_SHEET_TARGET, 'Id': THIRD_SHEET_RID})
                content = ET.tostring(rels, encoding='utf-8', xml_declaration=True)
            elif entry.filename == '[Content_Types].xml':
                types = ET.fromstring(content)
                ET.SubElement(types, f'{{{CT_NS}}}Override', {
                    'PartName': '/' + THIRD_SHEET_TARGET, 'ContentType': WORKSHEET_CONTENT_TYPE})
                content = ET.tostring(types, encoding='utf-8', xml_declaration=True)
            elif entry.filename == 'xl/workbook.xml':
                workbook = ET.fromstring(content)
                sheets_el = workbook.find(TAG('sheets'))
                for sheet in sheets_el:
                    if sheet.get('name') in DISPLAY_NAMES:
                        sheet.set('name', DISPLAY_NAMES[sheet.get('name')])
                ET.SubElement(sheets_el, TAG('sheet'), {
                    'name': DISPLAY_NAMES['HistorischeGutschriften'], 'sheetId': '3',
                    f'{{{R_NS}}}id': THIRD_SHEET_RID})
                names = workbook.find(TAG('definedNames'))
                if names is None:
                    names = ET.Element(TAG('definedNames'))
                    workbook.insert(list(workbook).index(sheets_el) + 1, names)
                for index, name in enumerate(SHEET_NAMES):
                    visible_last = _closing_statement_rows(name, len(model[name]))['visible_last']
                    ET.SubElement(names, TAG('definedName'), {'name': '_xlnm.Print_Area', 'localSheetId': str(index)}).text = f"'{DISPLAY_NAMES[name]}'!$A$1:$K${visible_last}"
                content = ET.tostring(workbook, encoding='utf-8', xml_declaration=True)
            target.writestr(entry, content)
        target.writestr(THIRD_SHEET_TARGET, third_sheet_content)
    return output.getvalue()
