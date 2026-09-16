"""Import receipts and concise source-derived data status; no network calls."""
from datetime import date, datetime, timezone, timedelta

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


def _day(value):
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return report_date(value).date()


def _coverage(start, end):
    start, end = _day(start), _day(end)
    if start is None or end is None:
        raise ValueError('Berichtszeitraum fehlt. Für Bestellberichte müssen Von und Bis bestätigt werden.')
    if start > end:
        raise ValueError('Berichtszeitraum ist ungültig: Von liegt nach Bis.')
    return start, end


def record_legacy_orders():
    existing = core.read_master(core.ORDERS_DB_PATH)
    if existing.empty:
        return
    with core.ledger() as db:
        if not db.execute("SELECT 1 FROM imports WHERE kind='orders' LIMIT 1").fetchone():
            period = dates(existing, 'orders')
            db.execute('''INSERT INTO imports(kind,filename,start,end,detected,added,present,issues,error,status,
                          observed_start,observed_end) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                       ('orders', 'Altbestand (Importdatum unbekannt)', period[0].isoformat() if period else None,
                        period[-1].isoformat() if period else None, len(existing), 0, len(existing), 0, '',
                        'legacy_unverified', period[0].isoformat() if period else None,
                        period[-1].isoformat() if period else None))
            db.commit()
        else:
            db.execute("""UPDATE imports SET observed_start=COALESCE(observed_start,start),
                          observed_end=COALESCE(observed_end,end)
                          WHERE kind='orders' AND status='legacy_unverified'""")
            db.commit()


def import_file(upload, kind, coverage_start=None, coverage_end=None):
    result = dict(kind=kind, filename=upload.name, detected=0, added=0, present=0, issues=0,
                  historical_without_sku=0, error='', status='failed', payouts=[],
                  coverage_start=None, coverage_end=None, observed_start=None, observed_end=None)
    period = []
    covered = (None, None)
    try:
        if kind == 'orders':
            record_legacy_orders()
            covered = _coverage(coverage_start, coverage_end)
            result['coverage_start'], result['coverage_end'] = map(display_date, covered)
        frame = core.read_report(upload, kind)
        result['detected'] = len(frame)
        period = dates(frame, kind)
        if period:
            result['observed_start'], result['observed_end'] = map(display_date, (period[0], period[-1]))
        if kind == 'orders' and period and (period[0] < covered[0] or period[-1] > covered[1]):
            raise ValueError('Bestellposition liegt außerhalb des bestätigten Berichtszeitraums; keine Daten übernommen.')
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
        if kind == 'payout':
            master = core.load_master_data()
            states = core.sync_status(master)
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
        result['status'] = 'success'
    except Exception as exc:
        result['error'] = str(exc)
    with core.ledger() as db:
        db.execute('''INSERT INTO imports(kind,filename,at,start,end,detected,added,present,issues,error,status,
                      coverage_start,coverage_end,observed_start,observed_end) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (kind, upload.name, datetime.now(timezone.utc).isoformat(),
                    period[0].isoformat() if period else None, period[-1].isoformat() if period else None,
                    result['detected'], result['added'], result['present'], result['issues'], result['error'], result['status'],
                    covered[0].isoformat() if covered[0] else None, covered[1].isoformat() if covered[1] else None,
                    period[0].isoformat() if period else None, period[-1].isoformat() if period else None))
        db.commit()
    return result


def coverage(imports):
    """Return merged successful order-report coverage and exact uncovered days."""
    order_imports = imports[imports.kind == 'orders'].copy()
    successful = order_imports[(order_imports.status == 'success')
                               & order_imports.coverage_start.notna() & order_imports.coverage_end.notna()]
    intervals = sorted((_day(row.coverage_start), _day(row.coverage_end)) for _, row in successful.iterrows())
    merged = []
    for start, end in intervals:
        if not merged or start > merged[-1][1] + timedelta(days=1):
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    gaps = []
    for left, right in zip(merged, merged[1:]):
        gaps.append((left[1] + timedelta(days=1), right[0] - timedelta(days=1)))
    last_success = successful.sort_values('id').iloc[-1] if not successful.empty else None
    last_attempt = order_imports.sort_values('id').iloc[-1] if not order_imports.empty else None
    return {'intervals': merged, 'gaps': gaps, 'start': merged[0][0] if merged else None,
            'end': merged[-1][1] if merged else None, 'last_success': last_success,
            'last_attempt': last_attempt}


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
    order_coverage = coverage(imports)
    gaps = [f'Datenlücke: {display_date(start)} bis {display_date(end)} ist durch keinen erfolgreich importierten Bestellbericht abgedeckt.'
            for start, end in order_coverage['gaps']]
    return {'latest': latest, 'order_end': display_date(order_dates[-1]) if order_dates else None,
            'history': history, 'imports': imports, 'gaps': gaps, 'warnings': warnings,
            'order_coverage': order_coverage, 'unbilled': int(states['Sperre'].isna().sum())}
