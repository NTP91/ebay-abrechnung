"""Import receipts and concise source-derived data status; no network calls."""
from datetime import datetime, timezone, timedelta

import pandas as pd
import core
from partner_export import report_date


def dates(frame, kind):
    fields = ('Verkauft am', 'Bestelldatum', 'Datum') if kind == 'orders' else ('Auszahlungsdatum', 'Datum')
    found = []
    for _, row in frame.iterrows():
        value = next((core.clean(row.get(k, '')) for k in fields if core.clean(row.get(k, ''))), '')
        if not value:
            continue
        try:
            found.append(report_date(value).date())
        except ValueError:
            continue
    return sorted(set(found))


def display_date(value):
    return value.strftime('%d.%m.%Y') if value else 'nicht bekannt'


def record_legacy_orders():
    existing = core.read_master(core.ORDERS_DB_PATH)
    if existing.empty:
        return
    with core.ledger() as db:
        if not db.execute("SELECT 1 FROM imports WHERE kind='orders' LIMIT 1").fetchone():
            period = dates(existing, 'orders')
            db.execute('INSERT INTO imports(kind,filename,start,end,detected,added,present,issues,error) VALUES(?,?,?,?,?,?,?,?,?)',
                       ('orders', 'Altbestand (Importdatum unbekannt)', period[0].isoformat() if period else None,
                        period[-1].isoformat() if period else None, len(existing), 0, len(existing), 0, ''))
            db.commit()


def import_file(upload, kind):
    result = dict(kind=kind, filename=upload.name, detected=0, added=0, present=0, issues=0,
                  historical_without_sku=0, error='', payouts=[])
    period = []
    try:
        if kind == 'orders':
            record_legacy_orders()
        frame = core.read_report(upload, kind)
        result['detected'] = len(frame)
        period = dates(frame, kind)
        path = core.PAYOUTS_DB_PATH if kind == 'payout' else core.ORDERS_DB_PATH
        before = core.read_master(path)
        if kind == 'payout':
            result['payouts'] = [{'number': p, 'known': p in set(before['Auszahlung Nr.'])} for p in frame['Auszahlung Nr.'].unique() if p]
        counters = {}
        result['added'] = core.import_reports([frame], path, kind, details=counters)
        result['transactions'] = counters
        result['present'] = result['detected'] - result['added']
        if kind == 'payout':
            result['present'] = counters['known_paid'] + counters['still_open']
        master = core.load_master_data()
        states = core.sync_status(master)
        if kind == 'payout':
            if not master.empty:
                relevant = master[master['Auszahlung Nr.'].isin(frame['Auszahlung Nr.'])]
                result['issues'] = int(relevant['Prüfhinweis'].astype(bool).sum())
            for payout in result['payouts']:
                payout['counts'] = counters['payouts'].get(payout['number'], {})
                payout['warning'] = next((w['reason'] for w in counters['warnings'] if w['payout'] == payout['number']), '')
                matching = states[states.Auszahlung == payout['number']]
                if not matching.empty:
                    state = matching.iloc[0]
                    payout.update(status=state.Status, locked=bool(state.Sperre), invoice=state.Entwurf)
                else:
                    payout.update(status='Nicht übernommen – manuelle Prüfung', locked=True, invoice=None)
        else:
            without_sku = frame['SKU'].str.split('/').str[0].str.strip() == ''
            result['historical_without_sku'] = int(without_sku.sum())
            # Missing SKU means there is deliberately no partner workflow.
            # Other defects in assignable rows remain real review issues.
            result['issues'] = int((~without_sku & (frame['Angebotstitel'] == '')).sum())
    except Exception as exc:
        result['error'] = str(exc)
    with core.ledger() as db:
        db.execute('INSERT INTO imports(kind,filename,at,start,end,detected,added,present,issues,error) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (kind, upload.name, datetime.now(timezone.utc).isoformat(), period[0].isoformat() if period else None,
                    period[-1].isoformat() if period else None, result['detected'], result['added'], result['present'], result['issues'], result['error']))
        db.commit()
    return result


def overview(master, states):
    record_legacy_orders()
    orders = core.read_master(core.ORDERS_DB_PATH)
    raw = core.read_master(core.PAYOUTS_DB_PATH)
    order_dates = dates(orders, 'orders')
    with core.ledger() as db:
        imports = pd.read_sql_query('SELECT * FROM imports ORDER BY id DESC', db)
        events = pd.read_sql_query("SELECT payout, MIN(at) AS imported FROM audit WHERE event='importiert' GROUP BY payout", db)
        warnings = pd.read_sql_query('SELECT payout, at, reason FROM import_warnings ORDER BY id DESC', db).drop_duplicates(['payout', 'reason'])
    history = []
    for _, state in states.iterrows():
        period = dates(raw[raw['Auszahlung Nr.'] == state.Auszahlung], 'payout')
        stamp = events[events.payout == state.Auszahlung]
        history.append({'Payoutnummer': state.Auszahlung, 'Datum / Zeitraum': ' – '.join(dict.fromkeys(display_date(d) for d in (period[0], period[-1]))) if period else 'nicht bekannt',
                        'Importdatum': pd.to_datetime(stamp.iloc[0].imported).tz_convert('Europe/Berlin').strftime('%d.%m.%Y %H:%M') if not stamp.empty else 'Altbestand: nicht bekannt',
                        'Status': state.Status, 'Sperre': 'gesperrt' if state.Sperre else '',
                        '_date': period[-1].isoformat() if period else ''})
    history.sort(key=lambda row: (row['_date'], row['Payoutnummer']), reverse=True)
    latest = history[0] if history else None
    for row in history:
        row.pop('_date')
    gaps = []
    # Transaction-free days do not establish missing reports. Compare only observed
    # imported report ranges, and label the inference explicitly as a possibility.
    valid = imports[(imports.kind == 'orders') & (imports.error == '') & imports.start.notna() & imports.end.notna()]
    end = None
    for _, row in valid.sort_values('start').iterrows():
        start = datetime.fromisoformat(row.start).date()
        finish = datetime.fromisoformat(row.end).date()
        if end and start > end + timedelta(days=1):
            gaps.append(f'Mögliche Datenlücke zwischen {display_date(end)} und {display_date(start)}. Aus beobachteten Berichtspositionen abgeleitet; verkaufsfreie Tage sind ebenfalls möglich.')
        end = max(end, finish) if end else finish
    return {'latest': latest, 'order_end': display_date(order_dates[-1]) if order_dates else None,
            'history': history, 'imports': imports, 'gaps': gaps, 'warnings': warnings,
            'unbilled': int(states['Sperre'].isna().sum())}
