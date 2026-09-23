"""Single derived status source for 2026-003+ rounds and their partners.

Nothing here is a stored field - every status is computed fresh, each call,
from the already-existing tables (group_b_rounds/group_b_round_positions,
partner_round_snapshots, partner_round_invoices, recovery_cases,
broker_commissions). No second
status logic may live in UI/export code going forward; they call
partner_status()/round_status() instead of re-deriving completion rules.

Deliberately scoped to 2026-003+ (source_kind='neutral_weekly') only.
Historical GB-2026-001/002 rounds keep their own old business logic
unchanged and unmigrated - is_neutral_round()/_require_neutral() refuse to
apply this module's rules to them at all, rather than reimplementing or
approximating the old completion rules here.
"""
import json
import logging
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import core
import group_b_rounds
import partner_snapshot
import position_workflow
import recovery_cases
import round_planner

BERLIN = ZoneInfo('Europe/Berlin')
logger = logging.getLogger(__name__)


def is_neutral_round(db, round_id):
    row = db.execute('SELECT source_kind FROM group_b_rounds WHERE id=?', (round_id,)).fetchone()
    return bool(row) and row['source_kind'] == 'neutral_weekly'


def _require_neutral(db, round_id):
    row = db.execute('SELECT * FROM group_b_rounds WHERE id=?', (round_id,)).fetchone()
    if not row:
        raise ValueError(f'{round_id} ist keine bekannte Runde.')
    if row['source_kind'] != 'neutral_weekly':
        raise ValueError(f'{round_id} nutzt die historische Geschäftslogik (source_kind={row["source_kind"]}); '
                          f'die zentrale 003+-Statuslogik gilt hier nicht.')
    return row


def partner_status(round_id, partner, business=None, now=None, db_context=None):
    """The full derived status for one confirmed partner in one 2026-003+
    round. Raises for a historical (non-neutral) round_id - callers must not
    apply this to 001/002.
    """
    import partner_round_invoices

    now_berlin = (now or datetime.now(BERLIN)).astimezone(BERLIN)
    business = position_workflow.positions() if business is None else business

    def _load(db):
        round_row = _require_neutral(db, round_id)
        window = json.loads(round_row['snapshot'])
        cut_passed = now_berlin >= datetime.fromisoformat(window['window_end'])
        assigned_keys = group_b_rounds.round_position_keys(db, round_id)
        snap = db.execute('SELECT * FROM partner_round_snapshots WHERE round_id=? AND partner=?',
                           (round_id, partner)).fetchone()
        invoice = db.execute('SELECT * FROM partner_round_invoices WHERE round_id=? AND partner=?',
                              (round_id, partner)).fetchone()
        return window, cut_passed, assigned_keys, (dict(snap) if snap else None), (dict(invoice) if invoice else None)

    if db_context is not None:
        window, cut_passed, assigned_keys, snap, invoice = _load(db_context)
        credit_status = recovery_cases.status(round_id, partner, db=db_context)
    else:
        with core.ledger() as db:
            partner_snapshot.initialize(db)
            partner_round_invoices.initialize(db)
            recovery_cases.initialize(db)
            window, cut_passed, assigned_keys, snap, invoice = _load(db)
            credit_status = recovery_cases.status(round_id, partner, db=db)

    if snap:
        positions = len(json.loads(snap['line_items']))
        claim = Decimal(snap['final_amount'])
        snapshot_status = 'vorhanden'
    else:
        rows = partner_snapshot._partner_round_rows(business, assigned_keys, partner)
        positions = int((rows.Art == 'Bestellung').sum()) if not rows.empty else 0
        claim = None  # not final until a real snapshot exists - never estimated here
        snapshot_status = 'kein_snapshot'

    if positions == 0:
        invoice_status = 'nicht_erforderlich'
        payment_status = 'nicht_erforderlich'
        paid_at = None
    else:
        if not snap:
            invoice_status = 'noch_nicht_moeglich'
        elif not invoice:
            invoice_status = 'fehlt'
        else:
            invoice_status = 'geprueft'
        if invoice and invoice.get('paid_at'):
            payment_status = 'bezahlt'
            paid_at = invoice['paid_at']
        else:
            payment_status = 'offen'
            paid_at = None

    invoice_number = invoice.get('invoice_number') if invoice else None

    # Each concrete blocking step is surfaced exactly once, at whichever
    # stage the partner is actually stuck - not every downstream consequence
    # of an earlier missing step (e.g. "noch_nicht_moeglich" alone explains
    # why payment is still 'offen' too; adding a redundant "Zahlung offen"
    # there would misleadingly suggest payment is already actionable).
    blockers = []
    if invoice_status == 'noch_nicht_moeglich':
        blockers.append('Finale Einzelabrechnung fehlt')
    elif invoice_status == 'fehlt':
        blockers.append('Rechnung fehlt')
    elif payment_status == 'offen':
        blockers.append('Zahlung offen')
    if credit_status == 'fehlt':
        blockers.append('Gutschrift fehlt')

    if positions == 0:
        overall = 'nichts_erforderlich'
    elif not cut_passed:
        overall = 'laufend'
    elif not blockers:
        overall = 'abgeschlossen'
    else:
        overall = 'in_Abwicklung'

    group = next((g for name, g in round_planner.confirmed_partners(business) if name == partner), None)

    return dict(
        round_id=round_id, partner=partner, group=group, positions=positions, claim=claim,
        snapshot_status=snapshot_status, invoice_status=invoice_status, invoice_number=invoice_number,
        payment_status=payment_status, paid_at=paid_at, credit_status=credit_status,
        overall_status=overall, blockers=blockers,
    )


def round_status(round_id, business=None, now=None):
    """The full derived status for one 2026-003+ round: every confirmed
    partner (including ones with 0 positions in this round - they never
    block), the round's own overall status, and a concrete blocker list
    ('Partner · Grund'), not just a count.

    Zusaetzlich unter 'broker' die davon vollstaendig getrennte Spur
    "Vermittlungsabrechnung Patrick -> Evelyn" (broker_commission.py) mit
    eigenem Status und eigener Zahlungsspur. Sie geht in den Rundenstatus
    ein, wird aber nie in die Blocker eines Partners gespiegelt: eine
    bezahlte Partnerposition darf dadurch nie wieder als "Partner noch zu
    bezahlen" erscheinen (und umgekehrt schliesst ein fertiger
    Vermittlungsbeleg keinen offenen Partner).

    Fail-soft: ein technischer Fehler beim Laden des Broker-Status (z.B.
    eine gegen die produktive DB fehlschlagende Query) darf niemals die
    fachlich unabhaengige Rundenanzeige (Partnerstatus, Einzelabrechnung,
    Historie) verhindern. 'broker' wird dann None und der Fehler steckt
    ausschliesslich in 'broker_error'; die Runde gilt in diesem Fall nie
    als 'abgeschlossen', sondern hoechstens 'in_Abwicklung'/'laufend'.
    """
    import broker_commission
    import partner_round_invoices

    now_berlin = (now or datetime.now(BERLIN)).astimezone(BERLIN)
    business = position_workflow.positions() if business is None else business

    with core.ledger() as db:
        partner_snapshot.initialize(db)
        partner_round_invoices.initialize(db)
        recovery_cases.initialize(db)
        round_row = _require_neutral(db, round_id)
        window = json.loads(round_row['snapshot'])
        cut_passed = now_berlin >= datetime.fromisoformat(window['window_end'])

        partners = [partner_status(round_id, name, business=business, now=now_berlin, db_context=db)
                    for name, _ in round_planner.confirmed_partners(business)]
        try:
            broker = broker_commission.status(round_id, business=business, db=db)
            broker_error = False
        except Exception:
            logger.exception('Broker-Commission-Status fuer Runde %s technisch nicht ladbar; '
                              'Rundenanzeige wird trotzdem fortgesetzt.', round_id)
            broker = None
            broker_error = True

    blockers = [f"{p['partner']} · {reason}" for p in partners for reason in p['blockers']]
    open_partners = {p['partner'] for p in partners if p['blockers']}

    # Eigene, von Partnerrechnung/-zahlung vollstaendig getrennte Spur: sie
    # wird NIE in p['blockers'] eines Partners gespiegelt, damit eine bereits
    # bezahlte Partnerposition nicht wegen des Vermittlungsbelegs wieder als
    # "Partner noch zu bezahlen" erscheint. Umgekehrt gilt dasselbe.
    broker_blockers = []
    if broker_error:
        # Technischer Fehler, keine fachliche Aussage moeglich - haelt die
        # Runde deshalb sicherheitshalber offen, statt zu raten.
        broker_blockers.append('Vermittlungsprovision Patrick → Evelyn · Status derzeit nicht verfügbar')
    elif broker['status'] == 'offen':
        broker_blockers.append('Vermittlungsprovision Patrick → Evelyn · Vermittlungsabrechnung offen')
    elif broker['status'] == 'erstellt' and broker['payment_status'] != 'bezahlt':
        broker_blockers.append('Vermittlungsprovision Patrick → Evelyn · Zahlung Evelyn → Patrick offen')
    # Eine dieser Runde zugeordnete, noch nicht verrechnete Provisions-
    # korrektur haelt die Runde offen - sie wird nie automatisch als erledigt
    # markiert und darf nicht verloren gehen.
    if not broker_error and broker.get('late_refunds'):
        broker_blockers.append(
            f"Vermittlungsprovision Patrick → Evelyn · {len(broker['late_refunds'])} "
            f"Provisionskorrektur(en) noch nicht verrechnet")

    if not cut_passed:
        status = 'laufend'
    elif blockers or broker_blockers:
        status = 'in_Abwicklung'
    else:
        status = 'abgeschlossen'

    return dict(
        round_id=round_id, window_start=window['window_start'], window_end=window['window_end'],
        round_status=status, partners=partners, open_partner_count=len(open_partners),
        blockers=blockers + broker_blockers, broker=broker, broker_error=broker_error,
    )
