"""Read-only API snapshots and transparent audit rules, separate from settlement state."""
import json
import os
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from filelock import FileLock

from ebay_readonly import EbayError

PAYOUT = '7718008497'
BANK = Decimal('491.80')
LOCAL = ZoneInfo('Europe/Berlin')


def now_utc():
    return datetime.now(timezone.utc)


def local_date(value):
    if isinstance(value, dict):
        value = value.get('value')
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.astimezone(LOCAL) if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def euro(value):
    if value is None:
        return 'nicht verfügbar'
    return f'{value:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.') + ' €'


def money(value):
    if not isinstance(value, dict) or value.get('currency') != 'EUR':
        return None
    try:
        number = Decimal(str(value['value']))
        return number if number.is_finite() else None
    except (KeyError, InvalidOperation):
        return None


def cache_path(data_dir):
    return Path(data_dir) / 'Ebay_Readonly' / 'latest.json'


def load_snapshot(data_dir):
    path = cache_path(data_dir)
    if not path.exists():
        return None
    try:
        result = json.loads(path.read_text(encoding='utf-8'))
        if result.get('version') != 1 or not isinstance(result.get('resources'), dict):
            raise ValueError()
        return result
    except (ValueError, OSError):
        raise EbayError('Gespeicherter API-Datenstand nicht lesbar. Bitte neu abrufen.') from None


def save_snapshot(data_dir, snapshot):
    path = cache_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + '.lock'):
        text = json.dumps(snapshot, ensure_ascii=False, indent=2)
        run = path.parent / (now_utc().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex + '.json')
        with run.open('x', encoding='utf-8') as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        temporary = path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)


def collect(client, payout_orders=(), progress=None):
    """No implicit API calls on render. A failed resource is never represented as zero cases."""
    stamp = now_utc()
    start = (stamp - timedelta(days=90)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    end = stamp.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    jobs = [
        ('standards', lambda: client.get('standards')),
        ('returns', lambda: client.pages('returns', 'members', {'return_state': 'ALL_OPEN', 'role': 'SELLER'})),
        ('disputes', lambda: client.pages('disputes', 'paymentDisputeSummaries')),
        ('transactions', lambda: client.pages('transactions', 'transactions', {'filter': f'transactionDate:[{start}..{end}]'})),
        ('payouts', lambda: client.pages('payouts', 'payouts')),
        ('funds', lambda: client.get('funds')),
        ('reference_payout', lambda: client.get('payout', PAYOUT)),
        ('reference_transactions', lambda: client.pages('transactions', 'transactions', {'filter': f'payoutId:{{{PAYOUT}}}'})),
    ]
    for kind in ('ITEM_NOT_RECEIVED', 'ITEM_NOT_AS_DESCRIBED'):
        for cycle in ('CURRENT', 'PROJECTED'):
            jobs.append((kind + '_' + cycle, lambda k=kind, c=cycle: client.get('metrics', k + '/' + c, {'evaluation_marketplace_id': 'EBAY_DE'})))
    for order in sorted(set(str(o) for o in payout_orders if str(o).strip())):
        # IDs originate in existing imports, but must not inject an API filter.
        if all(char.isalnum() or char == '-' for char in order):
            jobs.append(('order_' + order, lambda o=order: client.pages('transactions', 'transactions', {'filter': f'orderId:{{{o}}}'})))
    snapshot = {'version': 1, 'account': 'ebay_durchstart', 'fetched_at': stamp.isoformat(),
                'transaction_window_start': start, 'resources': {}}
    fatal = None
    for index, (name, job) in enumerate(jobs):
        if progress:
            progress(index / len(jobs), name)
        try:
            if fatal:
                raise EbayError(fatal)
            snapshot['resources'][name] = {'available': True, 'data': job()}
        except EbayError as exc:
            error = str(exc)
            snapshot['resources'][name] = {'available': False, 'error': error}
            if any(word in error for word in ('OAuth', 'Zugangsdaten', 'Rate Limit', 'HTTP 401')):
                fatal = error
    return snapshot


def resource(snapshot, name):
    item = (snapshot or {}).get('resources', {}).get(name, {})
    return item.get('data') if item.get('available') else None


def items(snapshot, name):
    return (resource(snapshot, name) or {}).get('items', [])


def service_metrics(snapshot):
    result = []
    for kind, label in [('ITEM_NOT_RECEIVED', 'Artikel nicht erhalten'), ('ITEM_NOT_AS_DESCRIBED', 'Artikel nicht wie beschrieben')]:
        for cycle in ('CURRENT', 'PROJECTED'):
            data = resource(snapshot, kind + '_' + cycle)
            for dimension in (data or {}).get('dimensionMetrics', []):
                scope = dimension.get('dimension') or {}
                for metric in dimension.get('metrics', []):
                    benchmark = metric.get('benchmark') or {}
                    result.append({'Bereich': label, 'Bewertung': 'Aktuell' if cycle == 'CURRENT' else 'Prognose',
                                   'Segment': str(scope.get('dimensionName') or scope.get('dimensionValue') or scope.get('dimensionKey') or ''),
                                   'Kennzahl': metric.get('metricKey', ''), 'Wert': str(metric.get('value', metric.get('metricValue', 'nicht verfügbar'))),
                                   'eBay-Einstufung': benchmark.get('rating', metric.get('benchmarkRating', 'nicht verfügbar'))})
    return result


def link_order(order, item, catalogue, orders):
    """Only stable IDs; ambiguous multi-partner orders stay unassigned."""
    rows = catalogue[catalogue.Bestellnummer == order] if not catalogue.empty else catalogue
    item_id = str(item.get('itemId') or item.get('listingId') or item.get('legacyItemId') or '')
    transaction = str(item.get('transactionId') or item.get('legacyTransactionId') or '')
    if (item_id or transaction) and not orders.empty:
        found = orders[orders.Bestellnummer == order]
        for column, value in [('Artikelnummer', item_id), ('Transaktionsnummer', transaction)]:
            if value:
                found = found[found[column].astype(str) == value] if column in found else found.iloc[:0]
        if found.empty:
            return {'Partner': 'Nicht zugeordnet', 'SKU': '', 'Artikel': '', 'Zuordnung': 'Artikel-ID nicht im Bestellbericht gefunden'}
        rows = rows[rows.SKU.isin(found.SKU)]
    partners = sorted(set(rows.Partner) - {''}) if not rows.empty else []
    return {'Partner': partners[0] if len(partners) == 1 else 'Nicht zugeordnet',
            'SKU': ' / '.join(sorted(set(rows.SKU))) if not rows.empty else '',
            'Artikel': ' | '.join(sorted(set(rows.Produkttitel))) if not rows.empty else '',
            'Zuordnung': 'Artikelzuordnung' if (item_id or transaction) and len(rows) == 1 else 'Bestellbezug' if len(rows) == 1 else 'Mehrere Artikel; nur Bestellbezug' if len(partners) == 1 else 'Keine eindeutige Partnerzuordnung'}


def audit(snapshot, catalogue, orders, now=None):
    now = (now or now_utc()).astimezone(LOCAL)
    cases = []
    seen = set()
    def add(kind, identifier, order, buyer, item, reason, status, due, amount, action, needs_action=False):
        key = (kind, str(identifier))
        if key in seen:
            return
        seen.add(key)
        date = local_date(due)
        today = bool(date and date.date() <= now.date())
        priority = '1 · Kritisch' if today else '2 · Handeln' if needs_action or (date and date.date() <= now.date() + timedelta(days=1)) else '3 · Beobachten'
        cases.append({'Priorität': priority, 'Vorgang': kind, 'Fall': str(identifier), 'Bestellnummer': order,
                      'Käufer': buyer, **link_order(order, item, catalogue, orders), 'Grund': reason,
                      'Status': status, 'Frist': date.strftime('%d.%m.%Y %H:%M') if date else 'nicht verfügbar',
                      'Betrag': euro(money(amount)), 'Nächste Handlung': action, 'heute': today,
                      '_due': date.isoformat() if date else '9999'})
    for row in items(snapshot, 'returns'):
        if row.get('state') == 'CLOSED':
            continue
        creation = row.get('creationInfo') or {}
        refund = row.get('sellerTotalRefund') or {}
        due = (row.get('sellerResponseDue') or {}).get('respondByDate')
        add('Rückgabe', row.get('returnId'), row.get('orderId', ''), row.get('buyerLoginName', ''), creation.get('item') or {}, creation.get('reason', ''), row.get('status') or row.get('state', ''), due,
            refund.get('actualRefundAmount') or refund.get('estimatedRefundAmount'), 'Rückgabe in eBay prüfen; Artikelzustand und Antwortfrist klären.', bool(due))
    for row in items(snapshot, 'disputes'):
        if row.get('paymentDisputeStatus') == 'CLOSED':
            continue
        add('Payment Dispute', row.get('paymentDisputeId'), row.get('orderId', ''), row.get('buyerUsername', ''), {}, row.get('reason', ''), row.get('paymentDisputeStatus', ''), row.get('respondByDate'), row.get('amount'), 'Streitfall in eBay öffnen; Versand- und Zustellnachweise prüfen.', row.get('paymentDisputeStatus') == 'ACTION_NEEDED')
    for row in items(snapshot, 'transactions'):
        if row.get('transactionStatus') not in ('FUNDS_ON_HOLD', 'FUNDS_PROCESSING'):
            continue
        add('Einbehalt' if row['transactionStatus'] == 'FUNDS_ON_HOLD' else 'In Bearbeitung', str(row.get('transactionId')) + '/' + str(row.get('transactionType')), row.get('orderId', ''), '', {}, row.get('transactionMemo', ''), row['transactionStatus'], None, row.get('amount'), 'Grund und mögliche Freigabe in eBay prüfen; keine Abrechnungsfreigabe aus diesem Audit.')
    cases.sort(key=lambda row: (row['Priorität'], row['_due'], row['Fall']))
    for row in cases:
        row.pop('_due')
    partner_texts = {}
    for partner in sorted({row['Partner'] for row in cases} - {'Nicht zugeordnet'}):
        group = [row for row in cases if row['Partner'] == partner]
        counts = Counter(row['Vorgang'] for row in group)
        lines = [f'{partner}: ' + ', '.join(f'{count} {kind}' for kind, count in counts.items()) + ' im abgerufenen Datenstand.']
        for row in group:
            lines.append(f"Bestellung {row['Bestellnummer']} · Fall {row['Fall']} · SKU {row['SKU'] or 'nicht verfügbar'}: {row['Grund'] or row['Status']}. Frist: {row['Frist']}. {row['Nächste Handlung']}")
        partner_texts[partner] = '\n'.join(lines)
    repeated = Counter(row['SKU'] for row in cases if row['Vorgang'] in ('Rückgabe', 'Payment Dispute') and row['SKU'] and row['Zuordnung'] in ('Artikelzuordnung', 'Bestellbezug'))
    return {'cases': cases, 'partners': partner_texts, 'critical': sum(row['Priorität'].startswith('1') for row in cases),
            'today': sum(row['heute'] for row in cases), 'repeated': {k: v for k, v in repeated.items() if v > 1}}


def finance_check(snapshot):
    """Conservative evidence check; never writes or changes a settlement release."""
    payout = resource(snapshot, 'reference_payout')
    data = resource(snapshot, 'reference_transactions')
    issues, rows, seen = [], [], {}
    total = Decimal('0')
    if not payout or payout.get('payoutId') != PAYOUT:
        issues.append('Payout-Detaildaten fehlen oder haben eine andere ID.')
    if data is None:
        issues.append('Payout-Transaktionen nicht vollständig verfügbar.')
    payout_amount = money((payout or {}).get('amount'))
    expected_count = (payout or {}).get('transactionCount')
    if expected_count is not None and expected_count != len((data or {}).get('items', [])):
        issues.append('Transaktionsanzahl stimmt nicht mit dem Payout-Detail ueberein.')
    if payout_amount != BANK:
        issues.append('API-Payoutbetrag bestätigt 491,80 € nicht.')
    for tx in (data or {}).get('items', []):
        key = (tx.get('transactionId'), tx.get('transactionType'))
        if not all(key):
            issues.append('Transaktionsidentität unvollständig.')
        if key in seen:
            if seen[key] != tx:
                issues.append('Widersprüchliche doppelte Transaktions-ID.')
            continue
        seen[key] = tx
        amount = money(tx.get('amount'))
        status = tx.get('transactionStatus', '')
        booking = tx.get('bookingEntry')
        included = tx.get('payoutId') == PAYOUT and status == 'PAYOUT' and booking in ('CREDIT', 'DEBIT') and amount is not None
        signed = abs(amount) * (-1 if booking == 'DEBIT' else 1) if amount is not None and booking in ('CREDIT', 'DEBIT') else None
        if included:
            total += signed
        elif status not in ('FUNDS_ON_HOLD', 'FUNDS_PROCESSING', 'FUNDS_AVAILABLE_FOR_PAYOUT'):
            issues.append('Mindestens eine Bewegung hat keine eindeutige finale Payout-/Buchungszuordnung.')
        rows.append({**tx, 'berücksichtigt': included, 'vorzeichenbetrag': str(signed) if signed is not None else None})
    if total != BANK:
        issues.append('Summe der eindeutig final zugeordneten Bewegungen weicht vom Bankbetrag ab.')
    order_resources = {name: val for name, val in (snapshot or {}).get('resources', {}).items() if name.startswith('order_')}
    holds = []
    for val in order_resources.values():
        if val.get('available'):
            holds.extend(tx for tx in val['data'].get('items', []) if tx.get('transactionStatus') == 'FUNDS_ON_HOLD')
    unique_holds = {(tx.get('transactionId'), tx.get('transactionType')): tx for tx in holds}
    # Observed eBay hold bookkeeping is also PAYOUT. These are historical debit
    # movements, not a new current-state classification or settlement permission.
    booked_holds = [tx for tx in rows if tx.get('transactionType') == 'DISPUTE'
                    and tx.get('bookingEntry') == 'DEBIT'
                    and str(tx.get('transactionId', '')).startswith(('RETRO_HOLD-', 'DISPUTE_HOLD-'))]
    return {'reference': str(BANK), 'api_amount': str(payout_amount) if payout_amount is not None else None,
            'final_sum': str(total), 'difference': str(total - BANK), 'reconstructed': not issues,
            'issues': sorted(set(issues)), 'transactions': rows, 'order_holds': list(unique_holds.values()),
            'booked_hold_movements': booked_holds,
            'hold_coverage_complete': bool(order_resources) and all(v.get('available') for v in order_resources.values()),
            'note': 'Aktueller API-Status ist kein rückwirkender Nachweis zum Auszahlungsdatum. Bestellbezogene Holds ohne Payout-ID beweisen keine Zugehörigkeit zu diesem Bank-Payout. Keine automatische Abrechnungsfreigabe.'}
