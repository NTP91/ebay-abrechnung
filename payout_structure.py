"""Validate eBay order headers and non-financial article references."""
from decimal import Decimal
import core


def child_reference(row):
    return (row['Typ'].strip().casefold() == 'bestellung'
            and bool(row['Bestellnummer'] and row['Transaktionsnummer'] and row['Artikelnummer'])
            and not core.clean(row['Betrag abzügl. Kosten'])
            and not core.clean(row.get('Transaktionsbetrag (inkl. Kosten)', '')))


def validate(frame):
    """Return validated child indices; never infer a financial amount for a child."""
    children = set()
    paid = frame[frame['Auszahlung Nr.'] != '']
    for (payout, order, kind), block in paid.groupby(['Auszahlung Nr.', 'Bestellnummer', 'Typ'], sort=False):
        references = block[block.apply(child_reference, axis=1)]
        if not references.empty:
            headers = block[(block.Transaktionsnummer == '') & (block.Artikelnummer == '')]
            if len(headers) != 1 or len(references) < 2 or len(block) != len(references) + 1:
                raise ValueError(f'Mehrfachbestellung {order}: keine eindeutige vollständige Parent-/Child-Gruppe; Beträge fehlen.')
            parent = headers.iloc[0]
            core.parse_money(parent['Betrag abzügl. Kosten'])
            gross = core.parse_money(parent.get('Transaktionsbetrag (inkl. Kosten)', ''))
            subtotal = sum((core.parse_money(row.get('Zwischensumme Artikel', '')) for _, row in references.iterrows()), Decimal(0))
            shipping = sum((core.parse_money(row['Verpackung und Versand']) for _, row in references.iterrows() if core.clean(row.get('Verpackung und Versand', ''))), Decimal(0))
            if subtotal + shipping != gross:
                raise ValueError(f'Mehrfachbestellung {order}: Child-Zwischensummen inklusive Versand {subtotal + shipping} EUR stimmen nicht mit Parent-Gesamtbetrag {gross} EUR überein.')
            children.update(references.index)
        for index, row in block.iterrows():
            if index not in children:
                core.parse_money(row['Betrag abzügl. Kosten'])
    return children
