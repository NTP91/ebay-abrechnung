"""Streamlit surface for the 2026-003+ round/partner workflow.

Pure presentation layer: every status, amount and eligibility check is read
from round_status.py / partner_snapshot.py / partner_round_invoices.py /
recovery_cases.py - no business rule is computed here. Historical
GB-2026-001/002 rounds are shown with their own existing information only
(group_b_rounds.overview()); this module never applies the 003+ status
rules to them (round_status.py itself refuses that).
"""
import json
from datetime import date, datetime

import streamlit as st

import core
import group_b_rounds
import partner_export
import partner_round_invoices
import partner_snapshot
import position_workflow
import recovery_cases
import round_planner
import round_status
import studio_view

INVOICE_LABELS = {
    'noch_nicht_moeglich': '⚠ Finale Einzelabrechnung fehlt',
    'fehlt': '⚠ Rechnung fehlt',
    'geprueft': '✅ Rechnung geprüft',
    'nicht_erforderlich': '✅ nicht erforderlich',
}
PAYMENT_LABELS = {'offen': '⏳ offen', 'nicht_erforderlich': '✅ nicht erforderlich'}
CREDIT_LABELS = {
    'nicht_erforderlich': '✅ nicht erforderlich',
    'fehlt': '⚠ Gutschrift fehlt',
    'erledigt': '✅ Gutschrift erledigt',
}
ROUND_LABELS = {'laufend': '🔵 laufend', 'in_Abwicklung': '🟠 in Abwicklung', 'abgeschlossen': '🟢 abgeschlossen'}
OVERALL_LABELS = {
    'laufend': 'laufend', 'in_Abwicklung': 'in Abwicklung',
    'abgeschlossen': 'abgeschlossen', 'nichts_erforderlich': 'nichts erforderlich',
}


def euros(value):
    return f'{value:,.2f} €'.replace(',', 'X').replace('.', ',').replace('X', '.')


def display_date(value):
    """dd.mm.yyyy for an ISO date (paid_at) or an ISO datetime (window bounds)."""
    if not value:
        return ''
    for fmt in ('%Y-%m-%d', '%Y-%m-%dT%H:%M:%S%z'):
        try:
            return datetime.strptime(value, fmt).strftime('%d.%m.%Y')
        except ValueError:
            continue
    return value[:10]


def invoice_label(status):
    return INVOICE_LABELS.get(status, status)


def payment_label(status, paid_at):
    if status == 'bezahlt' and paid_at:
        return f'✅ bezahlt am {display_date(paid_at)}'
    return PAYMENT_LABELS.get(status, status)


def credit_label(status):
    return CREDIT_LABELS.get(status, status)


def round_label(status):
    return ROUND_LABELS.get(status, status)


def overall_label(status):
    return OVERALL_LABELS.get(status, status)


def _neutral_round_ids():
    with core.ledger() as db:
        group_b_rounds.initialize(db)
        rows = db.execute("SELECT id FROM group_b_rounds WHERE source_kind='neutral_weekly' "
                           "ORDER BY sequence DESC").fetchall()
    return [row[0] for row in rows]


def _period(result):
    return f"{display_date(result['window_start'])}–{display_date(result['window_end'])}"


MATRIX_LEGEND = '✅ erledigt/vorhanden · ❌ erforderlich/offen · ➖ nicht erforderlich'


def _statement_icon(p):
    if p['positions'] == 0:
        return '➖'
    return '✅' if p['snapshot_status'] == 'vorhanden' else '❌'


def _invoice_icon(p):
    # 'noch_nicht_moeglich' (snapshot missing yet) is still a real, currently
    # unmet requirement - never displayed as done, and never as "not
    # required" either, so it maps to ❌ like 'fehlt', not to ➖.
    return {'noch_nicht_moeglich': '❌', 'fehlt': '❌', 'geprueft': '✅', 'nicht_erforderlich': '➖'}[p['invoice_status']]


def _payment_icon(p):
    return {'offen': '❌', 'bezahlt': '✅', 'nicht_erforderlich': '➖'}[p['payment_status']]


def _credit_icon(p):
    return {'fehlt': '❌', 'erledigt': '✅', 'nicht_erforderlich': '➖'}[p['credit_status']]


def _status_icon(p):
    if p['overall_status'] == 'nichts_erforderlich':
        return '➖'
    return '✅' if p['overall_status'] == 'abgeschlossen' else '❌'


def render_round_matrix(result):
    """Compact icon-only matrix (✅ ❌ ➖ - no ⏳, no orange dot) - long
    explanatory text belongs to the blocker list underneath, never to a
    matrix cell. Partner code '001' is just another column here, never
    confused with a round id. ➖ is reserved strictly for "not required in
    this round" (e.g. a 0-position partner); it is never used to paper over
    something that is actually still open."""
    import pandas as pd
    columns = {}
    for p in result['partners']:
        columns[p['partner']] = [_statement_icon(p), _invoice_icon(p), _payment_icon(p),
                                  _credit_icon(p), _status_icon(p)]
    frame = pd.DataFrame(columns, index=['Einzelabrechnung', 'Rechnung', 'Zahlung', 'Gutschrift', 'Status'])
    st.dataframe(frame, use_container_width=True)


def _historical_active_position_keys(round_id, db=None):
    """position_keys assigned to this historical round with an actual active
    claim - excludes role='hold_reserve' (group_b_rounds.py's own existing
    schema: positions reserved for a not-yet-invoiced API hold, never an
    active claim to begin with). A currently live API-Hold (api_holds.mask())
    is filtered separately by the caller since it needs the live business
    rows, not just this stored role column.

    db: an already-open core.ledger() connection to reuse (core.ledger()'s
    local-file FileLock is not reentrant) - opens its own otherwise."""
    def _query(connection):
        group_b_rounds.initialize(connection)
        return {r[0] for r in connection.execute(
            "SELECT position_key FROM group_b_round_positions WHERE round_id=? AND role != 'hold_reserve'",
            (round_id,))}
    if db is not None:
        return _query(db)
    with core.ledger() as own_db:
        return _query(own_db)


def _historical_matrix(business, round_id):
    """Same ✅/❌/➖ matrix as the 2026-003+ rounds, but for a historical
    GB-2026-001/002 round - built exclusively from already-existing facts:
    group_b_round_positions' own assignment, position_workflow's own
    paid_at/closed_at/paid_without_invoice_at markers, existing approved
    partner_invoices records, and studio_view.partner_refund_cases()
    (unchanged). Nothing is recomputed and no new business logic is
    introduced. Columns are only the partners actually present in this
    round. 'Einzelabrechnung' has no equivalent artifact in the old model
    (no frozen snapshot ever existed there) and is always ➖.

    Returns (frame_or_None, blockers, header_icon_or_'').
    """
    import api_holds
    import partner_invoices

    keys = _historical_active_position_keys(round_id)
    if not keys or business.empty:
        return None, [], ''
    rows = business[business.position_key.isin(keys) & (business.Art == 'Bestellung')]
    if not rows.empty:
        # A currently-held position is its own separate, still-unresolved
        # category everywhere else in the app (never counted as an open
        # claim needing Rechnung/Zahlung here either) - group_b_rounds.
        # overview() draws the exact same line via this same mask.
        rows = rows[~api_holds.mask(rows)]
    if rows.empty:
        return None, [], ''
    invoices = partner_invoices.list_invoices()
    refund_cases = studio_view.partner_refund_cases(business)
    refund_origin_keys = set(refund_cases.position_key) if not refund_cases.empty else set()

    columns, blockers = {}, []
    for partner, block in rows.groupby('Partner'):
        partner_keys = set(block.position_key)
        paid_ok = bool((block.paid_at.astype(bool) | block.closed_at.astype(bool)
                        | block[position_workflow.PAID_WITHOUT_INVOICE].astype(bool)).all())
        invoiced = any(
            record['partner'] == partner and record['approved_at']
            and partner_keys.intersection(item['key'] for item in record['expected']['items'])
            for record in invoices)
        has_open_refund = bool(partner_keys & refund_origin_keys)
        rechnung = '✅' if invoiced else '❌'
        zahlung = '✅' if paid_ok else '❌'
        gutschrift = '❌' if has_open_refund else '➖'
        status = '✅' if (invoiced and paid_ok and not has_open_refund) else '❌'
        columns[partner] = ['➖', rechnung, zahlung, gutschrift, status]
        if rechnung == '❌':
            blockers.append(f'{partner} · Rechnung fehlt')
        if zahlung == '❌':
            blockers.append(f'{partner} · Zahlung offen')
        if gutschrift == '❌':
            blockers.append(f'{partner} · Gutschrift offen')

    import pandas as pd
    frame = pd.DataFrame(columns, index=['Einzelabrechnung', 'Rechnung', 'Zahlung', 'Gutschrift', 'Status'])
    header_icon = '✅' if not (frame.loc['Status'] == '❌').any() else '❌'
    return frame, blockers, header_icon


def render_historical_rounds(business=None):
    """Historische GB-2026-001/002: same matrix style as 2026-003+, built
    only from already-existing historical facts - no new archive logic, no
    recomputation, ids never renamed, old business logic untouched."""
    import partner_invoices

    with core.ledger() as db:
        group_b_rounds.initialize(db)
        rows = db.execute("SELECT id, evelyn_amount, created_at FROM group_b_rounds "
                           "WHERE source_kind != 'neutral_weekly' ORDER BY sequence").fetchall()
    if not rows:
        return
    st.markdown('**Historische Runden (altes Modell)**', help=MATRIX_LEGEND)
    st.caption('GB-2026-001/002 laufen weiterhin nach der alten Geschäftslogik - reine Anzeige vorhandener Daten, '
                'keine neue 003+-Statuslogik und keine Neuberechnung.')
    business = position_workflow.positions() if business is None else business
    for row in rows:
        round_id = row['id']
        frame, blockers, header_icon = _historical_matrix(business, round_id)
        prefix = f'{header_icon} ' if header_icon else ''
        header = f"{prefix}{round_id} · Evelyn-Betrag {euros(float(row['evelyn_amount']))} · angelegt {display_date(row['created_at'])}"
        with st.expander(header, expanded=False):
            if frame is not None:
                st.dataframe(frame, use_container_width=True)
                if blockers:
                    st.caption('Offene Punkte: ' + ' · '.join(blockers))
            else:
                st.caption('Keine zuordenbaren historischen Partnerpositionen.')
            with core.ledger() as db:
                invoice_ids = {r[0] for r in db.execute(
                    'SELECT invoice_id FROM partner_invoice_rounds WHERE round_id=?', (round_id,))}
            documents = [record for record in partner_invoices.list_invoices() if record['id'] in invoice_ids]
            if documents:
                st.caption(f'Verknüpfte Partnerbelege · {len(documents)}')
                for record in documents:
                    freigabe = 'freigegeben' if record['approved_at'] else 'noch nicht freigegeben'
                    st.write(f"{record['partner']} · {record['invoice_number'] or record['file_name']} · {freigabe}")
            else:
                st.caption('Keine verknüpften Partnerbelege gespeichert.')


def render_overview_section(business=None):
    """The single 'Abrechnungsrunden' block for the Übersicht tab: current/
    in-Abwicklung 2026-003+ rounds prominent, abgeschlossene rounds
    collapsed, historical GB-2026-001/002 shown separately below with their
    own existing data only. No partner-card navigation here (next UI step)."""
    business = position_workflow.positions() if business is None else business
    st.subheader('Abrechnungsrunden', help=MATRIX_LEGEND)
    round_ids = _neutral_round_ids()
    if not round_ids:
        st.caption('Noch keine neutrale Wochenrunde vorhanden.')
    else:
        for round_id in round_ids:
            result = round_status.round_status(round_id, business=business)
            # One-time orientation note: 2026-003 is technically unchanged,
            # only labeled here as the first round shared by Gruppe A and B -
            # GB-2026-001/002 were the old Gruppe-B-only model.
            marker = ' · erste gemeinsame Runde' if round_id == '2026-003' else ''
            header = f"{round_id}{marker} · {_period(result)} · {round_label(result['round_status'])}"
            with st.expander(header, expanded=(result['round_status'] != 'abgeschlossen')):
                render_round_matrix(result)
                if result['blockers']:
                    st.caption('Offene Punkte: ' + ' · '.join(result['blockers']))
    render_historical_rounds(business)


def _live_claim(round_id, partner, business, payouts, orders):
    """Live, not-yet-frozen claim estimate for a still-open round - reuses
    the exact same row selection partner_snapshot.interim_export() already
    uses and the unchanged partner_export calculation. Finalization freezes
    this number into partner_snapshot's own final_amount; it never makes the
    claim computable in the first place - it was already computable here."""
    with core.ledger() as db:
        assigned_keys = {r[0] for r in db.execute(
            'SELECT position_key FROM group_b_round_positions WHERE round_id=?', (round_id,))}
    rows = partner_snapshot._partner_round_rows(business, assigned_keys, partner)
    if rows.empty:
        return None, 0
    try:
        model = partner_export.prepare_partner_export(rows, payouts, orders, statement_type='partner')
    except ValueError:
        return None, int((rows.Art == 'Bestellung').sum())
    return model['totals']['Rechnung']['gross'] + model['totals']['Gutschriften']['gross'], int((rows.Art == 'Bestellung').sum())


def render_statement_panel(round_id, partner, status, business=None, payouts=None, orders=None):
    st.markdown('**Einzelabrechnung**')
    claim, live_positions = status['claim'], None
    if claim is None and status['positions'] and business is not None and payouts is not None and orders is not None:
        claim, live_positions = _live_claim(round_id, partner, business, payouts, orders)
    positions = live_positions if live_positions is not None else status['positions']
    st.write(f"Positionen: {positions}")
    st.write('Partneranspruch: ' + (euros(float(claim)) if claim is not None else 'noch nicht berechenbar'))
    if status['positions'] == 0:
        st.caption('0 Positionen · nichts erforderlich')
        return
    if status['snapshot_status'] == 'vorhanden':
        content = partner_snapshot.final_file(round_id, partner)
        st.success('Finale Einzelabrechnung liegt vor.')
        if content:
            st.download_button('Finale Einzelabrechnung erneut herunterladen', content,
                                f'{round_id}_{partner}_final.xlsx', key=f'final-dl-{round_id}-{partner}',
                                icon=':material/download:', use_container_width=True)
        return
    if status['invoice_status'] == 'noch_nicht_moeglich' and status['overall_status'] != 'laufend':
        # cut has passed, nobody has finalized yet
        if st.button('Finale Einzelabrechnung erstellen', key=f'finalize-{round_id}-{partner}',
                     type='primary', use_container_width=True):
            try:
                partner_snapshot.finalize(round_id, partner)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        return
    content = partner_snapshot.interim_export(round_id, partner)
    if content:
        st.download_button('Zwischenstand herunterladen', content, f'{round_id}_{partner}_zwischenstand.xlsx',
                            key=f'interim-dl-{round_id}-{partner}', icon=':material/download:',
                            use_container_width=True)
    st.caption('Laufende Runde · kein finaler Snapshot, keine Sperre.')


def render_invoice_and_payment_panel(round_id, partner, status):
    st.markdown('**Partnerrechnung**')
    if status['invoice_status'] == 'noch_nicht_moeglich':
        st.info('Partnerrechnung noch nicht prüfbar - zuerst die finale Einzelabrechnung erstellen.')
        return
    if status['invoice_status'] == 'nicht_erforderlich':
        st.caption('0 Positionen · keine Rechnung erforderlich.')
        return
    if status['invoice_status'] in ('fehlt',):
        with st.expander('Partnerrechnung hochladen & prüfen', expanded=True):
            uploaded = st.file_uploader('Eingehende Partnerrechnung', type=['pdf', 'xlsx', 'csv'],
                                         key=f'invoice-file-{round_id}-{partner}')
            if st.button('Rechnung hochladen und prüfen', disabled=uploaded is None,
                         key=f'invoice-upload-{round_id}-{partner}'):
                try:
                    record, report = partner_round_invoices.check_and_review(
                        round_id, partner, uploaded.name, uploaded.getvalue())
                    if report['status'] == 'matched':
                        st.rerun()
                    else:
                        for message in report['errors']:
                            st.error(message)
                        for message in report.get('warnings', []):
                            st.warning(message)
                except ValueError as exc:
                    st.error(str(exc))
        return

    st.success('✅ Rechnung geprüft' + (f" · Rechnungsnr. {status['invoice_number']}" if status['invoice_number'] else ''))
    with st.expander('Details', expanded=False):
        with core.ledger() as db:
            partner_round_invoices.initialize(db)
            invoice = db.execute('SELECT * FROM partner_round_invoices WHERE round_id=? AND partner=?',
                                  (round_id, partner)).fetchone()
        if invoice:
            st.caption(f"Hochgeladen: {display_date(invoice['uploaded_at'])} · Betrag {euros(float(invoice['amount']))} "
                       f"· Snapshot-Hash {invoice['snapshot_hash'][:12]}…")

    st.markdown('**Zahlung**')
    if status['payment_status'] == 'bezahlt':
        st.success(payment_label('bezahlt', status['paid_at']))
        return
    if status['payment_status'] == 'nicht_erforderlich':
        st.caption('0 Positionen · keine Zahlung erforderlich.')
        return
    with st.form(key=f'pay-form-{round_id}-{partner}'):
        paid_date = st.date_input('Zahlungsdatum', value=date.today(), key=f'pay-date-{round_id}-{partner}')
        note = st.text_input('Notiz (optional)', key=f'pay-note-{round_id}-{partner}')
        submitted = st.form_submit_button('Zahlung überwiesen', type='primary', use_container_width=True)
    if submitted:
        try:
            partner_round_invoices.confirm_payment(round_id, partner, paid_date=paid_date.isoformat(), note=note)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))


def render_recovery_panel(round_id, partner):
    cases = recovery_cases.list_cases(round_id=round_id, partner=partner)
    if not cases:
        return
    open_cases = [c for c in cases if c['status'] == 'offen']
    st.markdown(f"**Gutschrift / Rückforderung** · {len(cases)} Fälle · "
                + (f'⚠ {len(open_cases)} offen' if open_cases else '✅ erledigt'))
    for case in cases:
        with st.expander(f"{case['order_number']} · {euros(abs(float(case['refund_amount'])))} · "
                          f"{'⚠ offen' if case['status'] == 'offen' else '✅ erledigt'}", expanded=case['status'] == 'offen'):
            st.caption(f"Ursprungsrunde {case['origin_round_id']} · erkannt {display_date(case['detected_at'])}")
            if case['status'] == 'erledigt':
                st.caption(f"Erledigt am {display_date(case['resolved_at'])}")
                continue
            uploaded = st.file_uploader('Gutschrift / Beleg', type=['pdf', 'xlsx', 'csv'],
                                         key=f'credit-file-{case["id"]}')
            if st.button('Gutschrift hochladen und prüfen', disabled=uploaded is None, key=f'credit-upload-{case["id"]}'):
                try:
                    record, report = recovery_cases.resolve(case['id'], uploaded.name, uploaded.getvalue())
                    if report['status'] == 'matched':
                        st.rerun()
                    else:
                        for message in report['errors']:
                            st.error(message)
                except ValueError as exc:
                    st.error(str(exc))


def _historical_partner_case(business, round_id, partner, invoices=None, db=None):
    """One historical open/documented case for `partner` in a historical
    GB-2026-xxx round - built only from the same existing facts as
    _historical_matrix(), reused per-partner. None if this partner has no
    active (non-held, non-hold_reserve) position there, or if everything is
    already fully settled (invoiced and paid) and therefore not worth a
    separate callout. Does not compute a displayed amount itself (that needs
    studio_view.partner_summary(), which internally opens its own
    core.ledger() and would deadlock while `db` is still open here) - the
    caller fills 'amount' via _historical_case_amount() once this ledger
    block has closed."""
    import api_holds
    import partner_invoices

    keys = _historical_active_position_keys(round_id, db=db)
    if not keys or business.empty:
        return None
    rows = business[business.position_key.isin(keys) & (business.Art == 'Bestellung') & (business.Partner == partner)]
    if not rows.empty:
        rows = rows[~api_holds.mask(rows)]
    if rows.empty:
        return None
    partner_keys = set(rows.position_key)
    paid_ok = bool((rows.paid_at.astype(bool) | rows.closed_at.astype(bool)
                    | rows[position_workflow.PAID_WITHOUT_INVOICE].astype(bool)).all())
    invoices = invoices if invoices is not None else partner_invoices.list_invoices()
    invoiced = any(
        record['partner'] == partner and record['approved_at']
        and partner_keys.intersection(item['key'] for item in record['expected']['items'])
        for record in invoices)
    if paid_ok and invoiced:
        return None  # fully settled historically - belongs in Rechnungshistorie, not a callout here
    links = core.refund_links(business)
    refund_idx = [r for r, s in links.items() if s in set(rows.index)]
    combined_keys = list(rows.position_key) + [business.loc[i].position_key for i in refund_idx]
    return dict(round_id=round_id, positions=len(rows), amount=None, paid_ok=paid_ok, invoiced=invoiced,
                combined_keys=combined_keys)


def _historical_case_amount(business, case):
    """Fills in 'amount' for a case _historical_partner_case() returned -
    reuses studio_view.partner_summary() (unchanged) on exactly the same row
    set, called outside any open core.ledger() to avoid the nested-lock
    deadlock that function's own invoice lookups would otherwise hit."""
    rows = business[business.position_key.isin(case['combined_keys'])]
    try:
        summary = studio_view.partner_summary(rows)
        if not summary.empty:
            case['amount'] = summary.iloc[0]['Verbleibender Anspruch']
    except ValueError:
        pass
    return case


def _older_open_round_case(business, round_id, partner, db):
    """An older (not-current) 2026-003+ round where this partner still has
    an unresolved overall_status - round_status.py's own existing
    computation, just queried for a round other than the current one."""
    result = round_status.partner_status(round_id, partner, business=business, db_context=db)
    if result['overall_status'] in ('abgeschlossen', 'nichts_erforderlich'):
        return None
    return result


def _sort_rank(partner, current_status, historical_cases):
    """Partners with an open task (current or historical) first; clean
    0-position/nothing-required partners last and collapsed by default."""
    blocked = bool(historical_cases) or (current_status and current_status['overall_status']
                                          not in ('abgeschlossen', 'nichts_erforderlich'))
    zero = bool(current_status) and current_status['positions'] == 0 and not historical_cases
    rank = 0 if blocked else (2 if zero else 1)
    return rank, partner


def render_partner_cards(business=None, payouts=None, orders=None, group=None):
    """Every confirmed partner (of `group`, or all if None) as a compact
    expander - no dropdown, one shared data load. Partners with an open
    current or historical task sort first; 0-position/nothing-required
    partners sort last and stay collapsed by default."""
    import partner_invoices

    business = position_workflow.positions() if business is None else business
    payouts = core.read_master(core.PAYOUTS_DB_PATH) if payouts is None else payouts
    orders = core.read_master(core.ORDERS_DB_PATH) if orders is None else orders
    invoices = partner_invoices.list_invoices()  # its own ledger() read - must happen before ours opens

    round_ids = _neutral_round_ids()
    current_round = round_ids[0] if round_ids else None
    older_rounds = round_ids[1:]
    with core.ledger() as db:
        group_b_rounds.initialize(db)
        historical_round_ids = [r[0] for r in db.execute(
            "SELECT id FROM group_b_rounds WHERE source_kind != 'neutral_weekly' ORDER BY sequence")]
        partner_snapshot.initialize(db)
        partner_round_invoices.initialize(db)
        recovery_cases.initialize(db)

        confirmed = [name for name, g in round_planner.confirmed_partners(business) if group is None or g == group]
        current_status = {}
        if current_round:
            for name in confirmed:
                current_status[name] = round_status.partner_status(current_round, name, business=business, db_context=db)

        historical_cases = {name: [] for name in confirmed}
        for round_id in historical_round_ids:
            for name in confirmed:
                case = _historical_partner_case(business, round_id, name, invoices=invoices, db=db)
                if case:
                    historical_cases[name].append(case)
        for round_id in older_rounds:
            for name in confirmed:
                case = _older_open_round_case(business, round_id, name, db)
                if case:
                    historical_cases[name].append(dict(round_id=round_id, older_neutral=case))

    for cases in historical_cases.values():
        for case in cases:
            if 'combined_keys' in case:
                _historical_case_amount(business, case)

    for partner in sorted(confirmed, key=lambda name: _sort_rank(name, current_status.get(name), historical_cases.get(name))):
        status = current_status.get(partner)
        cases = historical_cases.get(partner) or []
        if status:
            claim = status['claim']
            if claim is None and status['positions']:
                claim, _ = _live_claim(current_round, partner, business, payouts, orders)
            if status['positions'] == 0:
                icon_text = '➖ nichts erforderlich' if not cases else '❌ historischer Fall offen'
            elif status['blockers']:
                icon_text = f"❌ {status['blockers'][0]}"
            else:
                icon_text = '✅ abgeschlossen'
            claim_text = euros(float(claim)) if claim is not None else '–'
            header = f"{partner} · {current_round} · {status['positions']} Positionen · {claim_text} · {icon_text}"
        else:
            header = f"{partner} · {'kein aktuelle Runde' if not current_round else current_round} · ➖ nichts erforderlich"
        expanded = bool(cases) or (status and status['overall_status'] not in ('abgeschlossen', 'nichts_erforderlich'))
        with st.expander(header, expanded=bool(expanded)):
            for case in cases:
                if 'older_neutral' in case:
                    older = case['older_neutral']
                    older_claim = euros(float(older['claim'])) if older['claim'] is not None else '–'
                    st.warning(f"**Offene ältere Runde {case['round_id']}** · {older['positions']} Positionen · "
                               f"{older_claim} · {' · '.join(older['blockers']) or overall_label(older['overall_status'])}")
                else:
                    amount = euros(float(case['amount'])) if case['amount'] is not None else '–'
                    zahlung_text = '✅ bezahlt' if case['paid_ok'] else '❌ Zahlung offen'
                    rechnung_text = '✅ geprüft' if case['invoiced'] else '❌ Rechnung fehlt'
                    st.warning(f"**Historischer offener Beleg** · {case['round_id']} · {case['positions']} Positionen · "
                               f"{amount} · {zahlung_text} · {rechnung_text}")
            if status and current_round:
                st.markdown(f'**Aktuelle Runde {current_round}**')
                left, right = st.columns(2)
                with left:
                    render_invoice_and_payment_panel(current_round, partner, status)
                with right:
                    render_statement_panel(current_round, partner, status, business=business,
                                            payouts=payouts, orders=orders)
                render_recovery_panel(current_round, partner)
            elif not cases:
                st.caption('Keine aktuelle Runde vorhanden.')


def render_open_documents(business=None, group=None):
    """'Offene Belege': every partner/round still missing a reviewed
    invoice or an open recovery credit, across all neutral rounds - MH's
    paid-without-invoice historical case belongs to the old 001/002 model
    and is deliberately out of scope here (round_status.py refuses it)."""
    business = position_workflow.positions() if business is None else business
    allowed = None if group is None else {name for name, g in round_planner.confirmed_partners(business) if g == group}
    round_ids = _neutral_round_ids()
    missing_invoice, missing_credit = [], []
    for round_id in round_ids:
        result = round_status.round_status(round_id, business=business)
        for p in result['partners']:
            if allowed is not None and p['partner'] not in allowed:
                continue
            if p['invoice_status'] == 'fehlt':
                missing_invoice.append((round_id, p['partner']))
            if p['credit_status'] == 'fehlt':
                missing_credit.append((round_id, p['partner']))
    total_open = len(missing_invoice) + len(missing_credit)
    with st.expander(f'Offene Belege · {"❌ " + str(total_open) + " offen" if total_open else "✅ keine offen"}',
                      expanded=bool(total_open)):
        if not total_open:
            st.caption('Keine offenen Belege.')
        for round_id, partner in missing_invoice:
            st.write(f'❌ {partner} · {round_id} · Rechnung fehlt')
        for round_id, partner in missing_credit:
            st.write(f'❌ {partner} · {round_id} · Gutschrift fehlt')


def render_invoice_history(business=None, group=None):
    business = position_workflow.positions() if business is None else business
    allowed = None if group is None else {name for name, g in round_planner.confirmed_partners(business) if g == group}
    round_ids = _neutral_round_ids()
    rows = []
    with core.ledger() as db:
        partner_round_invoices.initialize(db)
        for round_id in round_ids:
            rows.extend(dict(r) for r in db.execute(
                'SELECT * FROM partner_round_invoices WHERE round_id=? ORDER BY uploaded_at DESC', (round_id,)))
    if allowed is not None:
        rows = [row for row in rows if row['partner'] in allowed]
    if not rows:
        return
    with st.expander(f'Rechnungshistorie · {len(rows)}', expanded=False):
        for row in rows:
            paid = f"✅ bezahlt am {display_date(row['paid_at'])}" if row['paid_at'] else '⏳ Zahlung offen'
            st.markdown(f"**{row['partner']} · {row['round_id']} · Rechnungsnr. {row['invoice_number'] or '–'} "
                        f"· {euros(float(row['amount']))} · {paid}**")
            with st.expander('Details', expanded=False):
                st.caption(f"Hochgeladen: {display_date(row['uploaded_at'])} · Geprüft: {display_date(row['reviewed_at'])}")
                st.caption(f"Snapshot-Hash: {row['snapshot_hash']}")
                content = partner_snapshot.final_file(row['round_id'], row['partner'])
                if content:
                    st.download_button('Finale Einzelabrechnung', content,
                                        f"{row['round_id']}_{row['partner']}_final.xlsx",
                                        key=f"hist-final-{row['round_id']}-{row['partner']}",
                                        icon=':material/download:')
                if row['file_bytes']:
                    st.download_button('Original-Partnerrechnung', row['file_bytes'], row['file_name'],
                                        key=f"hist-invoice-{row['round_id']}-{row['partner']}",
                                        icon=':material/download:')


def render(business=None, payouts=None, orders=None):
    """Full workspace (overview + partner cards + open documents + invoice
    history) - not currently wired into any app.py tab (the round overview
    lives inline in Übersicht via render_overview_section(); partner cards
    are wired into the Gruppe A/B tabs via render_partner_cards()). Kept
    intact and working for reuse."""
    st.header('Abrechnungsrunden')
    render_overview_section(business)
    st.divider()
    render_partner_cards(business, payouts, orders)
    st.divider()
    render_open_documents(business)
    render_invoice_history(business)
