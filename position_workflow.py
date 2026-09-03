"""Business completion per position, independent of immutable Lexware reservations."""
import hashlib
import json
from datetime import date, datetime, timezone
import pandas as pd
import core

FIELDS = ('reviewed_at', 'paid_at', 'received_at', 'closed_at')


def position_key(row):
    values = [str(row[k]) for k in ('Auszahlung Nr.', 'Transaktionsnummer', 'Bestellnummer', 'Artikelnummer', 'Art')]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def source_snapshot(row):
    return json.dumps({k: str(row[k]) for k in ('Auszahlung Nr.','Bestellnummer','Partner','Gruppe','SKU','Angebotstitel','Erlös_Brutto','eBay_Netto','Art')}, ensure_ascii=False, sort_keys=True)


def positions(master=None, states=None):
    master = core.load_master_data() if master is None else master
    states = core.sync_status(master) if states is None else states
    if master.empty:
        return master.copy()
    with core.ledger() as db:
        saved = {r['position_key']: dict(r) for r in db.execute('SELECT * FROM position_workflow')}
    transport = {r.Auszahlung: r for r in states.itertuples()}
    result = master.copy()
    result['position_key'] = result.apply(position_key, axis=1)
    if result.loc[result.Art != 'Gebühr', 'position_key'].duplicated().any():
        raise ValueError('Positionsstatus benötigt eindeutige Transaktionsidentitäten.')
    records = []
    for _, row in result.iterrows():
        stored = saved.get(row.position_key, {})
        record = {field: stored.get(field) or '' for field in FIELDS}
        state = transport.get(row['Auszahlung Nr.'])
        transferred = bool(state is not None and state.Entwurf and row.Gruppe == 'Gruppe B' and row.Art == 'Bestellung' and row['Erlös_Brutto'] > 0)
        source_changed = bool(stored.get('source') and stored['source'] != source_snapshot(row))
        valid = not row['Prüfhinweis'] and not source_changed and row.Gruppe in ('Gruppe A','Gruppe B')
        if record['closed_at']:
            status = 'abgeschlossen'
        elif not valid:
            status = 'Prüfung erforderlich'
        elif record['paid_at'] or record['received_at']:
            status = 'teilweise bezahlt / erhalten'
        elif record['reviewed_at']:
            status = 'Rechnung/Abrechnung geprüft'
        elif transferred:
            status = 'in Bearbeitung · Lexware-Entwurf erstellt'
        else:
            status = 'abrechnungsbereit' if row.Art == 'Bestellung' else 'Erstattung zu klären'
        record.update(Bearbeitungsstatus=status, Lexware_uebertragen=transferred,
                      Partnerrechnung='geprüft' if record['reviewed_at'] else 'noch nicht geprüft',
                      Partnerzahlung='bezahlt' if record['paid_at'] else 'offen',
                      Evelyn_Zahlung=('erhalten' if record['received_at'] else 'offen') if row.Gruppe == 'Gruppe B' else 'nicht zutreffend',
                      Quellenpruefung='Quelldaten seit Bestätigung verändert' if source_changed else '',
                      partner_ready=bool(valid and not record['closed_at'] and not record['paid_at'] and row.Art=='Bestellung' and row['Erlös_Brutto'] > 0))
        records.append(record)
    return pd.concat([result.reset_index(drop=True), pd.DataFrame(records)], axis=1)


def payout_status(positions):
    result = {}
    if positions.empty:
        return result
    for pid, block in positions.groupby('Auszahlung Nr.'):
        relevant = block[block.Art != 'Gebühr']
        if relevant.empty:
            status = 'vollständig zugeordnet'
        elif relevant['closed_at'].astype(bool).all():
            status = 'abgeschlossen'
        elif relevant['Prüfhinweis'].astype(bool).any() or relevant['Quellenpruefung'].astype(bool).any():
            status = 'Prüfung erforderlich'
        elif relevant['closed_at'].astype(bool).any() or relevant.Lexware_uebertragen.any() or relevant[list(FIELDS)].astype(bool).any().any():
            status = 'teilweise in Bearbeitung'
        else:
            status = 'abrechnungsbereit'
        result[pid] = status
    return result


def confirm(keys, action, event_date):
    """Manual evidence only. No network calls and no payout-wide status changes."""
    if action not in ('review', 'partner_paid', 'evelyn_received', 'refund_settled'):
        raise ValueError('Unbekannte Bestätigung.')
    value = date.fromisoformat(str(event_date))
    if value > date.today():
        raise ValueError('Ein zukünftiges Zahlungs-/Prüfdatum ist nicht zulässig.')
    keys = set(keys)
    if not keys:
        raise ValueError('Keine Positionen ausgewählt.')
    with core.FileLock(core.PAYOUTS_DB_PATH+'.lock'), core.FileLock(core.ORDERS_DB_PATH+'.lock'):
        current = positions()
        chosen = current[current.position_key.isin(keys)] if not current.empty else current
        if len(chosen) != len(keys):
            raise ValueError('Positionen nicht mehr eindeutig vorhanden. Ansicht aktualisieren.')
        with core.ledger() as db:
            db.execute('BEGIN IMMEDIATE')
            for _, row in chosen.iterrows():
                if row['Prüfhinweis'] or row.Quellenpruefung or row.Gruppe not in ('Gruppe A','Gruppe B') or row.Art=='Gebühr':
                    raise ValueError('Ungeklärte Positionen können nicht bestätigt werden.')
                old = db.execute('SELECT * FROM position_workflow WHERE position_key=?', (row.position_key,)).fetchone()
                saved = dict(old) if old else dict.fromkeys(FIELDS)
                if saved['closed_at']:
                    raise ValueError('Position bereits abgeschlossen; keine erneute Bearbeitung.')
                if action == 'review':
                    field = 'reviewed_at'
                elif action == 'partner_paid':
                    if not saved['reviewed_at'] or row.Art != 'Bestellung':
                        raise ValueError('Zuerst die Partnerrechnung prüfen; Erstattungen separat erledigen.')
                    field = 'paid_at'
                elif action == 'evelyn_received':
                    if row.Gruppe != 'Gruppe B' or not row.Lexware_uebertragen:
                        raise ValueError('Evelyn-Zahlung nur für bereits übertragene positive Gruppe-B-Positionen bestätigen.')
                    field = 'received_at'
                else:
                    if row.Art != 'Erstattung' or not saved['reviewed_at']:
                        raise ValueError('Erstattung zunächst prüfen und alle zugehörigen Zahlungswege klären.')
                    field = 'closed_at'
                if saved.get(field):
                    raise ValueError('Diese Bestätigung ist bereits vorhanden.')
                saved[field] = value.isoformat()
                if action == 'refund_settled':
                    saved['paid_at'] = value.isoformat()
                    if row.Gruppe == 'Gruppe B':
                        saved['received_at'] = value.isoformat()
                if saved['reviewed_at'] and saved['paid_at'] and (row.Gruppe=='Gruppe A' or (saved['received_at'] and row.Lexware_uebertragen)):
                    saved['closed_at'] = max(saved['reviewed_at'], saved['paid_at'], saved['received_at'] or '')
                db.execute('INSERT OR REPLACE INTO position_workflow(position_key,reviewed_at,paid_at,received_at,closed_at,source) VALUES(?,?,?,?,?,?)',
                           (row.position_key, *(saved[f] for f in FIELDS), source_snapshot(row)))
                core.audit(db, row['Auszahlung Nr.'], f"Positionsbestätigung {action}: {row.position_key}; Datum {value.isoformat()}; manuell durch Nutzer")
            db.commit()
