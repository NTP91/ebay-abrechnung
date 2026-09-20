"""Planning simulation and idempotent creation for the neutral post-2026-002
settlement round (2026-003+).

plan_round() never writes anything -- no group_b_rounds/group_b_round_positions
insert, no status field, no Excel/Lexware call. Every number is derived fresh
from position_workflow.positions() and the existing historical round
assignment table at call time, exactly the sources the rest of the app already
treats as authoritative.

commit_round() is the one function in this module that writes: it reuses
group_b_rounds._insert_round()'s exact hash-locked insert/idempotency pattern
and the same group_b_rounds/group_b_round_positions tables - no new schema, no
'GB-' id, no RE0090/Lexware dependency, and no touch of an existing round's
rows (a real INSERT OR REPLACE never happens for position_key values already
present - the table's PRIMARY KEY on position_key makes a second assignment a
hard error, not a silent overwrite).

Explicitly independent of the historical Gruppe-B/Lexware model throughout:
no RE0090 lookup, no has_re0090 gate, no Patrick-collects-then-invoices-Evelyn
export mode, no evelyn_invoice_id/evelyn_document_number. Rounds 001/002 keep
that model unchanged; this module only ever reasons about 003+.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import core
import position_workflow

BERLIN = ZoneInfo('Europe/Berlin')
FIRST_NEUTRAL_YEAR = 2026
FIRST_NEUTRAL_SEQUENCE = 3
CUT_HOUR, CUT_MINUTE = 23, 59
GROUP_A_PARTNERS = ('PP', 'BA', 'MK', '001')
# core.normalized_partner() collapses every 'MH...'-prefixed SKU to the single
# partner code 'MH', and core.load_master_data() recognizes it the same way
# regardless of config/partners.json - mirrored here so MH is never
# misclassified as an unconfirmed prefix by this simulation.
GROUP_B_BUILTIN_PARTNERS = ('MH',)


def round_id(year, sequence):
    return f'{year}-{sequence:03d}'


def _cut_instant(sunday_date):
    """The exact Sunday 23:59 Europe/Berlin instant a round ends at."""
    return datetime.combine(sunday_date, time(CUT_HOUR, CUT_MINUTE), BERLIN)


def round_for_sequence(sequence, base_cut):
    """The half-open [start, end) window for 2026-0<sequence>, counting forward
    in exact 7-day steps from base_cut (the cut instant that ENDS
    FIRST_NEUTRAL_SEQUENCE, i.e. 2026-003's own Sunday 23:59)."""
    offset = sequence - FIRST_NEUTRAL_SEQUENCE
    end = base_cut + timedelta(days=7 * offset)
    start = end - timedelta(days=7)
    return dict(id=round_id(FIRST_NEUTRAL_YEAR, sequence), sequence=sequence, start=start, end=end)


def default_base_cut(now):
    """Absent an explicit anchor and absent any already-created 2026-003+ round,
    the first neutral round is defined as the week containing `now`: its cut is
    the next Sunday 23:59 Europe/Berlin at or after `now` (today itself if `now`
    is already at/after that Sunday's cut). This makes a first-ever planner run
    always describe "the current week" instead of guessing a business decision
    the caller didn't supply. Once a real 2026-003+ round exists, its own
    recorded end should be passed in as `base_cut` instead."""
    days_to_sunday = (6 - now.weekday()) % 7
    candidate_sunday = now.date() + timedelta(days=days_to_sunday)
    cut = _cut_instant(candidate_sunday)
    if now >= cut:
        cut = cut + timedelta(days=7)
    return cut


def current_sequence(now, base_cut):
    """Which 2026-0xx sequence number is in flight (or just starting) at `now`."""
    if now < base_cut:
        return FIRST_NEUTRAL_SEQUENCE
    # now is exactly at/after base_cut: that instant already belongs to the
    # NEXT round (round_for_sequence's window is half-open [start, end)).
    weeks_after = (now - base_cut).days // 7
    return FIRST_NEUTRAL_SEQUENCE + 1 + weeks_after


def _historical_round_positions(db):
    """position_key -> round_id for every position already assigned to ANY
    existing round (001/002 today; would include any real 003+ round later)."""
    return {row['position_key']: row['round_id']
            for row in db.execute('SELECT position_key, round_id FROM group_b_round_positions')}


def confirmed_partners(business):
    """Group A's fixed set plus Group B's persisted, durable confirmation list
    (config/partners.json via core.known_group_b_partners()) - the same
    dynamic partner store already used everywhere else, not a new one."""
    group_b = sorted(set(core.known_group_b_partners()) | set(GROUP_B_BUILTIN_PARTNERS))
    return [(p, 'Gruppe A') for p in GROUP_A_PARTNERS] + [(p, 'Gruppe B') for p in group_b]


def plan_round(now=None, base_cut=None, open_round_ids=(), business=None, payouts=None, db=None):
    """Compute (never write) the full planning picture for the neutral round
    that is current as of `now`.

    now: aware datetime (any tz; converted to Europe/Berlin). Defaults to the
        current time.
    base_cut: the Sunday-23:59-Berlin instant that ENDS FIRST_NEUTRAL_SEQUENCE
        (2026-003). Defaults to default_base_cut(now) - see its docstring.
    open_round_ids: 2026-0xx round ids (strictly older than the round being
        planned) that are NOT yet final - i.e. a late payout dated into one of
        them may still retroactively belong there (rule 4). Empty by default:
        with no 003+ round created yet, there is nothing to reopen. Rounds
        001/002 are never in this set - they are always final (rule 2).
    business, payouts, db: injectable for tests; default to the live
        position_workflow.positions() / core.read_master(PAYOUTS) / a fresh
        read-only in-memory decode of state/settlement.sqlite3.
    """
    now = (now or datetime.now(BERLIN)).astimezone(BERLIN)
    base_cut = base_cut or default_base_cut(now)
    sequence = current_sequence(now, base_cut)
    window = round_for_sequence(sequence, base_cut)
    # A same-day trigger, not a durable state: True exactly while `now` is at
    # or past this Sunday's 23:59 Berlin cut - the instant a rollover to the
    # next round is required. current_sequence() has, by construction, already
    # advanced to that next round by then, so it is always 'laufend' from its
    # own perspective; this flag is the signal a caller uses to notice the
    # rollover just happened (Monday onward it is False again).
    cut_passed = now.weekday() == 6 and now.time() >= time(CUT_HOUR, CUT_MINUTE)
    open_round_ids = set(open_round_ids)

    business = position_workflow.positions() if business is None else business
    payouts = core.read_master(core.PAYOUTS_DB_PATH) if payouts is None else payouts

    if db is None:
        # core.ledger() already abstracts local-file vs. Supabase; a pure SELECT
        # here never changes the connection's bytes, so its own before/after
        # diff on exit correctly skips any write-back.
        with core.ledger() as own_db:
            historical_assignment = _historical_round_positions(own_db)
    else:
        historical_assignment = _historical_round_positions(db)

    payout_dates = {}
    if not payouts.empty:
        for payout_id, block in payouts.groupby('Auszahlung Nr.'):
            if not payout_id:
                continue
            text = core.clean(block.iloc[0].get('Auszahlungsdatum', ''))
            if not text:
                continue
            try:
                payout_dates[payout_id] = datetime.strptime(text, '%d.%m.%Y').replace(tzinfo=BERLIN)
            except ValueError:
                continue

    confirmed = confirmed_partners(business)
    confirmed_names = {name for name, _ in confirmed}
    # The fixed start of 2026-003 itself, regardless of which sequence is
    # currently being planned - anything older belongs to the pre-neutral
    # 001/002 model (rule 2: always final, never a "2026-00x" round id).
    earliest_neutral_start = round_for_sequence(FIRST_NEUTRAL_SEQUENCE, base_cut)['start']

    # core.load_master_data() (position_workflow.positions()'s own source)
    # already drops every row without an 'Auszahlung Nr.' before business ever
    # sees it - Gruppe A and Gruppe B alike, no exception. So a "no payout yet"
    # order structurally never reaches the loop below; it is found instead via
    # the same existing open-transaction report the rest of the app uses.
    import studio_view
    open_orders_from_master = [dict(Bestellnummer=r['Bestellnummer'], Partner=r['Partner'])
                                for _, r in studio_view.open_positions(payouts).iterrows()]

    included = []
    excluded = {
        'historisch_zugeordnet': [],       # already tied to a specific round (001/002 or any earlier 003+ round)
        # no real eBay payout yet - stays outside every round (Gruppe A and B alike).
        # Starts with the open-transaction report's own findings; the defensive
        # guard below appends to this same list if a `business` row ever slips
        # through load_master_data's filter without a payout.
        'kein_payout': list(open_orders_from_master),
        'bereits_bezahlt_abgeschlossen': [],  # closed_at / paid_at / paid_without_invoice_at already set
        'ungeklaerte_sperre': [],          # Prüfhinweis / Quellenpruefung / API-Hold, unrelated to partner recognition
        'unbekannter_partner': [],         # SKU prefix not (yet) a confirmed partner
        'aeltere_offene_runde': [],        # payout date belongs to an older, still-open 003+ round instead
    }
    unknown_prefixes = {}

    for _, row in business.iterrows():
        if row['Art'] != 'Bestellung':
            continue
        payout_id = row['Auszahlung Nr.']
        position_key = row['position_key']

        # payout_id is always non-empty here (see the studio_view.open_positions
        # note above) - kept as a defensive, never-expected-to-fire guard so a
        # future change to load_master_data's filtering can never silently
        # let an unpaid order through.
        if not payout_id:
            excluded['kein_payout'].append(dict(position_key=position_key, Bestellnummer=row['Bestellnummer']))
            continue

        if position_key in historical_assignment:
            excluded['historisch_zugeordnet'].append(dict(
                position_key=position_key, Bestellnummer=row['Bestellnummer'],
                round=historical_assignment[position_key]))
            continue

        if bool(row.get('closed_at')) or bool(row.get('paid_at')) or bool(row.get(position_workflow.PAID_WITHOUT_INVOICE)):
            excluded['bereits_bezahlt_abgeschlossen'].append(dict(
                position_key=position_key, Bestellnummer=row['Bestellnummer'],
                paid_without_invoice=bool(row.get(position_workflow.PAID_WITHOUT_INVOICE))))
            continue

        if row['Prüfhinweis'] and 'unbekannter Partner' not in str(row['Prüfhinweis']):
            excluded['ungeklaerte_sperre'].append(dict(position_key=position_key, Bestellnummer=row['Bestellnummer'], Prüfhinweis=row['Prüfhinweis']))
            continue
        if row.get('Quellenpruefung') or bool(row.get('API_Hold', False)):
            excluded['ungeklaerte_sperre'].append(dict(position_key=position_key, Bestellnummer=row['Bestellnummer'], Prüfhinweis=row['Prüfhinweis'] or 'Sperre'))
            continue

        if row['Partner'] not in confirmed_names or 'unbekannter Partner' in str(row['Prüfhinweis']):
            unknown_prefixes.setdefault(row['Partner'], 0)
            unknown_prefixes[row['Partner']] += 1
            excluded['unbekannter_partner'].append(dict(position_key=position_key, Bestellnummer=row['Bestellnummer'], Partner=row['Partner']))
            continue

        payout_date = payout_dates.get(payout_id)
        if payout_date is not None and payout_date < window['start']:
            if payout_date >= earliest_neutral_start:
                weeks_back = (window['start'] - payout_date).days // 7 + 1
                home_round = round_id(FIRST_NEUTRAL_YEAR, sequence - weeks_back)
            else:
                home_round = None  # older than any neutral round -> 001/002 territory, always final
            if home_round is not None and home_round in open_round_ids:
                excluded['aeltere_offene_runde'].append(dict(
                    position_key=position_key, Bestellnummer=row['Bestellnummer'],
                    payout_date=payout_date.date().isoformat(), gehoert_zu=home_round))
                continue
            # else: that older round (or 001/002) is final -> falls forward into the round being planned (rule 4).

        included.append(dict(
            position_key=position_key, Bestellnummer=row['Bestellnummer'], Partner=row['Partner'],
            Gruppe=row['Gruppe'], Betrag=Decimal(str(row['Erlös_Brutto'])), Payout=payout_id,
            payout_date=payout_date.date().isoformat() if payout_date else None))

    by_partner = {}
    for item in included:
        by_partner.setdefault(item['Partner'], []).append(item)

    partners_view = []
    for name, group in confirmed:
        positions = by_partner.get(name, [])
        claim = sum((p['Betrag'] for p in positions), Decimal('0'))
        partners_view.append(dict(
            partner=name, group=group, positions=len(positions), claim=claim,
            label=(f'{len(positions)} Positionen · {claim} EUR' if positions else '0 Positionen · nichts erforderlich')))

    total_positions = len(included)
    total_claim = sum((p['Betrag'] for p in included), Decimal('0'))
    # The round this function returns is, by construction, always the one
    # whose window currently contains `now` - so it is 'laufend' except in the
    # single-instant transition window flagged by cut_passed above.
    derived_status = 'in Abwicklung' if cut_passed else 'laufend'

    return dict(
        now=now, base_cut=base_cut, round_id=window['id'], sequence=sequence,
        start=window['start'], end=window['end'],
        cut_passed=cut_passed, next_round_required=cut_passed,
        derived_status=derived_status,
        partners=partners_view, included=included, excluded=excluded,
        unknown_prefixes=unknown_prefixes, orders_without_payout=excluded['kein_payout'],
        total_positions=total_positions, total_claim=total_claim,
    )


def commit_round(now=None, base_cut=None, open_round_ids=()):
    """Idempotently create the neutral round plan_round() reports as current.

    True no-op on repeat: once a round exists, its own assigned positions
    become 'historisch_zugeordnet' from plan_round()'s own point of view, so
    re-planning after creation would (correctly) describe a *smaller* round
    than the one just created - comparing that against the original insert's
    snapshot would look like drift, not stability. So existence is checked
    FIRST, directly against group_b_rounds, before any (re-)planning happens
    at all: if the round already exists, this returns immediately with
    `created=False` and does not touch plan_round or group_b_round_positions
    again. Only a round that does not exist yet is actually planned and
    inserted, via group_b_rounds._insert_round() - the same hash-locked
    insert this module deliberately did not reimplement.

    Every accepted position already went through plan_round()'s payout/
    partner/history/lock checks identically for Gruppe A and Gruppe B - there
    is no group-specific branch anywhere in this path.

    Returns (round_id, created, plan_or_None). plan is None when created is
    False because the round already existed (no re-planning was done).
    """
    import group_b_rounds

    now_berlin = (now or datetime.now(BERLIN)).astimezone(BERLIN)
    base_cut_value = base_cut or default_base_cut(now_berlin)
    sequence = current_sequence(now_berlin, base_cut_value)
    round_id_value = round_id(FIRST_NEUTRAL_YEAR, sequence)

    with core.ledger() as db:
        group_b_rounds.initialize(db)
        if db.execute('SELECT 1 FROM group_b_rounds WHERE id=?', (round_id_value,)).fetchone():
            return round_id_value, False, None

    # Gather everything read-only, outside any lock: position_workflow.positions()
    # and the ledger() read below each open their own short-lived core.ledger()
    # internally, and core.ledger()'s local FileLock is not reentrant - nesting
    # a second ledger() call inside an already-open one deadlocks. Only the
    # write below needs the lock held.
    business = position_workflow.positions()
    payouts = core.read_master(core.PAYOUTS_DB_PATH)
    with core.ledger() as read_db:
        plan = plan_round(now=now, base_cut=base_cut, open_round_ids=open_round_ids,
                           business=business, payouts=payouts, db=read_db)
    accepted_keys = {item['position_key'] for item in plan['included']}
    if not accepted_keys:
        # An empty business frame (e.g. no eligible position at all) carries
        # no columns to filter by - and there is nothing to assign anyway.
        positions = business.iloc[0:0]
    else:
        positions = business[business['position_key'].isin(accepted_keys)].copy()
    if len(positions) != len(accepted_keys):
        raise ValueError('Positionsbestand hat sich seit der Planung verändert; Anlage abgebrochen.')
    snapshot = {
        'source': 'neutral_weekly_round',
        'window_start': plan['start'].isoformat(),
        'window_end': plan['end'].isoformat(),
        'payouts': sorted({item['Payout'] for item in plan['included']}),
        'positions': sorted(position_workflow.source_snapshot(row) for _, row in positions.iterrows()),
    }

    with core.ledger() as db:
        group_b_rounds.initialize(db)
        db.execute('BEGIN IMMEDIATE')
        # Re-check both possibilities that could have changed since the reads
        # above (TOCTOU): the round itself may have been created concurrently,
        # or one of the accepted positions may already have been assigned to
        # any round (historical or a concurrently created neutral one).
        if db.execute('SELECT 1 FROM group_b_rounds WHERE id=?', (plan['round_id'],)).fetchone():
            return plan['round_id'], False, None
        already_assigned = accepted_keys & set(_historical_round_positions(db))
        if already_assigned:
            raise ValueError('Positionen wurden seit der Planung bereits einer Runde zugeordnet; '
                              f'Anlage abgebrochen: {sorted(already_assigned)[:5]}')
        created = group_b_rounds._insert_round(
            db, plan['round_id'], plan['sequence'], 'neutral_weekly', None, None,
            plan['total_claim'], snapshot, positions, {})
        db.commit()
    return plan['round_id'], created, plan
