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
MIN_CASES_FOR_RATE = 2
MIN_ORDER_VOLUME = 5
HIGH_ERROR_RATE = 10.0


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
        return {'total': 0, 'partners': empty, 'partners_by_cases': empty,
                'groups': empty, 'skus': empty, 'skus_by_rate': empty,
                'skus_by_cases': empty, 'problems': empty, 'priority': empty,
                'negative': empty, 'cases': empty, 'unresolved_skus': empty,
                'volume_available': False}
    cases = case_rows.copy().fillna('')
    if 'is_problem' in cases:
        cases = cases[cases.is_problem.map(bool)].copy()
    for field in CATEGORIES:
        cases[field] = cases[field].map(bool)
    cases['has_negative_feedback'] = cases['has_negative_feedback'].map(bool)
    # Holds, seller replies and neutral messages remain evidence but are not
    # operational customer-quality cases.
    quality_mask = cases[list(CATEGORIES)].any(axis=1) | cases['has_negative_feedback']
    cases = cases[quality_mask].copy()
    if cases.empty:
        empty = pd.DataFrame()
        return {'total': 0, 'partners': empty, 'partners_by_cases': empty,
                'groups': empty, 'skus': empty, 'skus_by_rate': empty,
                'skus_by_cases': empty, 'problems': empty, 'priority': empty,
                'negative': empty, 'cases': empty, 'unresolved_skus': empty,
                'volume_available': False}
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
    partners['Fehlerquote'] = partners['Fälle je 100 Bestellungen']
    partners[f'Mindestfallzahl ({MIN_CASES_FOR_RATE}) erreicht'] = partners['Fälle'] >= MIN_CASES_FOR_RATE
    partners['Datenbasis'] = partners.apply(
        lambda row: 'ausreichend' if row['Bestellungen'] >= MIN_ORDER_VOLUME and row['Fälle'] >= MIN_CASES_FOR_RATE
        else 'kleine Stichprobe', axis=1)
    partners_by_cases = partners.sort_values(['Fälle', 'Partner'], ascending=[False, True]).reset_index(drop=True)
    partners = partners.sort_values(['Fehlerquote', 'Fälle', 'Partner'], ascending=[False, False, True], na_position='last').reset_index(drop=True)

    groups = summary(['Gruppe'])
    member = cases.groupby('Gruppe').partner_id.apply(lambda values: ', '.join(sorted(set(values)))).rename('Partner').reset_index()
    groups = groups.merge(member, on='Gruppe').sort_values('Fälle', ascending=False).reset_index(drop=True)

    skus = summary(['sku', 'partner_id']).rename(columns={'sku': 'SKU', 'partner_id': 'Partner'})
    skus['Gruppe'] = skus.Partner.map(group_for)
    skus = skus.merge(volumes.rename(columns={'sku': 'SKU', 'partner_id': 'Partner', 'orders': 'Bestellmenge'}), on=['SKU', 'Partner'], how='left')
    skus['Fehlerquote'] = skus['Fälle'] / skus['Bestellmenge'] * 100
    sku_specific = ~skus.SKU.str.rstrip().str.endswith('/')
    skus['Wiederholungsfall'] = sku_specific & (skus['Fälle'] >= MIN_CASES_FOR_RATE)
    skus['Hohe Fehlerquote'] = (skus['Fehlerquote'] >= HIGH_ERROR_RATE) & (skus['Bestellmenge'] >= MIN_ORDER_VOLUME)
    skus['Datenbasis'] = skus.apply(
        lambda row: 'ausreichend' if row['Bestellmenge'] >= MIN_ORDER_VOLUME and row['Fälle'] >= MIN_CASES_FOR_RATE
        else 'kleine Stichprobe', axis=1)
    skus['Kennzeichnung'] = [
        'Keine produktspezifische SKU' if not specific
        else 'Wiederholt auffällig · hohe Fehlerquote' if repeated and high
        else 'Wiederholt auffällig' if repeated
        else 'Hohe Fehlerquote' if high
        else 'Einzelfall'
        for specific, repeated, high in zip(sku_specific, skus['Wiederholungsfall'], skus['Hohe Fehlerquote'])]
    skus_by_cases = skus.sort_values(['Fälle', 'Fehlerquote', 'Partner', 'SKU'], ascending=[False, False, True, True], na_position='last').reset_index(drop=True)
    skus_by_rate = skus.sort_values(['Fehlerquote', 'Fälle', 'Partner', 'SKU'], ascending=[False, False, True, True], na_position='last').reset_index(drop=True)
    skus = skus_by_cases

    order_lookup = orders.copy().fillna('') if orders is not None else pd.DataFrame()
    order_columns = {name.casefold(): name for name in order_lookup.columns}
    lookup_order = order_columns.get('bestellnummer')
    lookup_line = order_columns.get('transaktionsnummer')
    lookup_item = order_columns.get('artikelnummer')
    unresolved_rows = []
    for row in cases[cases.sku.astype(str).str.rstrip().str.endswith('/')].itertuples():
        matches = order_lookup[
            (order_lookup[lookup_order].astype(str) == str(row.order_id)) &
            (order_lookup[lookup_line].astype(str) == str(row.line_item_id))
        ] if lookup_order and lookup_line else pd.DataFrame()
        item_id = str(matches.iloc[0].get(lookup_item, '')) if len(matches) == 1 and lookup_item else ''
        unresolved_rows.append({'Bestellung': row.order_id, 'Line Item': row.line_item_id,
            'Item-ID': item_id, 'Partner': row.partner_id, 'Artikel': row.title,
            'Grund': 'Bestellbericht und gespeicherte eBay-Order enthalten nur das Partnerpräfix; konkrete SKU fehlt.'})
    unresolved_skus = pd.DataFrame(unresolved_rows)

    problem_rows = []
    for field, label in CATEGORIES.items():
        affected = cases[cases[field]]
        problem_rows.append({'Problemart': label, 'Fälle': len(affected),
            'Partner': ', '.join(sorted(set(affected.partner_id))),
            'SKUs': ', '.join(sorted(set(affected.sku)))})
    problems = pd.DataFrame(problem_rows).sort_values(['Fälle', 'Problemart'], ascending=[False, True]).reset_index(drop=True)

    partner_stats = partners_by_cases.set_index('Partner')
    sku_stats = skus_by_cases.set_index(['Partner', 'SKU'])
    def priority(row):
        severe = any(row[field] for field in SEVERE)
        generic = str(row.sku).rstrip().endswith('/')
        stats = sku_stats.loc[(row.partner_id, row.sku)]
        repeated_sku = 0 if generic else int(stats['Fälle'])
        enough_sku_data = (not generic and stats['Bestellmenge'] >= MIN_ORDER_VOLUME
                           and repeated_sku >= MIN_CASES_FOR_RATE)
        high_sku_rate = enough_sku_data and stats['Fehlerquote'] >= HIGH_ERROR_RATE
        if row.has_negative_feedback or (severe and repeated_sku >= 2 and high_sku_rate) or (repeated_sku >= 3 and high_sku_rate):
            return 'Priorität 1'
        partner = partner_stats.loc[row.partner_id]
        enough_partner_data = partner['Bestellungen'] >= MIN_ORDER_VOLUME and partner['Fälle'] >= MIN_CASES_FOR_RATE
        if severe or repeated_sku >= 2 or (enough_partner_data and partner['Fehlerquote'] >= HIGH_ERROR_RATE):
            return 'Priorität 2'
        return 'Priorität 3'
    cases['Priorität'] = cases.apply(priority, axis=1)
    display = cases.rename(columns={'order_id': 'Bestellung', 'line_item_id': 'Line Item', 'sku': 'SKU',
        'partner_id': 'Partner', 'title': 'Artikel', 'problem_signal_count': 'Problemsignale'})
    columns = ['Priorität', 'Partner', 'Gruppe', 'Bestellung', 'Line Item', 'SKU', 'Artikel', 'Problemarten', 'Problemsignale']
    priority_cases = display[display.Priorität == 'Priorität 1'][columns].copy()
    negative = display[cases.has_negative_feedback.values][columns].copy()
    return {'total': len(cases), 'partners': partners, 'partners_by_cases': partners_by_cases,
            'groups': groups, 'skus': skus, 'skus_by_rate': skus_by_rate, 'skus_by_cases': skus_by_cases,
            'problems': problems, 'priority': priority_cases, 'negative': negative,
            'cases': display[columns], 'unresolved_skus': unresolved_skus,
            'volume_available': bool(partners['Bestellungen'].notna().any())}
