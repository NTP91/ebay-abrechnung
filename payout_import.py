"""Source transaction lifecycle, separate from all settlement calculations."""
import pandas as pd
import re


def identity(row):
    # A refund and a sale may share the order/transaction reference.
    kind = row['Typ'].strip().lower()
    reference = str(row.get('Referenznummer', '')).strip()
    if kind == 'rückerstattung' and reference:
        numbers = re.findall(r'\d+', reference)
        return ('refund-reference', row['Bestellnummer'], numbers[-1] if numbers else reference, kind)
    if row['Transaktionsnummer']:
        return ('transaction', row['Transaktionsnummer'], kind)
    if row['Bestellnummer'] and row['Artikelnummer']:
        return ('item', row['Bestellnummer'], row['Artikelnummer'], kind)
    if kind == 'andere gebühr' and reference:
        return ('fee-reference', reference, kind)
    if kind == 'bestellung' and row['Bestellnummer'] and str(row['Betrag abzügl. Kosten']).strip():
        return ('order-parent', row['Bestellnummer'], kind)
    return None


def merge_transactions(existing, incoming, locked_payouts=()):
    columns = list(dict.fromkeys([*existing.columns, *incoming.columns]))
    records = existing.reindex(columns=columns, fill_value='').fillna('').to_dict('records')
    counters = dict(new_paid=0, known_paid=0, new_open=0, still_open=0, assigned_open=0)
    # Paid records win regardless of ordering within the same uploaded batch.
    incoming = incoming.assign(_paid=incoming['Auszahlung Nr.'] != '').sort_values('_paid', ascending=False, kind='stable').drop(columns='_paid')
    for row in incoming.reindex(columns=columns, fill_value='').fillna('').to_dict('records'):
        key = identity(row)
        def same_position(old):
            if key and identity(old) == key:
                return True
            old_key=identity(old)
            if key and old_key and key[0]==old_key[0]=='refund-reference':
                return False  # Separate partial refunds of one item remain separate movements.
            if old['Transaktionsnummer'] and row['Transaktionsnummer']:
                return False
            if old['Typ'].strip().lower() != row['Typ'].strip().lower():
                return False
            if row['Bestellnummer'] and row['Artikelnummer']:
                return (old['Bestellnummer'], old['Artikelnummer']) == (row['Bestellnummer'], row['Artikelnummer'])
            return old == row

        matches = [i for i, old in enumerate(records) if same_position(old)]
        if len(matches) > 1:
            raise ValueError('Uneindeutige Transaktionsidentität; keine Daten übernommen. Bestell-/Transaktionsnummer prüfen.')
        match = matches[0] if matches else None
        paid = bool(row['Auszahlung Nr.'])
        if match is not None:
            old = records[match]
            if any(old[k] and row[k] and old[k] != row[k] for k in ('Bestellnummer', 'Artikelnummer')):
                raise ValueError('Widersprüchliche Transaktionsidentität; keine Daten übernommen.')
            if old['Auszahlung Nr.']:
                if paid and old['Auszahlung Nr.'] != row['Auszahlung Nr.']:
                    raise ValueError('Transaktion ist bereits einem anderen Payout zugeordnet; keine Daten übernommen.')
                if paid:
                    from core import parse_money
                    from payout_structure import child_reference
                    references = child_reference(old) and child_reference(row)
                    if references:
                        for field in ('Zwischensumme Artikel', 'Verpackung und Versand'):
                            from core import clean
                            a, b = clean(old.get(field, '')), clean(row.get(field, ''))
                            if bool(a) != bool(b) or (a and parse_money(a) != parse_money(b)):
                                raise ValueError('Bekannte Child-Position mit abweichender Zwischensumme; Payout unverändert.')
                    if not references and parse_money(old['Betrag abzügl. Kosten']) != parse_money(row['Betrag abzügl. Kosten']):
                        raise ValueError('Bekannte Transaktion mit abweichendem Geldbetrag; Payout unverändert, manuelle Prüfung erforderlich.')
                counters['known_paid'] += 1
                continue  # An older open report must never downgrade a paid row.
            if paid:
                if row['Auszahlung Nr.'] in locked_payouts:
                    raise ValueError('Zusätzliche bisher unbekannte Position in gesperrtem/abgerechnetem Payout; manuelle Prüfung erforderlich. Payout unverändert.')
                records[match] = row
                counters['new_paid'] += 1
                counters['assigned_open'] += 1
            else:
                counters['still_open'] += 1
            continue
        if paid and row['Auszahlung Nr.'] in locked_payouts:
            raise ValueError('Zusätzliche bisher unbekannte Position in gesperrtem/abgerechnetem Payout; manuelle Prüfung erforderlich. Payout unverändert.')
        records.append(row)
        counters['new_paid' if paid else 'new_open'] += 1
    return pd.DataFrame(records, columns=columns), counters
