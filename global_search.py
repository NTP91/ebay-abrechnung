"""Global read-only search across current (2026-003+) and historical
(GB-2026-001/002) cases - Bestellnummer, SKU, Partnerrechnungsnummer,
Evelyn-/Lexware-Belegnummer, Payoutnummer, optionally Round-ID.

Pure navigation/research: every value shown here already comes from
round_status.py / round_ui.py's own existing case-building functions - no
new status derivation, and this module never writes anything. It groups
matches into the same "Partner + Runde" units the Partnerkarte/Archiv
already use, so a search result is never a synthetic case that doesn't
exist anywhere else in the app.
"""
from decimal import Decimal

import api_holds
import core
import group_b_rounds
import partner_invoices
import partner_round_invoices
import position_workflow
import round_planner
import round_status
import round_ui
import studio_view

MIN_PARTIAL_LENGTH = 3


def _norm(value):
    return str(value or '').strip().casefold()


def _matches_exact(series, needle):
    return series.astype(str).str.strip().str.casefold() == needle


def _matches_contains(series, needle):
    return series.astype(str).str.casefold().str.contains(needle, regex=False, na=False)


def _position_round_id(db, position_key):
    row = db.execute('SELECT round_id FROM group_b_round_positions WHERE position_key=?', (position_key,)).fetchone()
    if row:
        return row[0]
    row = db.execute('SELECT origin_round_id FROM group_b_round_refunds WHERE refund_key=?', (position_key,)).fetchone()
    return row[0] if row else None


def _round_source_kind(db, round_id):
    row = db.execute('SELECT source_kind FROM group_b_rounds WHERE id=?', (round_id,)).fetchone()
    return row[0] if row else None


def _neutral_case(round_id, partner, business, current_round_id, db):
    status = round_status.partner_status(round_id, partner, business=business, db_context=db)
    return dict(
        kind='aktuelle_runde' if round_id == current_round_id else 'aeltere_runde',
        round_id=round_id, round_label=round_id, partner=partner, gruppe=status['group'],
        positions=status['positions'],
        amount=status['claim'],
        invoice_icon=round_ui._invoice_icon(status), invoice_text=round_ui.invoice_label(status['invoice_status']),
        invoice_number=status['invoice_number'],
        payment_icon=round_ui._payment_icon(status), payment_text=round_ui.payment_label(status['payment_status'], status['paid_at']),
        credit_icon=round_ui._credit_icon(status), credit_text=round_ui.credit_label(status['credit_status']),
        overall_icon=round_ui._status_icon(status), overall_text=round_ui.overall_label(status['overall_status']),
        open_points=list(status['blockers']),
        nav=dict(target='partnerkarte', gruppe=status['group'], partner=partner, round_id=round_id),
    )


def _historical_case_dict(round_id, partner, historical_round_ids_asc, business, invoices, db):
    """Reuses round_ui's own combined-open-case logic first (never a
    re-split per-round amount); falls back to that single round's own
    closed slice if the partner has nothing open anywhere."""
    case = round_ui._historical_partner_case(business, historical_round_ids_asc, partner, invoices=invoices, db=db)
    if not case:
        per_round = round_ui._historical_round_partner_cases(business, round_id, invoices=invoices, db=db)
        case = next((c for c in per_round if c['partner'] == partner), None)
    if not case:
        return None
    case['partner'] = partner
    return case


def _render_historical_summary(case, business, invoices):
    round_ui._historical_case_amount(business, case)
    label = round_ui._historical_case_label(case)
    amount = case['amount']
    matching_invoice = round_ui._historical_matching_invoice(case, invoices)
    refund_cases = studio_view.partner_refund_cases(business)
    has_open_refund = (bool(set(case['combined_keys']) & set(refund_cases.position_key))
                        if not refund_cases.empty else False)
    open_points = []
    if not case['invoiced']:
        open_points.append('Rechnung fehlt')
    if not case['paid_ok']:
        open_points.append('Zahlung offen')
    if has_open_refund:
        open_points.append('Gutschrift offen')
    overall_ok = case['paid_ok'] and case['invoiced'] and not has_open_refund
    rows = business[business.position_key.isin(case['combined_keys'])]
    gruppe = rows.iloc[0].Gruppe if not rows.empty else None
    return dict(
        kind='historisch', round_id=label, round_label=label, partner=case['partner'], gruppe=gruppe,
        positions=case['positions'], amount=amount,
        invoice_icon='✅' if case['invoiced'] else '❌',
        invoice_text='Rechnung geprüft' if case['invoiced'] else 'Rechnung fehlt',
        invoice_number=matching_invoice['invoice_number'] if matching_invoice else None,
        payment_icon='✅' if case['paid_ok'] else '❌',
        payment_text='bezahlt' if case['paid_ok'] else 'Zahlung offen',
        credit_icon='❌' if has_open_refund else '➖',
        credit_text='Gutschrift offen' if has_open_refund else 'nicht erforderlich',
        overall_icon='✅' if overall_ok else '❌',
        overall_text='abgeschlossen' if overall_ok else 'in Abwicklung',
        open_points=open_points,
        nav=dict(target='archiv', round_id=case['round_ids'][-1], partner=case['partner']),
    )


def _evelyn_case_summary(record, round_id, round_row):
    status_text = ('bezahlt' if record['Zahlungsstatus'].endswith('erhalten') else record['Zahlungsstatus'])
    return dict(
        kind='evelyn_beleg', round_id=round_id, round_label=round_id or '–', partner=None,
        gruppe='Gruppe B', positions=record['Positionen'], amount=Decimal(str(record['Betrag'])) if record['Betrag'] is not None else None,
        invoice_icon='✅', invoice_text='fakturiert',
        invoice_number=record['Belegnummer'],
        payment_icon='✅' if record['Zahlungsstatus'].endswith('erhalten') else '❌',
        payment_text=status_text,
        credit_icon='➖', credit_text='nicht erforderlich',
        overall_icon='✅' if record['Abschlussstatus'] == 'abgeschlossen' else '❌',
        overall_text=record['Abschlussstatus'],
        open_points=[] if record['Abschlussstatus'] == 'abgeschlossen' else ['Abschluss offen'],
        nav=dict(target='archiv', round_id=round_id, partner=None) if round_id else None,
    )


def _discarded_case_summary(record):
    return dict(
        kind='verworfen', round_id=None, round_label=record['Belegnummer'], partner=None, gruppe='Gruppe B',
        positions=record['Positionen'], amount=Decimal(str(record['Betrag'])) if record['Betrag'] is not None else None,
        invoice_icon='➖', invoice_text='verworfen · keine operative Wirkung',
        invoice_number=record['Belegnummer'],
        payment_icon='➖', payment_text='nicht zutreffend',
        credit_icon='➖', credit_text='nicht zutreffend',
        overall_icon='➖', overall_text='verworfen',
        open_points=[], nav=None,
    )


def search(query, business=None, payouts=None, orders=None):
    """Read-only. Returns a list of case-summary dicts (schema: kind,
    round_id, round_label, partner, gruppe, positions, amount, invoice_*,
    payment_*, credit_*, overall_*, open_points, nav, match_types,
    matched_values) - never a raw row dump, always grouped by the same
    Partner+Runde unit the rest of the app already uses. Empty list for an
    empty/too-short query or no hits; never raises for "nothing found"."""
    query = (query or '').strip()
    if not query:
        return []
    needle = _norm(query)
    business = position_workflow.positions() if business is None else business
    payouts = core.read_master(core.PAYOUTS_DB_PATH) if payouts is None else payouts
    orders = core.read_master(core.ORDERS_DB_PATH) if orders is None else orders

    # Both open their own core.ledger() - must happen before ours opens.
    invoices = partner_invoices.list_invoices()
    evelyn_invoices = studio_view.invoice_history()

    results = {}  # key -> summary dict; key is unique per case/beleg
    match_types = {}  # key -> set of match-type labels

    def record(key, summary, match_type, matched_value):
        if key not in results:
            results[key] = summary
            match_types[key] = {}
        match_types[key].setdefault(match_type, matched_value)

    if not business.empty:
        order_exact = business[_matches_exact(business.Bestellnummer, needle)]
        sku_exact = business[_matches_exact(business.SKU, needle)]
        sku_partial = (business[_matches_contains(business.SKU, needle) & ~_matches_exact(business.SKU, needle)]
                       if len(needle) >= MIN_PARTIAL_LENGTH else business.iloc[0:0])
        payout_exact = business[_matches_exact(business['Auszahlung Nr.'], needle)]

        row_hits = [(order_exact, 'Bestellnummer', True), (sku_exact, 'SKU', True),
                    (sku_partial, 'SKU', False), (payout_exact, 'Payoutnummer', True)]

        with core.ledger() as db:
            group_b_rounds.initialize(db)
            historical_round_ids_asc = [r[0] for r in db.execute(
                "SELECT id FROM group_b_rounds WHERE source_kind != 'neutral_weekly' ORDER BY sequence ASC")]
            neutral_round_ids = [r[0] for r in db.execute(
                "SELECT id FROM group_b_rounds WHERE source_kind = 'neutral_weekly' ORDER BY sequence DESC")]
            current_round_id = neutral_round_ids[0] if neutral_round_ids else None

            # Round-ID match (optional, exact only): every confirmed partner
            # with any position in that round.
            round_id_hit = next((rid for rid in historical_round_ids_asc + neutral_round_ids
                                 if _norm(rid) == needle), None)
            round_hits = []
            if round_id_hit:
                assigned_keys = {r[0] for r in db.execute(
                    'SELECT position_key FROM group_b_round_positions WHERE round_id=?', (round_id_hit,))}
                partners_in_round = sorted(business.loc[business.position_key.isin(assigned_keys), 'Partner'].unique())
                for partner in partners_in_round:
                    round_hits.append((round_id_hit, partner))

            case_neutral = {}  # (round_id, partner) -> summary
            case_historical = {}  # partner -> case dict (pre-amount)

            def resolve_row_group(rows):
                """One representative row per (round_id, partner) group -
                enough to report which exact value in that row matched."""
                groups = {}
                for _, row in rows.iterrows():
                    round_id = _position_round_id(db, row.position_key)
                    if round_id is None:
                        continue
                    groups.setdefault((round_id, row.Partner), row)
                return groups

            for rows, match_type, exact in row_hits:
                if rows.empty:
                    continue
                for (round_id, partner), row in resolve_row_group(rows).items():
                    kind = _round_source_kind(db, round_id)
                    key = ('neutral', round_id, partner) if kind == 'neutral_weekly' else ('historical', partner)
                    if kind == 'neutral_weekly':
                        if key not in case_neutral:
                            case_neutral[key] = _neutral_case(round_id, partner, business, current_round_id, db)
                        summary = case_neutral[key]
                    else:
                        if partner not in case_historical:
                            case_historical[partner] = _historical_case_dict(
                                round_id, partner, historical_round_ids_asc, business, invoices, db)
                        if case_historical[partner] is None:
                            continue
                        summary = case_historical[partner]  # placeholder, replaced below after db closes
                    matched_value = (row.Bestellnummer if match_type == 'Bestellnummer'
                                     else row.SKU if match_type == 'SKU' else row['Auszahlung Nr.'])
                    record(key, summary, match_type, matched_value)

            # Explicit round-ID search shows exactly that round's own
            # partner slices (never a redirect to a different round the
            # partner also happens to be open in - that would make
            # searching "GB-2026-001" silently surface a GB-2026-002 case).
            round_scoped_cases = None
            for round_id, partner in round_hits:
                kind = _round_source_kind(db, round_id)
                key = ('neutral', round_id, partner) if kind == 'neutral_weekly' else ('historical', partner)
                if kind == 'neutral_weekly':
                    if key not in case_neutral:
                        case_neutral[key] = _neutral_case(round_id, partner, business, current_round_id, db)
                    record(key, case_neutral[key], 'Round-ID', round_id_hit)
                else:
                    if round_scoped_cases is None:
                        round_scoped_cases = {c['partner']: c for c in round_ui._historical_round_partner_cases(
                            business, round_id, invoices=invoices, db=db)}
                    case = round_scoped_cases.get(partner)
                    if case is not None:
                        case_historical[partner] = case  # rendered below alongside every other historical hit
                        record(key, case, 'Round-ID', round_id_hit)

            # Partnerrechnungsnummer (2026-003+): exact match only.
            invoice_rows = db.execute(
                'SELECT round_id, partner, invoice_number FROM partner_round_invoices').fetchall()
        # db closed here - safe to call self-opening helpers below.

        for round_id, partner, invoice_number in invoice_rows:
            if invoice_number and _norm(invoice_number) == needle:
                key = ('neutral', round_id, partner)
                if key not in results:
                    with core.ledger() as db2:
                        summary = _neutral_case(round_id, partner, business, current_round_id, db2)
                    record(key, summary, 'Partnerrechnung', invoice_number)
                else:
                    record(key, results[key], 'Partnerrechnung', invoice_number)

        for case in case_historical.values():
            if case is None:
                continue
            key = ('historical', case['partner'])
            if key in results:
                results[key] = _render_historical_summary(case, business, invoices)

    # Historical partner invoice numbers (e.g. NB0576) - exact match. Shows
    # exactly the round that invoice actually belongs to (its own per-round
    # slice), never a different, still-open round the same partner happens
    # to also have - searching a specific invoice must show that invoice's
    # own case, not an unrelated redirect.
    for inv_record in invoices:
        number = inv_record.get('invoice_number')
        if number and _norm(number) == needle and inv_record.get('approved_at'):
            partner = inv_record['partner']
            key = ('historical', partner)
            if key not in results:
                with core.ledger() as db:
                    group_b_rounds.initialize(db)
                    historical_round_ids_asc = [r[0] for r in db.execute(
                        "SELECT id FROM group_b_rounds WHERE source_kind != 'neutral_weekly' ORDER BY sequence ASC")]
                    partner_keys = {item['key'] for item in inv_record['expected']['items']}
                    round_id = None
                    for rid in historical_round_ids_asc:
                        assigned = {r[0] for r in db.execute(
                            'SELECT position_key FROM group_b_round_positions WHERE round_id=?', (rid,))}
                        if partner_keys & assigned:
                            round_id = rid
                            break
                    if round_id is None:
                        continue
                    case = next((c for c in round_ui._historical_round_partner_cases(
                        business, round_id, invoices=invoices, db=db) if c['partner'] == partner), None)
                if case is None:
                    continue
                summary = _render_historical_summary(case, business, invoices)
                record(key, summary, 'Partnerrechnung', number)
            else:
                record(key, results[key], 'Partnerrechnung', number)

    # Evelyn-/Lexware-Belegnummer (RE0090 active, RE0089 discarded).
    for record_item in evelyn_invoices.values():
        belegnummer = record_item.get('Belegnummer', '')
        if record_item.get('discarded'):
            if len(needle) >= MIN_PARTIAL_LENGTH and needle in _norm(belegnummer):
                key = ('discarded', belegnummer)
                record(key, _discarded_case_summary(record_item), 'Evelyn-Beleg', belegnummer)
        elif _norm(belegnummer) == needle:
            with core.ledger() as db:
                group_b_rounds.initialize(db)
                row = db.execute(
                    "SELECT id FROM group_b_rounds WHERE evelyn_document_number=?", (belegnummer,)).fetchone()
            round_id = row[0] if row else None
            key = ('evelyn', belegnummer)
            record(key, _evelyn_case_summary(record_item, round_id, row), 'Evelyn-Beleg', belegnummer)

    for key, summary in results.items():
        summary['match_types'] = match_types.get(key, {})
    return list(results.values())
