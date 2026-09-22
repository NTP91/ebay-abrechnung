"""Vermittlungsabrechnung Patrick -> Evelyn, ab Runde 2026-003 ("003+").

Fachlich ein EIGENER Beleg, nicht die alte Gruppe-B-Sammelrechnung aus
GB-2026-001/002: jene Mechanik (RE0090/Lexware, evelyn_invoice_id,
'Patrick kassiert und stellt Evelyn eine Rechnung') wird hier weder gelesen
noch reaktiviert. Dieses Modul arbeitet ausschliesslich gegen neutrale
Wochenrunden (source_kind='neutral_weekly'); _require_neutral() weigert sich,
seine Regeln auf 001/002 anzuwenden.

Pro Runde gibt es genau EINEN Beleg - auch wenn die Runde MH-, NB- und
PM-Positionen mischt. Die Saetze sind pro Partner verschieden (Standard
Gruppe B 3,0 %, PM 2,0 %, Gruppe A strukturell 0,0 % und komplett
ausgeschlossen) und kommen ausnahmslos aus partner_conditions.

Basis ist die fachlich massgebliche Payout-/Positionsdaten der Runde (dieselben
`business`-Zeilen, die partner_snapshot und group_b_rounds.overview schon
nutzen) - nie ein Lexware-Beleg und nie eine Partnerrechnung als zweite
Geldquelle. Keine Schaetzungen, keine Bestellung ohne echten Payout, keine
offenen API-Holds.

Dublettenschutz (hoechste Prioritaet), dreifach:
  1. broker_commissions.round_id ist PRIMARY KEY  -> nie ein zweiter Beleg
     pro Runde; ein wiederholter finalize() ist ein echter No-op.
  2. broker_commission_positions.position_key ist PRIMARY KEY -> eine
     Position kann ueber ALLE Runden hinweg nur genau einmal eine
     Vermittlungsprovision ausloesen. Ein Wiedereroeffnen/Re-Import kann sie
     nicht ein zweites Mal einbringen; der INSERT scheitert hart.
  3. snapshot_hash friert Positionsmenge, Saetze und Betraege ein (dasselbe
     hash-gesicherte Insert-Once-Muster wie group_b_rounds._insert_round).
     Ein finalisierter Beleg wird nie neu gerechnet - status()/record()
     liefern immer den gespeicherten Stand, nie eine Live-Neuberechnung.
  4. broker_commission_corrections.refund_key ist PRIMARY KEY -> ein
     Erstattungsereignis erzeugt genau EINEN Provisionskorrekturfall, und das
     UPDATE beim Verrechnen greift nur bei settled_round_id IS NULL.

Spaetere (Teil-)Erstattung auf eine bereits abgerechnete Position:
detect_corrections() legt einen eigenen negativen Korrekturfall an (verknuepft
mit Ursprungsposition, -runde, -beleg, Erstattungsereignis und dem
urspruenglich angewendeten Satz). Der historische Beleg wird dabei NIE
veraendert oder wiedereroeffnet; die Korrektur wird in der naechsten offenen
Vermittlungsabrechnung als eigene negative Position verrechnet. Existiert noch
keine naechste offene Abrechnung, bleibt der Fall offen stehen - er wird nie
automatisch als erledigt markiert.
"""
import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal

import api_holds
import core
import partner_conditions
import partner_export
import position_workflow

PM = 'PM'


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS broker_commissions (
        round_id TEXT PRIMARY KEY, total_net TEXT NOT NULL, total_commission TEXT NOT NULL,
        breakdown TEXT NOT NULL, position_keys TEXT NOT NULL, position_count INTEGER NOT NULL,
        snapshot_hash TEXT NOT NULL UNIQUE, finalized_at TEXT NOT NULL,
        paid_at TEXT, paid_amount TEXT, paid_note TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS broker_commission_positions (
        position_key TEXT PRIMARY KEY, round_id TEXT NOT NULL,
        source TEXT NOT NULL,
        FOREIGN KEY(round_id) REFERENCES broker_commissions(round_id))''')
    # Write-once-Journal fuer die Aktivierung einer Sonderkondition (PM).
    db.execute('''CREATE TABLE IF NOT EXISTS partner_condition_activation (
        partner TEXT PRIMARY KEY, round_id TEXT NOT NULL, determined_at TEXT NOT NULL)''')
    # Provisionskorrekturfaelle: eine spaetere (Teil-)Erstattung auf eine
    # bereits abgerechnete Position. refund_key ist PRIMARY KEY - ein
    # Erstattungsereignis erzeugt damit genau EINEN Korrekturfall, auch bei
    # wiederholtem Import oder Re-Run.
    db.execute('''CREATE TABLE IF NOT EXISTS broker_commission_corrections (
        refund_key TEXT PRIMARY KEY, origin_position_key TEXT NOT NULL,
        origin_round_id TEXT NOT NULL, origin_snapshot_hash TEXT NOT NULL,
        partner TEXT NOT NULL, rate TEXT NOT NULL,
        refund_net TEXT NOT NULL, correction TEXT NOT NULL,
        detected_at TEXT NOT NULL, source TEXT NOT NULL,
        settled_round_id TEXT,
        FOREIGN KEY(origin_round_id) REFERENCES broker_commissions(round_id))''')


def _stable(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _require_neutral(db, round_id):
    row = db.execute('SELECT * FROM group_b_rounds WHERE id=?', (round_id,)).fetchone()
    if not row:
        raise ValueError(f'{round_id} ist keine bekannte Runde.')
    if row['source_kind'] != 'neutral_weekly':
        raise ValueError(f'{round_id} nutzt die historische Gruppe-B-Logik '
                         f'(source_kind={row["source_kind"]}); eine Vermittlungsabrechnung '
                         f'Patrick -> Evelyn existiert dort nicht und wird hier nicht erzeugt.')
    return row


def _commission_rows(business, assigned_keys):
    """Die provisionsrelevanten Zeilen einer Runde: ausschliesslich echte,
    ausgezahlte Gruppe-B-Verkaufspositionen dieser Runde plus die zu ihnen
    verlinkten Erstattungen (Fall A - eine Erstattung derselben Abrechnung
    mindert die Basis). Gruppe A ist hier strukturell nie enthalten.

    Ausgeschlossen: offener API-Hold, Pruefhinweis, Quellenpruefung, fehlender
    Payout (letzterer kann `business` ohnehin nie erreichen, core.
    load_master_data() filtert ihn vorher weg) - genau die Sperren, die
    round_planner.plan_round() schon bei der Rundenbildung anwendet, hier
    erneut gegen die LIVE-Daten geprueft, damit eine nachtraeglich gesetzte
    Sperre nie in eine Provision laeuft."""
    if business.empty or not assigned_keys:
        return business.iloc[0:0]
    sales = business[business.position_key.isin(assigned_keys)
                     & (business.Gruppe == 'Gruppe B')
                     & (business.Art == 'Bestellung')
                     & (business['Erlös_Brutto'] > 0)
                     & ~business['Prüfhinweis'].astype(bool)
                     & ~business.Quellenpruefung.astype(bool)
                     & ~api_holds.mask(business)
                     & business['Auszahlung Nr.'].astype(str).astype(bool)]
    if sales.empty:
        return sales
    sale_index = set(sales.index)
    links = core.refund_links(business)
    refunds = [refund for refund, sale in links.items() if sale in sale_index]
    return business.loc[sorted(sale_index | set(refunds))]


def basis(round_id, business=None, payouts=None, orders=None, db=None):
    """Die Live-Bemessungsgrundlage einer Runde, pro Partner aufgeschluesselt.

    Schreibt nichts und finalisiert nichts. Rueckgabe enthaelt je Partner die
    provisionsrelevante Netto-Basis, den verwendeten Satz und den
    Provisionsbetrag - damit ist die Summe immer partnerweise nachvollziehbar
    und PM kann nicht versehentlich mit 3 % laufen.

    Zusaetzlich werden alle derzeit OFFENEN Provisionskorrekturen aus
    frueheren Runden als eigene negative Positionen mitgefuehrt ('corrections')
    und fliessen in total_commission ein: sie werden in der naechsten offenen
    Vermittlungsabrechnung verrechnet, ohne den historischen Beleg anzufassen.
    Eine Runde, die ausschliesslich Korrekturen enthaelt, ist deshalb
    ebenfalls 'required'.
    """
    business = position_workflow.positions() if business is None else business
    payouts = core.read_master(core.PAYOUTS_DB_PATH) if payouts is None else payouts
    orders = core.read_master(core.ORDERS_DB_PATH) if orders is None else orders

    def _read(connection):
        initialize(connection)
        _require_neutral(connection, round_id)
        keys = {r[0] for r in connection.execute(
            'SELECT position_key FROM group_b_round_positions WHERE round_id=?', (round_id,))}
        detect_corrections(business=business, db=connection)
        # Eine bereits finalisierte Runde hat ihre Korrekturen schon
        # gebunden; nur eine noch offene Runde nimmt neue auf.
        finalized = connection.execute('SELECT 1 FROM broker_commissions WHERE round_id=?',
                                       (round_id,)).fetchone()
        pending = [] if finalized else [
            row for row in corrections(open_only=True, db=connection)
            if row['origin_round_id'] != round_id]
        return keys, pending

    assigned_keys, pending = _read(db) if db is not None else _read_with_own_ledger(_read)

    rows = _commission_rows(business, assigned_keys)
    partners, total_net, total_commission, keys = [], Decimal(0), Decimal(0), []
    for partner in sorted(rows.Partner.unique()) if not rows.empty else []:
        block = rows[rows.Partner == partner]
        model = partner_export.prepare_partner_export(block, payouts, orders, statement_type='partner')
        # Netto nach derselben USt-Systematik wie ueberall sonst
        # (eBay_Netto = brutto/1,19), Fall-A-Erstattungen mindern die Basis.
        net = model['totals']['Rechnung']['net'] + model['totals']['Gutschriften']['net']
        rate = partner_conditions.broker_rate(partner, 'Gruppe B')
        commission = partner_export.cents(net * rate)
        sale_keys = sorted(block.loc[block.Art == 'Bestellung', 'position_key'])
        partners.append(dict(partner=partner, positions=len(sale_keys), net_basis=net,
                             rate=rate, commission=commission))
        total_net += net
        total_commission += commission
        keys.extend(sale_keys)
    correction_rows = [dict(refund_key=row['refund_key'], origin_round_id=row['origin_round_id'],
                            origin_position_key=row['origin_position_key'], partner=row['partner'],
                            rate=Decimal(row['rate']), refund_net=Decimal(row['refund_net']),
                            correction=Decimal(row['correction'])) for row in pending]
    total_corrections = sum((row['correction'] for row in correction_rows), Decimal(0))
    return dict(round_id=round_id, partners=partners, total_net=total_net,
                corrections=correction_rows, total_corrections=total_corrections,
                total_commission=total_commission + total_corrections,
                commission_before_corrections=total_commission,
                position_keys=sorted(keys),
                required=bool(partners) or bool(correction_rows))


def _read_with_own_ledger(fn):
    with core.ledger() as own_db:
        return fn(own_db)


def pm_effective_round(business=None, db=None):
    """Die erste Runde, ab der PMs Sonderkondition operativ wirksam ist.

    Regel: die frueheste neutrale Runde (2026-003+), in der eine echte,
    abrechenbare PM-Position mit tatsaechlicher eBay-Auszahlungsnummer liegt.
    Eine Bestellung ohne Payout aktiviert nichts.

    Die einmal getroffene Feststellung wird in partner_condition_activation
    write-once persistiert und verschiebt sich danach NICHT mehr. Ein
    spaeterer Re-Import, der PM-Daten in eine FRUEHERE Runde nachtraegt, kann
    die Wirksamkeit also nicht rueckwirkend vorziehen - der gespeicherte Wert
    gewinnt. Ohne echte PM-Position gibt es (noch) keine Feststellung: None.
    """
    business = position_workflow.positions() if business is None else business

    def _run(connection):
        initialize(connection)
        stored = connection.execute(
            'SELECT round_id FROM partner_condition_activation WHERE partner=?', (PM,)).fetchone()
        if stored:
            return stored[0]
        rounds = [r[0] for r in connection.execute(
            "SELECT id FROM group_b_rounds WHERE source_kind='neutral_weekly' ORDER BY year,sequence")]
        for rid in rounds:
            keys = {r[0] for r in connection.execute(
                'SELECT position_key FROM group_b_round_positions WHERE round_id=?', (rid,))}
            rows = _commission_rows(business, keys)
            if rows.empty or not (rows.Partner == PM).any():
                continue
            now = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
            connection.execute('INSERT OR IGNORE INTO partner_condition_activation VALUES(?,?,?)',
                               (PM, rid, now))
            connection.commit()
            row = connection.execute(
                'SELECT round_id FROM partner_condition_activation WHERE partner=?', (PM,)).fetchone()
            return row[0]
        return None

    return _run(db) if db is not None else _read_with_own_ledger(_run)


def finalize(round_id, now=None, business=None, payouts=None, orders=None):
    """Den Vermittlungsbeleg der Runde einmalig und unveraenderlich erzeugen.

    Erlaubt erst nach dem Sonntags-23:59-Cut der Runde (vorher gibt es nur
    den Live-Zwischenstand aus basis()). Ein zweiter Aufruf ist ein echter
    No-op und liefert den gespeicherten Beleg unveraendert zurueck - er
    rechnet nichts neu und legt insbesondere keinen zweiten Provisionsfall an.

    Rueckgabe (record_or_None, created). record ist None, wenn die Runde keine
    provisionsrelevante Gruppe-B-Position hat ("nicht erforderlich").
    """
    from zoneinfo import ZoneInfo
    berlin = ZoneInfo('Europe/Berlin')
    now_berlin = (now or datetime.now(berlin)).astimezone(berlin)
    business = position_workflow.positions() if business is None else business
    payouts = core.read_master(core.PAYOUTS_DB_PATH) if payouts is None else payouts
    orders = core.read_master(core.ORDERS_DB_PATH) if orders is None else orders

    with core.ledger() as db:
        initialize(db)
        round_row = _require_neutral(db, round_id)
        window = json.loads(round_row['snapshot'])
        if now_berlin < datetime.fromisoformat(window['window_end']):
            raise ValueError(f'{round_id} ist noch laufend; die Vermittlungsabrechnung '
                             f'Patrick -> Evelyn kann erst nach dem Cut erstellt werden.')
        existing = db.execute('SELECT * FROM broker_commissions WHERE round_id=?', (round_id,)).fetchone()
        if existing:
            return dict(existing), False

        model = basis(round_id, business=business, payouts=payouts, orders=orders, db=db)
        if not model['required']:
            return None, False
        pm_effective_round(business=business, db=db)

        breakdown = [dict(partner=p['partner'], positions=p['positions'],
                          net_basis=str(p['net_basis']), rate=str(p['rate']),
                          commission=str(p['commission'])) for p in model['partners']]
        settled_corrections = [row['refund_key'] for row in model['corrections']]
        payload = dict(round_id=round_id, breakdown=breakdown,
                       position_keys=model['position_keys'],
                       corrections=sorted(settled_corrections),
                       total_corrections=str(model['total_corrections']),
                       total_net=str(model['total_net']),
                       total_commission=str(model['total_commission']))
        digest = _hash(_stable(payload))
        sources = {row.position_key: position_workflow.source_snapshot(row)
                   for _, row in business[business.position_key.isin(model['position_keys'])].iterrows()}

        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM broker_commissions WHERE round_id=?', (round_id,)).fetchone():
            db.rollback()
            return dict(db.execute('SELECT * FROM broker_commissions WHERE round_id=?',
                                   (round_id,)).fetchone()), False
        finalized_at = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        db.execute('INSERT INTO broker_commissions VALUES(?,?,?,?,?,?,?,?,?,?,?)', (
            round_id, str(model['total_net']), str(model['total_commission']),
            json.dumps(breakdown, ensure_ascii=False),
            json.dumps(model['position_keys'], ensure_ascii=False), len(model['position_keys']),
            digest, finalized_at, None, None, None))
        for key in model['position_keys']:
            # PRIMARY KEY(position_key): schlaegt hart fehl, falls diese
            # Position schon irgendeiner anderen Vermittlungsabrechnung
            # zugrunde liegt - lieber Abbruch als eine zweite Provision.
            db.execute('INSERT INTO broker_commission_positions VALUES(?,?,?)',
                       (key, round_id, sources.get(key, '')))
        for refund_key in settled_corrections:
            # Nur einen noch OFFENEN Fall binden. Das WHERE settled_round_id
            # IS NULL macht ein zweites Verrechnen desselben Korrekturfalls
            # strukturell unmoeglich, auch bei gleichzeitigen Laeufen.
            updated = db.execute(
                'UPDATE broker_commission_corrections SET settled_round_id=? '
                'WHERE refund_key=? AND settled_round_id IS NULL',
                (round_id, refund_key)).rowcount
            if updated != 1:
                db.rollback()
                raise ValueError(f'Provisionskorrektur {refund_key} wurde zwischenzeitlich '
                                 f'bereits verrechnet; Vermittlungsabrechnung abgebrochen.')
        db.commit()
        record = dict(db.execute('SELECT * FROM broker_commissions WHERE round_id=?', (round_id,)).fetchone())
    return record, True


def confirm_payment(round_id, paid_date=None, note=''):
    """'Zahlung Evelyn -> Patrick' dokumentieren. Idempotent: ein zweiter
    Aufruf fuer eine bereits bezahlte Runde ist ein echter No-op."""
    with core.ledger() as db:
        initialize(db)
        row = db.execute('SELECT * FROM broker_commissions WHERE round_id=?', (round_id,)).fetchone()
        if not row:
            raise ValueError(f'Fuer {round_id} existiert noch keine Vermittlungsabrechnung; '
                             f'Zahlung nicht moeglich.')
        if row['paid_at']:
            return dict(row), False
        value = date.fromisoformat(str(paid_date)) if paid_date else date.today()
        if value > date.today():
            raise ValueError('Ein zukuenftiges Zahlungsdatum ist nicht zulaessig.')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM broker_commissions WHERE round_id=?', (round_id,)).fetchone()
        if row['paid_at']:
            db.rollback()
            return dict(row), False
        db.execute('UPDATE broker_commissions SET paid_at=?, paid_amount=?, paid_note=? WHERE round_id=?',
                   (value.isoformat(), row['total_commission'], note or '', round_id))
        db.commit()
        record = dict(db.execute('SELECT * FROM broker_commissions WHERE round_id=?', (round_id,)).fetchone())
    return record, True


def _origin_rate(record, partner):
    """Der URSPRUENGLICH angewendete Satz aus dem eingefrorenen Beleg - nie
    der heutige Satz aus partner_conditions. Aendert sich eine Kondition
    spaeter, wird eine Altkorrektur trotzdem mit dem Satz gerechnet, mit dem
    die Position tatsaechlich abgerechnet wurde."""
    for item in json.loads(record['breakdown']):
        if item['partner'] == partner:
            return Decimal(item['rate'])
    raise ValueError(f'{partner} kommt im eingefrorenen Beleg {record["round_id"]} nicht vor.')


def detect_corrections(business=None, db=None):
    """Provisionskorrekturfaelle fuer spaetere (Teil-)Erstattungen anlegen.

    Fachregel: wird eine Position nach Finalisierung der Vermittlungs-
    abrechnung ganz oder teilweise erstattet, wird die bereits berechnete
    Provision anteilig korrigiert - aber NIE durch Aendern oder Wiederoeffnen
    des historischen Belegs. Stattdessen entsteht ein eigener, negativer
    Korrekturfall, eindeutig verknuepft mit Ursprungsposition, Ursprungsrunde,
    Ursprungsbeleg (snapshot_hash), Erstattungsereignis und dem urspruenglich
    angewendeten Provisionssatz.

    Betrag = erstatteter Nettoanteil * urspruenglicher Satz. Eine
    Vollerstattung ergibt damit automatisch die vollstaendige Korrektur, eine
    Teilerstattung den exakten Anteil - ohne zweite Rechenregel.

    Dublettenschutz: refund_key ist PRIMARY KEY. Ein erneuter Import oder
    Re-Run desselben Erstattungsereignisses erzeugt keinen zweiten Fall.

    Rueckgabe: Liste der in DIESEM Aufruf neu angelegten Faelle.
    """
    live = position_workflow.positions() if business is None else business

    def _run(connection):
        initialize(connection)
        if live.empty:
            return []
        finalized = [dict(row) for row in connection.execute('SELECT * FROM broker_commissions')]
        if not finalized:
            return []
        origin_of = {}
        for record in finalized:
            for key in json.loads(record['position_keys']):
                origin_of[key] = record
        by_index = {index: row for index, row in live.iterrows()}
        created = []
        for refund_index, sale_index in core.refund_links(live).items():
            refund, sale = by_index[refund_index], by_index[sale_index]
            record = origin_of.get(sale.position_key)
            if record is None:
                continue  # Position war nie Basis einer Vermittlungsabrechnung
            if connection.execute('SELECT 1 FROM broker_commission_corrections WHERE refund_key=?',
                                  (refund.position_key,)).fetchone():
                continue
            rate = _origin_rate(record, str(sale.Partner))
            refund_net = Decimal(str(refund['eBay_Netto']))
            correction = partner_export.cents(refund_net * rate)
            now = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
            connection.execute(
                'INSERT OR IGNORE INTO broker_commission_corrections VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (refund.position_key, sale.position_key, record['round_id'],
                 record['snapshot_hash'], str(sale.Partner), str(rate),
                 str(refund_net), str(correction), now,
                 position_workflow.source_snapshot(refund), None))
            created.append(dict(refund_key=refund.position_key, origin_position_key=sale.position_key,
                                origin_round_id=record['round_id'], partner=str(sale.Partner),
                                rate=rate, refund_net=refund_net, correction=correction))
        if created:
            connection.commit()
        return created

    return _run(db) if db is not None else _read_with_own_ledger(_run)


def corrections(round_id=None, open_only=False, db=None):
    """Korrekturfaelle lesen. round_id filtert auf die URSPRUNGSrunde,
    open_only auf noch nicht verrechnete Faelle."""
    def _run(connection):
        initialize(connection)
        sql = 'SELECT * FROM broker_commission_corrections'
        clauses, params = [], []
        if round_id is not None:
            clauses.append('origin_round_id=?')
            params.append(round_id)
        if open_only:
            clauses.append('settled_round_id IS NULL')
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        return [dict(row) for row in connection.execute(sql + ' ORDER BY detected_at, refund_key', params)]

    return _run(db) if db is not None else _read_with_own_ledger(_run)


def late_refund_flags(round_id, business=None, db=None):
    """Die noch OFFENEN (nicht verrechneten) Provisionskorrekturen, deren
    Ursprung diese Runde ist. Solange davon etwas offen ist, gilt die Runde
    nicht als vollstaendig abgeschlossen - der Fall darf nicht verloren gehen
    und wird nie automatisch als erledigt markiert."""
    # Vor dem Oeffnen der Ledger-Verbindung aufloesen: position_workflow.
    # positions() oeffnet intern eine eigene core.ledger(), und deren
    # FileLock ist nicht reentrant.
    live = position_workflow.positions() if business is None else business

    def _run(connection):
        detect_corrections(business=live, db=connection)
        return [dict(refund_key=row['refund_key'], origin_position_key=row['origin_position_key'],
                     partner=row['partner'], rate=Decimal(row['rate']),
                     betrag=Decimal(row['refund_net']), correction=Decimal(row['correction']))
                for row in corrections(round_id=round_id, open_only=True, db=connection)]

    return _run(db) if db is not None else _read_with_own_ledger(_run)


def status(round_id, business=None, payouts=None, orders=None, db=None):
    """Der abgeleitete Status der Vermittlungsabrechnung einer Runde -
    vollstaendig unabhaengig von Rechnung/Zahlung der einzelnen Partner.

    'nicht_erforderlich'  Runde hat keine provisionsrelevante Gruppe-B-Position
    'offen'               erforderlich, aber noch kein Beleg
    'erstellt'            Beleg dokumentiert (unveraenderlich)
    zusaetzlich getrennt:  payment_status 'offen' / 'bezahlt'
    """
    live = position_workflow.positions() if business is None else business

    def _run(connection):
        initialize(connection)
        _require_neutral(connection, round_id)
        row = connection.execute('SELECT * FROM broker_commissions WHERE round_id=?', (round_id,)).fetchone()
        if row:
            record = dict(row)
            # Erst erkennen, dann lesen - sonst liefe die Liste der eigenen
            # Korrekturfaelle dem gerade erkannten Fall eine Runde hinterher.
            flags = late_refund_flags(round_id, business=live, db=connection)
            own = corrections(round_id=round_id, db=connection)
            return dict(round_id=round_id, status='erstellt',
                        total_commission=Decimal(record['total_commission']),
                        total_net=Decimal(record['total_net']),
                        breakdown=json.loads(record['breakdown']),
                        finalized_at=record['finalized_at'],
                        payment_status='bezahlt' if record['paid_at'] else 'offen',
                        paid_at=record['paid_at'],
                        corrections=[], total_corrections=Decimal(0),
                        origin_corrections=own, late_refunds=flags)
        model = basis(round_id, business=live, payouts=payouts, orders=orders, db=connection)
        return dict(round_id=round_id,
                    status='offen' if model['required'] else 'nicht_erforderlich',
                    total_commission=model['total_commission'], total_net=model['total_net'],
                    breakdown=[dict(partner=p['partner'], positions=p['positions'],
                                    net_basis=str(p['net_basis']), rate=str(p['rate']),
                                    commission=str(p['commission'])) for p in model['partners']],
                    finalized_at=None,
                    payment_status='nicht_erforderlich' if not model['required'] else 'offen',
                    paid_at=None,
                    corrections=model['corrections'], total_corrections=model['total_corrections'],
                    origin_corrections=corrections(round_id=round_id, db=connection),
                    late_refunds=late_refund_flags(round_id, business=live, db=connection))

    return _run(db) if db is not None else _read_with_own_ledger(_run)


LABELS = {
    'nicht_erforderlich': '➖ nicht erforderlich',
    'offen': '❌ Vermittlungsabrechnung offen',
    'erstellt': '✅ Vermittlungsabrechnung erstellt/dokumentiert',
}


CORRECTION_LABELS = {'offen': '❌ offen', 'verrechnet': '✅ verrechnet'}


def label(result):
    """Einzeiler fuer die Rundenuebersicht, inkl. getrennter Zahlungsspur."""
    text = LABELS[result['status']]
    if result['status'] == 'erstellt':
        text += ' · Zahlung Evelyn → Patrick ' + ('✅ erfolgt' if result['payment_status'] == 'bezahlt' else '❌ offen')
    if result.get('late_refunds'):
        text += (f" · Korrekturen {CORRECTION_LABELS['offen']}"
                 f" ({len(result['late_refunds'])})")
    if result.get('corrections'):
        text += f" · inkl. {len(result['corrections'])} Provisionskorrektur(en)"
    return text


def correction_rows(result):
    """Die UI-Zeilen fuer 'Vermittlungsprovision · Korrekturen / Erstattungen'
    einer Runde: die aus dieser Runde stammenden Faelle mit ihrem Status, plus
    die in dieser Runde verrechneten Faelle aus frueheren Runden."""
    rows = []
    for row in result.get('origin_corrections', []):
        rows.append(dict(refund_key=row['refund_key'], partner=row['partner'],
                         rate=Decimal(row['rate']), correction=Decimal(row['correction']),
                         origin_round_id=row['origin_round_id'],
                         settled_round_id=row['settled_round_id'],
                         status='verrechnet' if row['settled_round_id'] else 'offen'))
    for row in result.get('corrections', []):
        rows.append(dict(refund_key=row['refund_key'], partner=row['partner'],
                         rate=row['rate'], correction=row['correction'],
                         origin_round_id=row['origin_round_id'],
                         settled_round_id=result['round_id'], status='verrechnet'))
    return rows
