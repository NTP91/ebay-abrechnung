"""Read-only operational reporting for the deduplicated Trust/Risk cases."""
from __future__ import annotations

import pandas as pd

import core


CASE_KEY = ['order_id', 'line_item_id', 'sku', 'partner_id']
CATEGORIES = {
    'not_as_described': 'Nicht wie beschrieben',
    'defective': 'Defekt / beschädigt',
    'used_instead_of_new': 'Gebraucht statt neu',
    'opened_used': 'Geöffnet / benutzt',
    'empty_consumed': 'Leer / verbraucht',
    'incomplete_parts': 'Unvollständig',
    'wrong_variant': 'Falsche Variante',
    'wrong_item': 'Falschlieferung',
    'item_not_received': 'Nicht erhalten',
    'other_complaint': 'Sonstige Beschwerde',
}
SEVERE = {'defective', 'used_instead_of_new', 'opened_used', 'empty_consumed', 'incomplete_parts'}


def group_for(partner):
    return 'Gruppe A' if str(partner).upper() in {'PP', 'BA', 'MK', '001'} else 'Gruppe B'


def order_volume(orders):
    if orders is None or orders.empty:
        return pd.DataFrame(columns=['partner_id', 'sku', 'orders'])
    rows = orders.copy().fillna('')
    columns = {name.casefold(): name for name in rows.columns}
    sku_col = columns.get('sku')
    order_col = columns.get('bestellnummer')
    line_col = columns.get('transaktionsnummer')
    item_col = columns.get('artikelnummer')
    if not all((sku_col, order_col, line_col, item_col)):
        return pd.DataFrame(columns=['partner_id', 'sku', 'orders'])
    rows['sku'] = rows[sku_col].astype(str).str.strip()
    rows['partner_id'] = rows['sku'].map(core.normalized_partner)
    known = {'PP', 'BA', 'MK', '001', 'MH', *core.known_group_b_partners()}
    rows = rows[rows.partner_id.isin(known) & rows.sku.astype(bool)].copy()
    rows['_position'] = rows[line_col].where(rows[line_col].astype(bool),
        rows[order_col].astype(str) + '|' + rows[item_col].astype(str))
    return rows.groupby(['partner_id', 'sku'], as_index=False)['_position'].nunique().rename(columns={'_position': 'orders'})


def _problem_names(row):
    names = [label for field, label in CATEGORIES.items() if bool(row.get(field))]
    if bool(row.get('has_negative_feedback')):
        names.append('Negative Bewertung')
    return ', '.join(names)


def aggregate(case_rows, orders):
    """Build deterministic partner/group/SKU rankings from unique problem cases."""
    if case_rows is None or case_rows.empty:
        empty = pd.DataFrame()
        return {'total': 0, 'partners': empty, 'groups': empty, 'skus': empty,
                'problems': empty, 'priority': empty, 'negative': empty, 'cases': empty,
                'volume_available': False}
    cases = case_rows.copy().fillna('')
    if 'is_problem' in cases:
        cases = cases[cases.is_problem.map(bool)].copy()
    for field in CATEGORIES:
        cases[field] = cases[field].map(bool)
    cases['has_negative_feedback'] = cases['has_negative_feedback'].map(bool)
    if 'problem_signal_count' not in cases:
        cases['problem_signal_count'] = 0
    cases['problem_signal_count'] = pd.to_numeric(cases['problem_signal_count'], errors='coerce').fillna(0).astype(int)
    cases = cases.drop_duplicates(CASE_KEY).copy()
    cases['Gruppe'] = cases.partner_id.map(group_for)
    cases['Problemarten'] = cases.apply(_problem_names, axis=1)
    volumes = order_volume(orders)
    partner_volume = volumes.groupby('partner_id', as_index=False).orders.sum() if not volumes.empty else volumes

    def summary(keys):
        grouped = cases.groupby(keys, as_index=False).agg(Fälle=('order_id', 'size'))
        for field, label in CATEGORIES.items():
            counts = cases.groupby(keys, as_index=False)[field].sum().rename(columns={field: label})
            grouped = grouped.merge(counts, on=keys, how='left')
        return grouped

    partners = summary(['partner_id']).rename(columns={'partner_id': 'Partner'})
    partners['Anteil'] = partners['Fälle'] / len(cases) * 100
    partners = partners.merge(partner_volume.rename(columns={'partner_id': 'Partner', 'orders': 'Bestellungen'}), on='Partner', how='left')
    partners['Fälle je 100 Bestellungen'] = partners['Fälle'] / partners['Bestellungen'] * 100
    partners = partners.sort_values(['Fälle', 'Partner'], ascending=[False, True]).reset_index(drop=True)

    groups = summary(['Gruppe'])
    member = cases.groupby('Gruppe').partner_id.apply(lambda values: ', '.join(sorted(set(values)))).rename('Partner').reset_index()
    groups = groups.merge(member, on='Gruppe').sort_values('Fälle', ascending=False).reset_index(drop=True)

    skus = summary(['sku', 'partner_id']).rename(columns={'sku': 'SKU', 'partner_id': 'Partner'})
    skus['Gruppe'] = skus.Partner.map(group_for)
    skus = skus.merge(volumes.rename(columns={'sku': 'SKU', 'partner_id': 'Partner', 'orders': 'Bestellmenge'}), on=['SKU', 'Partner'], how='left')
    skus['Fehlerquote'] = skus['Fälle'] / skus['Bestellmenge'] * 100
    sku_specific = ~skus.SKU.str.rstrip().str.endswith('/')
    skus['Kennzeichnung'] = ['Wiederholt auffällig' if specific and value > 1 else 'Keine produktspezifische SKU' if not specific else 'Einzelfall'
                              for specific, value in zip(sku_specific, skus['Fälle'])]
    skus = skus.sort_values(['Fälle', 'Partner', 'SKU'], ascending=[False, True, True]).reset_index(drop=True)

    problem_rows = []
    for field, label in CATEGORIES.items():
        affected = cases[cases[field]]
        problem_rows.append({'Problemart': label, 'Fälle': len(affected),
            'Partner': ', '.join(sorted(set(affected.partner_id))),
            'SKUs': ', '.join(sorted(set(affected.sku)))})
    problems = pd.DataFrame(problem_rows).sort_values(['Fälle', 'Problemart'], ascending=[False, True]).reset_index(drop=True)

    partner_counts = cases.partner_id.value_counts()
    sku_counts = cases.groupby(['partner_id', 'sku']).size()
    def priority(row):
        severe = any(row[field] for field in SEVERE)
        repeated_sku = 0 if str(row.sku).rstrip().endswith('/') else sku_counts.get((row.partner_id, row.sku), 0)
        if row.has_negative_feedback or repeated_sku >= 3 or (severe and (repeated_sku >= 2 or row.problem_signal_count > 1)):
            return 'Priorität 1'
        if severe or repeated_sku >= 2 or partner_counts.get(row.partner_id, 0) > 1:
            return 'Priorität 2'
        return 'Priorität 3'
    cases['Priorität'] = cases.apply(priority, axis=1)
    display = cases.rename(columns={'order_id': 'Bestellung', 'line_item_id': 'Line Item', 'sku': 'SKU',
        'partner_id': 'Partner', 'title': 'Artikel', 'problem_signal_count': 'Problemsignale'})
    columns = ['Priorität', 'Partner', 'Gruppe', 'Bestellung', 'Line Item', 'SKU', 'Artikel', 'Problemarten', 'Problemsignale']
    priority_cases = display[display.Priorität == 'Priorität 1'][columns].copy()
    negative = display[cases.has_negative_feedback.values][columns].copy()
    return {'total': len(cases), 'partners': partners, 'groups': groups, 'skus': skus,
            'problems': problems, 'priority': priority_cases, 'negative': negative,
            'cases': display[columns], 'volume_available': bool(partners['Bestellungen'].notna().any())}
