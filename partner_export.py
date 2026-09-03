"""Partner-only exports; no changes to import, settlement, or invoice API logic.

The versioned template is authored with artifact-tool. At runtime only the
standard-library OpenXML writer below fills it, without a Node dependency.
"""
import copy
import io
import math
import re
import textwrap
import zipfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET

import core

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
ET.register_namespace('', NS)
TAG = lambda name: f'{{{NS}}}{name}'
TEMPLATE = Path(__file__).with_name('templates') / 'partner.xlsx'
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


def prepare_partner_export(rows, payouts=None, orders=None):
    """Enrich only the export, resolving original transaction and order fields."""
    if rows.empty or rows['Partner'].nunique() != 1 or rows['Gruppe'].nunique() != 1:
        raise ValueError('Partnerexport benötigt genau einen Partner und eine Gruppe.')
    partner, group = rows.iloc[0]['Partner'], rows.iloc[0]['Gruppe']
    if group not in ('Gruppe A', 'Gruppe B') or rows['Prüfhinweis'].astype(bool).any():
        raise ValueError('Partnerexport enthält ungeklärte Zuordnungen.')
    payouts = core.read_master(core.PAYOUTS_DB_PATH) if payouts is None else payouts
    orders = core.read_master(core.ORDERS_DB_PATH) if orders is None else orders
    rate = Decimal('.005') if group == 'Gruppe A' else Decimal('.035')
    result = {'partner': partner, 'group': group, 'rate': rate, 'payouts': {},
              'Rechnung': [], 'Gutschriften': []}
    for _, row in rows.iterrows():
        if row['Art'] not in ('Bestellung', 'Erstattung'):
            continue
        match, issue = core.match_order(row, orders)
        if issue or match is None or not core.clean(match['Angebotstitel']):
            raise ValueError('Partnerexport benötigt den eindeutig zugeordneten Bestellbericht-Titel.')
        candidates = payouts
        for key in ('Auszahlung Nr.', 'Bestellnummer', 'Transaktionsnummer', 'Artikelnummer'):
            candidates = candidates[candidates[key] == row[key]]
        base = Decimal(str(row['Erlös_Brutto']))
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
            'article': str(match['Angebotstitel']) + '\nSKU: ' + str(match['SKU']),
            'base': base, 'net': net, 'discount': net * rate,
            'gross': base * (1 - rate), 'ebay': original_gross,
        }
        result['Gutschriften' if row['Art'] == 'Erstattung' else 'Rechnung'].append(item)
    return result


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
        row[:] = sorted(row, key=lambda cell: cell.get('r'))
        data.append(row)
        return row

    payout_ids = sorted(model['payouts'])
    dates = [model['payouts'][pid] for pid in payout_ids]
    metadata = {
        4: {'A': model['partner'], 'C': model['group'], 'H': model['rate']},
        6: {'A': '\n'.join(payout_ids), 'D': '\n'.join(
            f'{date:%d.%m.%Y}' if date else 'Nicht im Bericht angegeben'
            for pid, date in zip(payout_ids, dates))},
        10: {'F': 'Rabatt 0,5 %' if model['group'] == 'Gruppe A' else 'Rabatt 3,5 %'},
    }
    for number in sorted(n for n in prototype if n <= 10):
        row = row_from(number, number, metadata.get(number))
        if number == 6:
            row.set('ht', str(max(26, 18 * len(payout_ids))))
            row.set('customHeight', '1')
    items = model[name]
    for offset, item in enumerate(items):
        number = 11 + offset
        row = row_from(11 + offset % 2, number, {
            'A': item['date'] or 'Nicht angegeben', 'B': item['order'], 'C': item['article'],
            'D': 1, 'E': item['net'], 'F': item['discount'], 'G': item['gross'], 'H': item['ebay'],
        }, {'F': f'E{number}*$H$4', 'G': f'{item["base"]}*(1-$H$4)'})
        lines = sum(max(1, len(textwrap.wrap(line, width=52))) for line in item['article'].split('\n'))
        row.set('ht', str(max(60, lines * 16 + 16)))
        row.set('customHeight', '1')
    if not items:
        row_from(11, 11, {'A': None, 'B': '', 'C': 'Keine Erstattungen vorhanden.' if name == 'Gutschriften'
                         else 'Keine Rechnungspositionen vorhanden.', 'D': None, 'E': None, 'F': None, 'G': None, 'H': None})
    last = 10 + max(1, len(items))
    start = last + 3
    net = sum((item['net'] for item in items), Decimal(0))
    discount = sum((item['discount'] for item in items), Decimal(0))
    net_after = net - discount
    tax = net_after * Decimal('.19')
    gross = sum((item['gross'] for item in items), Decimal(0))
    ebay = sum((item['ebay'] for item in items), Decimal(0))
    formulas = [f'SUM(E11:E{last})', f'SUM(F11:F{last})', f'H{start}-H{start+1}',
                f'H{start+2}*$E$4', f'SUM(G11:G{last})', f'SUM(H11:H{last})']
    for offset, value in enumerate([net, discount, net_after, tax, gross, ebay]):
        row_from(15 + offset, start + offset, {'H': value}, {'H': formulas[offset]})
    note = ('Netto vor Rabatt: bisheriger, je Transaktion auf Cent gerundeter Nettobetrag. '
            'Brutto nach Rabatt: bisheriger Abrechnungsbetrag × (1 − Provisionssatz). '
            'Erstattungen bleiben mit ihrem ursprünglichen Vorzeichen erhalten.')
    row_from(22, start + 7, {'A': note})
    row_from(23, start + 8, {'A': f'Rundungsdifferenz zum unveränderten Partnerbrutto (Brutto − Netto nach Rabatt − USt.): '
              f'{gross - net_after - tax:.4f} €'.replace('.', ',')})
    merges = sheet.find(TAG('mergeCells'))
    for merge in list(merges):
        if int(re.search(r'\d+', merge.get('ref')).group()) > 10:
            merges.remove(merge)
    for number in range(start, start + 6):
        ET.SubElement(merges, TAG('mergeCell'), {'ref': f'A{number}:G{number}'})
    for number in (start + 7, start + 8):
        ET.SubElement(merges, TAG('mergeCell'), {'ref': f'A{number}:H{number}'})
    merges.set('count', str(len(merges)))
    dimension = sheet.find(TAG('dimension'))
    if dimension is not None:
        dimension.set('ref', f'A1:H{start + 8}')
    view = sheet.find(f'{TAG("sheetViews")}/{TAG("sheetView")}')
    for child in list(view):
        view.remove(child)
    ET.SubElement(view, TAG('pane'), {'ySplit': '10', 'topLeftCell': 'A11',
                                     'activePane': 'bottomLeft', 'state': 'frozen'})
    ET.SubElement(view, TAG('selection'), {'pane': 'bottomLeft', 'activeCell': 'A11', 'sqref': 'A11'})
    auto_filter = ET.Element(TAG('autoFilter'), {'ref': f'A10:H{last}'})
    sheet.insert(list(sheet).index(merges), auto_filter)
    return ET.tostring(sheet, encoding='utf-8', xml_declaration=True)


def export_partner_excel(rows, payouts=None, orders=None):
    model = prepare_partner_export(rows, payouts, orders)
    output = io.BytesIO()
    with zipfile.ZipFile(TEMPLATE) as source, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as target:
        for entry in source.infolist():
            content = source.read(entry.filename)
            if entry.filename in ('xl/worksheets/sheet1.xml', 'xl/worksheets/sheet2.xml'):
                name = 'Rechnung' if entry.filename.endswith('sheet1.xml') else 'Gutschriften'
                content = _fill_sheet(content, model, name)
            target.writestr(entry, content)
    return output.getvalue()
