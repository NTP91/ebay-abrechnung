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
from decimal import Decimal

import streamlit as st

import core
import group_b_rounds
import partner_conditions
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


def render_broker_line(result):
    """Eine kompakte Zeile 'Vermittlungsprovision Patrick → Evelyn' mit
    Gesamtbetrag und Status, Details (Partnerbasis, Satz, Provision) nur
    aufklappbar. Keine Debugdaten, keine Positionsschluessel."""
    import broker_commission
    broker = result.get('broker')
    if not broker:
        return
    amount = euros(float(broker['total_commission'])) if broker['status'] != 'nicht_erforderlich' else euros(0)
    st.caption(f"Vermittlungsprovision Patrick → Evelyn · {amount} · {broker_commission.label(broker)}")
    if not broker['breakdown']:
        return
    with st.expander('Vermittlungsprovision · Details', expanded=False):
        import pandas as pd
        st.dataframe(pd.DataFrame([{
            'Partner': item['partner'],
            'Positionen': item['positions'],
            'Provisionsrelevante Netto-Basis': euros(float(item['net_basis'])),
            'Satz': partner_conditions.percent(Decimal(item['rate'])),
            'Provision': euros(float(item['commission'])),
        } for item in broker['breakdown']]), use_container_width=True, hide_index=True)
        for flag in broker.get('late_refunds', []):
            st.warning(f"Erstattung nach finalisierter Vermittlungsabrechnung · {flag['partner']} · "
                       f"{euros(float(flag['betrag']))} · manuelle Klärung erforderlich; der "
                       f"finalisierte Beleg wurde bewusst nicht verändert.")


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
                render_broker_line(result)
                if result['blockers']:
                    st.caption('Offene Punkte: ' + ' · '.join(result['blockers']))
                st.caption('Partnerpakete & Dokumente: Historie → Abrechnungsarchiv.')
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
    if status['positions'] == 0:
        # 0 abrechenbare Positionen -> Anspruch ist tatsächlich 0,00 €, not
        # an unknown/uncomputable amount - and no download button, there is
        # nothing to export.
        st.write('Positionen: 0')
        st.write('Partneranspruch: ' + euros(0))
        st.caption('0 Positionen · nichts erforderlich')
        return
    claim, live_positions = status['claim'], None
    if claim is None and business is not None and payouts is not None and orders is not None:
        claim, live_positions = _live_claim(round_id, partner, business, payouts, orders)
    positions = live_positions if live_positions is not None else status['positions']
    st.write(f"Positionen: {positions}")
    st.write('Partneranspruch: ' + (euros(float(claim)) if claim is not None else 'noch nicht berechenbar'))
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


def _historical_partner_case(business, historical_round_ids, partner, invoices=None, db=None):
    """ONE combined historical case for `partner`, spanning every historical
    GB-2026-xxx round where they still have an active, not-fully-settled
    position - built only from the same existing facts as _historical_
    matrix() (group_b_round_positions role/hold filtering, position_
    workflow's own paid_at/closed_at/paid_without_invoice_at, existing
    approved partner_invoices records). Deliberately never computed as
    separate per-round slices that are then added together: that would
    rerun studio_view.partner_summary()'s own cent-rounding on two disjoint
    slices and can land a cent away from the one true combined figure - the
    historical payout itself was one combined transaction, so the case is
    too. None if nothing is open anywhere for this partner. Does not
    compute a displayed amount itself (needs studio_view.partner_summary(),
    which opens its own core.ledger() and would deadlock while `db` is
    still open here) - the caller fills 'amount' via _historical_case_
    amount() once this ledger block has closed."""
    import api_holds
    import partner_invoices

    invoices = invoices if invoices is not None else partner_invoices.list_invoices()
    round_ids_used, row_indices = [], []
    paid_ok, invoiced = True, True
    for round_id in historical_round_ids:
        keys = _historical_active_position_keys(round_id, db=db)
        if not keys or business.empty:
            continue
        rows = business[business.position_key.isin(keys) & (business.Art == 'Bestellung') & (business.Partner == partner)]
        if not rows.empty:
            rows = rows[~api_holds.mask(rows)]
        if rows.empty:
            continue
        partner_keys = set(rows.position_key)
        round_paid_ok = bool((rows.paid_at.astype(bool) | rows.closed_at.astype(bool)
                               | rows[position_workflow.PAID_WITHOUT_INVOICE].astype(bool)).all())
        round_invoiced = any(
            record['partner'] == partner and record['approved_at']
            and partner_keys.intersection(item['key'] for item in record['expected']['items'])
            for record in invoices)
        if round_paid_ok and round_invoiced:
            continue  # this round's slice is fully settled - Rechnungshistorie, not a callout
        round_ids_used.append(round_id)
        row_indices.extend(rows.index.tolist())
        paid_ok = paid_ok and round_paid_ok
        invoiced = invoiced and round_invoiced
    if not row_indices:
        return None
    links = core.refund_links(business)
    refund_idx = [r for r, s in links.items() if s in set(row_indices)]
    combined_keys = [business.loc[i].position_key for i in row_indices] + \
                     [business.loc[i].position_key for i in refund_idx]
    return dict(round_ids=round_ids_used, positions=len(row_indices), amount=None, paid_ok=paid_ok,
                invoiced=invoiced, combined_keys=combined_keys)


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


def _historical_case_file(business, payouts, orders, case):
    """The historical case's Einzelabrechnung, produced by the same
    unchanged partner_export.export_partner_excel() on the exact same
    (immutable) historical position set the case was built from - never a
    new/separate file per historical round, one combined file matching how
    the historical payout itself was made."""
    rows = business[business.position_key.isin(case['combined_keys'])]
    try:
        return partner_export.export_partner_excel(rows, payouts, orders, statement_type='partner')
    except ValueError:
        return None


def _historical_case_label(case):
    if len(case['round_ids']) > 1:
        return 'GB-2026-' + '/'.join(rid.replace('GB-2026-', '') for rid in case['round_ids'])
    return case['round_ids'][0]


def render_historical_case(business, payouts, orders, partner, case):
    """Offener älterer Fall: download of the existing combined historical
    Einzelabrechnung, invoice upload against it (reusing partner_invoices.
    upload()/approve() unchanged), and - only once invoiced and only while
    still unpaid - the existing position_workflow.confirm('partner_paid')
    payment action. A paid_without_invoice_at case (MH) never gets a second
    payment button once invoiced; an already-paid case never gets one at
    all."""
    import partner_invoices

    label = _historical_case_label(case)
    amount = euros(float(case['amount'])) if case['amount'] is not None else '–'
    zahlung_icon = '✅ bezahlt' if case['paid_ok'] else '❌ Zahlung offen'
    rechnung_icon = '✅ geprüft' if case['invoiced'] else '❌ Rechnung fehlt'
    st.warning(f"**{label}** · {case['positions']} Positionen · {amount} · {zahlung_icon} · {rechnung_icon}")
    content = _historical_case_file(business, payouts, orders, case)
    if content:
        st.download_button('Historische Einzelabrechnung herunterladen', content,
                            f"{label.replace('/', '_')}_{partner}_historisch.xlsx",
                            key=f'hist-case-dl-{partner}-{label}', icon=':material/download:')
    if not case['invoiced']:
        uploaded = st.file_uploader('Partnerrechnung (historischer Fall)', type=['pdf', 'xlsx', 'csv'],
                                     key=f'hist-case-file-{partner}-{label}')
        if st.button('Rechnung hochladen & prüfen', disabled=uploaded is None,
                     key=f'hist-case-upload-{partner}-{label}'):
            try:
                record, duplicate = partner_invoices.upload(partner, uploaded.name, uploaded.getvalue())
                if record['report']['status'] == 'matched':
                    partner_invoices.approve(record['id'], 'Patrick')
                    st.rerun()
                else:
                    for message in record['report']['errors']:
                        st.error(message)
                    for message in record['report'].get('warnings', []):
                        st.warning(message)
            except ValueError as exc:
                st.error(str(exc))
    elif not case['paid_ok']:
        if st.button('Zahlung überwiesen', key=f'hist-case-pay-{partner}-{label}', type='primary'):
            fresh = position_workflow.positions()
            payable = fresh[(fresh.Partner == partner) & (fresh.Art == 'Bestellung')
                             & fresh.reviewed_at.astype(bool) & ~fresh.paid_at.astype(bool)
                             & ~fresh.closed_at.astype(bool)]
            if payable.empty:
                st.warning('Keine geprüfte, noch nicht bezahlte Position gefunden.')
            else:
                try:
                    position_workflow.confirm(payable.position_key.tolist(), 'partner_paid', date.today())
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
    else:
        st.caption('Kein zweiter Zahlungsschritt erforderlich.')


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

        gb_case = {}
        for name in confirmed:
            case = _historical_partner_case(business, historical_round_ids, name, invoices=invoices, db=db)
            if case:
                gb_case[name] = case

        older_neutral_cases = {name: [] for name in confirmed}
        for round_id in older_rounds:
            for name in confirmed:
                case = _older_open_round_case(business, round_id, name, db)
                if case:
                    older_neutral_cases[name].append(dict(round_id=round_id, older_neutral=case))

    for case in gb_case.values():
        _historical_case_amount(business, case)

    older_cases = {name: ([gb_case[name]] if name in gb_case else []) + older_neutral_cases.get(name, [])
                   for name in confirmed}

    for partner in sorted(confirmed, key=lambda name: _sort_rank(name, current_status.get(name), older_cases.get(name))):
        status = current_status.get(partner)
        cases = older_cases.get(partner) or []
        if cases:
            # An open older case always dominates the closed-card header -
            # the current round's own state is summarised compactly next to
            # it, never re-stated as a second "2026-003" the way the old
            # layout duplicated it.
            current_bit = (f"aktuell {status['positions']} Positionen / "
                            f"{euros(float(status['claim'])) if status['claim'] is not None else euros(0)}"
                            if status else 'keine aktuelle Runde')
            header = f"{partner} · ❌ älterer Fall offen · {current_bit}"
        elif status:
            if status['positions'] == 0:
                icon_text = '➖ nichts erforderlich'
            elif status['blockers']:
                icon_text = f"❌ {status['blockers'][0]}"
            else:
                icon_text = '✅ abgeschlossen'
            claim = status['claim']
            if claim is None and status['positions']:
                claim, _ = _live_claim(current_round, partner, business, payouts, orders)
            claim_text = euros(float(claim)) if claim is not None else (euros(0) if not status['positions'] else '–')
            header = f"{partner} · {current_round} · {status['positions']} Positionen · {claim_text} · {icon_text}"
        else:
            header = f"{partner} · {'kein aktuelle Runde' if not current_round else current_round} · ➖ nichts erforderlich"
        expanded = bool(cases) or (status and status['overall_status'] not in ('abgeschlossen', 'nichts_erforderlich'))
        with st.expander(header, expanded=bool(expanded)):
            # PM bleibt ein ganz normaler Gruppe-B-Partner (kein eigener Tab,
            # kein eigener Workflow) - nur die Kondition wird sichtbar
            # ausgewiesen, aus der zentralen Konditionsquelle.
            st.caption(partner_conditions.label(partner, broker=True))
            st.caption('Dokumente & vollständige Fallhistorie: Historie → Abrechnungsarchiv.')
            if cases:
                st.markdown('**Offene ältere Fälle**')
                for case in cases:
                    if 'older_neutral' in case:
                        older = case['older_neutral']
                        older_claim = euros(float(older['claim'])) if older['claim'] is not None else '–'
                        st.warning(f"**{case['round_id']}** · {older['positions']} Positionen · {older_claim} · "
                                   f"{' · '.join(older['blockers']) or overall_label(older['overall_status'])}")
                    else:
                        render_historical_case(business, payouts, orders, partner, case)
                st.divider()
            if status and current_round:
                st.markdown(f'**Aktuelle Runde {current_round}**')
                if status['positions'] == 0:
                    # No empty two-column workspace for a partner with
                    # nothing to do in the current round - one compact line.
                    st.caption('0 Positionen · 0,00 € · ➖ nichts erforderlich')
                else:
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
    invoice or an open recovery credit - across all neutral rounds AND every
    historical GB-2026-xxx open case (MH's/NB's missing historical invoice
    included), so this list matches exactly what the partner cards already
    show as open. Resolved as soon as the underlying invoice/credit is
    actually reviewed - never re-opened for an already-paid position."""
    import partner_invoices

    business = position_workflow.positions() if business is None else business
    allowed = None if group is None else {name for name, g in round_planner.confirmed_partners(business) if g == group}
    confirmed_names = [name for name, g in round_planner.confirmed_partners(business) if allowed is None or name in allowed]
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

    invoices = partner_invoices.list_invoices()
    with core.ledger() as db:
        group_b_rounds.initialize(db)
        historical_round_ids = [r[0] for r in db.execute(
            "SELECT id FROM group_b_rounds WHERE source_kind != 'neutral_weekly' ORDER BY sequence")]
        for name in confirmed_names:
            case = _historical_partner_case(business, historical_round_ids, name, invoices=invoices, db=db)
            if case and not case['invoiced']:
                missing_invoice.append((_historical_case_label(case), name))

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
    """Only cases with an actually-present, valid partner invoice - a
    'bezahlt · Rechnung fehlt' case belongs in Offene Belege, not here, and
    only moves here once that invoice is really uploaded and reviewed."""
    import partner_invoices

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

    historical = [record for record in partner_invoices.list_invoices()
                  if record['approved_at'] and record['expected']['scope'] == 'Rechnung'
                  and (allowed is None or record['partner'] in allowed)]

    if not rows and not historical:
        return
    with st.expander(f'Rechnungshistorie · {len(rows) + len(historical)}', expanded=False):
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
        for record in historical:
            item_keys = {item['key'] for item in record['expected']['items']}
            paid_rows = business[business.position_key.isin(item_keys)]
            paid = ('✅ bezahlt' if not paid_rows.empty and paid_rows.paid_at.astype(bool).all()
                    else '❌ Zahlung offen')
            st.markdown(f"**{record['partner']} · historisch (Altmodell) · "
                        f"Rechnungsnr. {record['invoice_number'] or record['file_name']} "
                        f"· {euros(float(record['expected']['total']))} · {paid}**")
            with st.expander('Details', expanded=False):
                st.caption(f"Hochgeladen: {display_date(record['uploaded_at'][:10])} · freigegeben: "
                           f"{display_date(record['approved_at'][:10]) if record['approved_at'] else '–'}")
                stored = partner_invoices.stored_original(record)
                if stored is not None:
                    content = stored if isinstance(stored, bytes) else stored.read_bytes()
                    st.download_button('Original-Partnerrechnung', content, record['file_name'],
                                        key=f"hist-old-invoice-{record['id']}", icon=':material/download:')


def _historical_round_partner_cases(business, round_id, invoices=None, db=None):
    """Every partner's slice of ONE historical round, regardless of settled
    state - unlike _historical_partner_case() (which combines across rounds
    and deliberately skips an already fully-settled slice, built for the
    'still open' partner-card callout), the archive needs closed cases too,
    scoped to exactly the round being displayed. Same underlying facts as
    _historical_matrix() (group_b_round_positions role/hold filtering,
    position_workflow's own paid_at/closed_at/paid_without_invoice_at,
    existing approved partner_invoices records) - no new derivation."""
    import api_holds
    import partner_invoices

    keys = _historical_active_position_keys(round_id, db=db)
    if not keys or business.empty:
        return []
    rows = business[business.position_key.isin(keys) & (business.Art == 'Bestellung')]
    if rows.empty:
        return []
    rows = rows[~api_holds.mask(rows)]
    if rows.empty:
        return []
    invoices = invoices if invoices is not None else partner_invoices.list_invoices()
    links = core.refund_links(business)
    cases = []
    for partner, block in rows.groupby('Partner'):
        partner_keys = set(block.position_key)
        paid_ok = bool((block.paid_at.astype(bool) | block.closed_at.astype(bool)
                        | block[position_workflow.PAID_WITHOUT_INVOICE].astype(bool)).all())
        invoiced = any(
            record['partner'] == partner and record['approved_at']
            and partner_keys.intersection(item['key'] for item in record['expected']['items'])
            for record in invoices)
        row_indices = block.index.tolist()
        refund_idx = [r for r, s in links.items() if s in set(row_indices)]
        combined_keys = [business.loc[i].position_key for i in row_indices] + \
                         [business.loc[i].position_key for i in refund_idx]
        cases.append(dict(round_ids=[round_id], partner=partner, positions=len(row_indices), amount=None,
                           paid_ok=paid_ok, invoiced=invoiced, combined_keys=combined_keys))
    return cases


def _historical_paid_marker(business, case):
    """Representative payment date from whichever existing marker applies -
    reads only already-stored fields, never invents a date."""
    rows = business[business.position_key.isin(case['combined_keys']) & (business.Art == 'Bestellung')]
    if rows.empty:
        return None
    candidates = []
    for column in ('closed_at', 'paid_at', position_workflow.PAID_WITHOUT_INVOICE):
        values = [v for v in rows[column] if v]
        if values:
            candidates.append(max(values))
    return max(candidates) if candidates else None


def _historical_matching_invoice(case, invoices):
    partner_keys = set(case['combined_keys'])
    return next((record for record in invoices if record['partner'] == case['partner'] and record['approved_at']
                 and partner_keys.intersection(item['key'] for item in record['expected']['items'])), None)


def _render_document_line(label, content, filename, key):
    if content:
        st.download_button(label, content, filename, key=key, icon=':material/download:')
    else:
        st.caption(f'{label}: ❌ fehlt')


def _render_archive_detail_table(business, keys, key_suffix):
    rows = business[business.position_key.isin(keys)]
    with st.expander('Prüfdetails', expanded=False):
        if rows.empty:
            st.caption('Keine Positionsdetails verfügbar.')
            return
        columns = [c for c in ('Auszahlung Nr.', 'Bestellnummer', 'SKU', 'Art', 'Erlös_Brutto') if c in rows.columns]
        st.dataframe(rows[columns], hide_index=True, use_container_width=True, key=f'archive-detail-{key_suffix}')


def _render_archive_neutral_partner(round_id, partner, status, business, invoice_row, cases, detail_keys):
    claim_text = euros(float(status['claim'])) if status['claim'] is not None else (
        euros(0) if not status['positions'] else '–')
    icon = _status_icon(status)
    header = f"{partner} · {status['positions']} Positionen · {claim_text} · {icon} {overall_label(status['overall_status'])}"
    with st.expander(header, expanded=False):
        st.write(f"Round-ID: {round_id} · Partner: {partner} · Gruppe: {status['group'] or '–'}")
        st.write(f"Positionen: {status['positions']} · Partnerbetrag: {claim_text}")
        st.write(f"Einzelabrechnung: {_statement_icon(status)}")
        st.write(f"Partnerrechnung: {_invoice_icon(status)}"
                 + (f" · Rechnungsnr. {status['invoice_number']}" if status['invoice_number'] else ''))
        st.write(f"Zahlung: {_payment_icon(status)}"
                 + (f" · {display_date(status['paid_at'])}" if status['paid_at'] else ''))
        st.write(f"Gutschrift/Recovery: {_credit_icon(status)}")
        st.write(f"Gesamtstatus: {_status_icon(status)} {overall_label(status['overall_status'])}")
        if status['blockers']:
            st.caption('Offene Punkte: ' + ' · '.join(status['blockers']))

        st.markdown('**Dokumente**')
        # Only ever the stored final snapshot bytes - never regenerated from
        # live data - and only fetched here, outside any open core.ledger()
        # block (final_file() opens its own; a nested open would deadlock).
        content = partner_snapshot.final_file(round_id, partner) if status['snapshot_status'] == 'vorhanden' else None
        _render_document_line('Einzelabrechnung', content, f'{round_id}_{partner}_final.xlsx',
                               f'archive-final-{round_id}-{partner}')

        if invoice_row:
            st.caption(f"Rechnung hochgeladen {display_date(invoice_row['uploaded_at'])} "
                       f"· geprüft {display_date(invoice_row['reviewed_at'])}"
                       + (f" · Nr. {invoice_row['invoice_number']}" if invoice_row['invoice_number'] else ''))
            _render_document_line('Partnerrechnung', invoice_row['file_bytes'], invoice_row['file_name'],
                                   f'archive-invoice-{round_id}-{partner}')
            if invoice_row['paid_at']:
                note = f" · Notiz: {invoice_row['paid_note']}" if invoice_row['paid_note'] else ''
                st.caption(f"Zahlung: {euros(float(invoice_row['paid_amount']))} am "
                           f"{display_date(invoice_row['paid_at'])}{note}")
        else:
            st.caption('Partnerrechnung: ❌ fehlt' if status['invoice_status'] == 'fehlt' else
                        'Partnerrechnung: ' + _invoice_icon(status))

        if cases:
            st.markdown('**Gutschrift / Rückforderung**')
            for case in cases:
                st.write(f"{case['order_number']} · {euros(abs(float(case['refund_amount'])))} · "
                         f"{'✅ erledigt' if case['status'] == 'erledigt' else '❌ offen'}"
                         + (f" · {display_date(case['resolved_at'])}" if case['resolved_at'] else ''))
                if case.get('credit_file_bytes'):
                    st.download_button('Gutschriftbeleg', case['credit_file_bytes'],
                                        case['credit_file_name'] or f"credit-{case['id']}",
                                        key=f"archive-credit-{case['id']}", icon=':material/download:')

        _render_archive_detail_table(business, detail_keys, f'{round_id}-{partner}')


def _render_archive_neutral_round(round_id, business, payouts, orders):
    result = round_status.round_status(round_id, business=business)
    header = f"{round_id} · {_period(result)} · {round_label(result['round_status'])}"
    with st.expander(header, expanded=(result['round_status'] != 'abgeschlossen')):
        # Phase 1: collect every db-backed fact for every partner in ONE open
        # connection (raw SQL / db= aware calls only - never a self-opening
        # helper here, or the nested core.ledger() would deadlock).
        collected = []
        with core.ledger() as db:
            partner_round_invoices.initialize(db)
            recovery_cases.initialize(db)
            for status in result['partners']:
                partner = status['partner']
                invoice_row = db.execute('SELECT * FROM partner_round_invoices WHERE round_id=? AND partner=?',
                                          (round_id, partner)).fetchone()
                cases = recovery_cases.list_cases(round_id=round_id, partner=partner, db=db)
                assigned_keys = {r[0] for r in db.execute(
                    'SELECT position_key FROM group_b_round_positions WHERE round_id=?', (round_id,))}
                rows = partner_snapshot._partner_round_rows(business, assigned_keys, partner)
                detail_keys = set(rows.position_key) if not rows.empty else set()
                collected.append((status, dict(invoice_row) if invoice_row else None, cases, detail_keys))
        # Phase 2: render - connection closed, safe to call final_file() etc.
        for status, invoice_row, cases, detail_keys in collected:
            _render_archive_neutral_partner(round_id, status['partner'], status, business, invoice_row, cases, detail_keys)


def _render_archive_historical_partner(case, business, payouts, orders, invoices):
    """Renders one historical partner package - either a single-round closed
    slice, or (round_ids has more than one entry) the same cross-round
    combined open case the partner card already shows, so the archive never
    re-splits an already-fixed combined figure (e.g. MH's 59 positions /
    4.299,74 €) back into a per-round amount that would reintroduce the old
    cent-rounding mismatch."""
    label = _historical_case_label(case)
    amount = euros(float(case['amount'])) if case['amount'] is not None else '–'
    zahlung_icon = '✅ bezahlt' if case['paid_ok'] else '❌ Zahlung offen'
    rechnung_icon = '✅ geprüft' if case['invoiced'] else '❌ Rechnung fehlt'
    header = f"{case['partner']} · {case['positions']} Positionen · {amount} · {zahlung_icon} · {rechnung_icon}"
    with st.expander(header, expanded=False):
        rows = business[business.position_key.isin(case['combined_keys'])]
        gruppe = rows.iloc[0].Gruppe if not rows.empty else '–'
        st.write(f"Round-ID: {label} · Partner: {case['partner']} · Gruppe: {gruppe}")
        st.write('Zeitraum: ➖ im Altmodell nicht gespeichert')
        st.write(f"Positionen: {case['positions']} · Partnerbetrag: {amount}")
        st.write('Einzelabrechnung: ✅ vorhanden' if case['combined_keys'] else 'Einzelabrechnung: ❌ fehlt')
        matching_invoice = _historical_matching_invoice(case, invoices)
        st.write(f"Partnerrechnung: {'✅ geprüft' if case['invoiced'] else '❌ fehlt'}"
                 + (f" · Rechnungsnr. {matching_invoice['invoice_number']}"
                    if matching_invoice and matching_invoice.get('invoice_number') else ''))
        paid_marker = _historical_paid_marker(business, case)
        st.write(f"Zahlung: {zahlung_icon}" + (f" · {display_date(paid_marker)}" if paid_marker else ''))
        refund_cases = studio_view.partner_refund_cases(business)
        has_open_refund = bool(set(case['combined_keys']) & set(refund_cases.position_key)) if not refund_cases.empty else False
        st.write(f"Gutschrift/Recovery: {'❌ offen' if has_open_refund else '➖ nicht erforderlich'}")
        gesamt = '✅' if (case['paid_ok'] and case['invoiced'] and not has_open_refund) else '❌'
        st.write(f"Gesamtstatus: {gesamt}")
        offen = []
        if not case['invoiced']:
            offen.append('Rechnung fehlt')
        if not case['paid_ok']:
            offen.append('Zahlung offen')
        if has_open_refund:
            offen.append('Gutschrift offen')
        if offen:
            st.caption('Offene Punkte: ' + ' · '.join(offen))
        st.caption('Bearbeitung (Rechnungsupload/Zahlung) erfolgt in der Partnerkarte, nicht im Archiv.')

        st.markdown('**Dokumente**')
        content = _historical_case_file(business, payouts, orders, case)
        _render_document_line('Historische Einzelabrechnung', content,
                               f"{label.replace('/', '_')}_{case['partner']}_historisch.xlsx",
                               f"archive-hist-file-{label}-{case['partner']}")
        if matching_invoice:
            import partner_invoices
            stored = partner_invoices.stored_original(matching_invoice)
            original = (stored if isinstance(stored, (bytes, bytearray)) else
                        (stored.read_bytes() if stored is not None else None))
            _render_document_line('Partnerrechnung (Original)', original,
                                   matching_invoice['file_name'], f"archive-hist-invoice-{label}-{case['partner']}")

        _render_archive_detail_table(business, set(case['combined_keys']), f"{label}-{case['partner']}")


def _render_archive_historical_round(round_id, round_row, header_icon, cases, business, payouts, orders, invoices):
    status_text = 'abgeschlossen' if header_icon == '✅' else ('offen' if header_icon == '❌' else 'ohne Fälle')
    doc = round_row['evelyn_document_number'] if round_row['evelyn_document_number'] else '❌ fehlt'
    header = f"{round_id} · historisches Altmodell · Beleg {doc} · {header_icon or '➖'} {status_text}"
    with st.expander(header, expanded=(header_icon == '❌')):
        st.caption('Altmodell (nicht neutral_weekly) - keine 003+-Statuslogik, nur belegte historische Fakten.')
        if round_row['evelyn_document_number']:
            st.write(f"Evelyn-/Lexware-Beleg: {round_row['evelyn_document_number']} "
                     f"· {euros(float(round_row['evelyn_amount']))}")
        else:
            st.write('Evelyn-/Lexware-Beleg: ❌ fehlt')
        if not cases:
            st.caption('Keine zuordenbaren historischen Partnerpositionen.')
        for case in cases:
            _render_archive_historical_partner(case, business, payouts, orders, invoices)


def render_archive(business=None, payouts=None, orders=None):
    """Abrechnungsarchiv: the single, read-only, chronological (newest
    first) document/case archive spanning every 2026-003+ round and every
    historical GB-2026-xxx round - built entirely from the same existing
    status/data functions the Partnerkarte (round_status.py,
    partner_snapshot.py) and Rundenübersicht (group_b_rounds.py's own
    matrix) already use. No second status derivation, no new business
    logic, no writes - archive is strictly for viewing and downloading
    already-stored documents. Open historical cases stay visible here too
    (they may also appear in render_open_documents() - one fact, two
    listings, never a second store)."""
    import partner_invoices

    business = position_workflow.positions() if business is None else business
    payouts = core.read_master(core.PAYOUTS_DB_PATH) if payouts is None else payouts
    orders = core.read_master(core.ORDERS_DB_PATH) if orders is None else orders
    invoices = partner_invoices.list_invoices()

    st.subheader('Abrechnungsarchiv')
    st.caption('Zentrales Archiv für alle Abrechnungsrunden und Partnerpakete · neueste zuerst · '
               'Aktionen (Upload, Zahlung, Finalisierung) erfolgen weiterhin in der Partnerkarte.')

    for round_id in _neutral_round_ids():
        _render_archive_neutral_round(round_id, business, payouts, orders)

    # Phase 1: collect every db-backed fact (round rows + per-partner case
    # sets, ascending so a combined case's round_ids read chronologically)
    # in ONE open connection.
    with core.ledger() as db:
        group_b_rounds.initialize(db)
        historical_round_ids_asc = [r[0] for r in db.execute(
            "SELECT id FROM group_b_rounds WHERE source_kind != 'neutral_weekly' ORDER BY sequence ASC")]
        round_rows = {rid: dict(db.execute('SELECT * FROM group_b_rounds WHERE id=?', (rid,)).fetchone())
                      for rid in historical_round_ids_asc}
        per_round_cases = {rid: _historical_round_partner_cases(business, rid, invoices=invoices, db=db)
                            for rid in historical_round_ids_asc}
        partners_seen = sorted({case['partner'] for cases in per_round_cases.values() for case in cases})
        combined_open = {}
        for partner in partners_seen:
            case = _historical_partner_case(business, historical_round_ids_asc, partner, invoices=invoices, db=db)
            if case:
                case['partner'] = partner
                combined_open[partner] = case
    # Phase 2: connection closed - safe to call _historical_matrix() and
    # _historical_case_amount() (both open their own core.ledger()).
    for cases in per_round_cases.values():
        for case in cases:
            _historical_case_amount(business, case)
    for case in combined_open.values():
        _historical_case_amount(business, case)
    header_icons = {rid: _historical_matrix(business, rid)[2] for rid in historical_round_ids_asc}

    # A round-slice that is already fully settled (paid + invoiced) is shown
    # exactly where it happened; a still-open slice is folded into the one
    # cross-round combined case (never re-split back into a per-round
    # amount) and shown once, under the most recent round it touches.
    closed_by_round = {rid: [] for rid in historical_round_ids_asc}
    for round_id, cases in per_round_cases.items():
        closed_by_round[round_id].extend(case for case in cases if case['paid_ok'] and case['invoiced'])
    open_by_last_round = {rid: [] for rid in historical_round_ids_asc}
    for case in combined_open.values():
        open_by_last_round[case['round_ids'][-1]].append(case)

    for round_id in reversed(historical_round_ids_asc):  # newest first
        _render_archive_historical_round(round_id, round_rows[round_id], header_icons[round_id],
                                          closed_by_round[round_id] + open_by_last_round[round_id],
                                          business, payouts, orders, invoices)


_SEARCH_KIND_LABELS = {
    'aktuelle_runde': 'Aktuelle Runde', 'aeltere_runde': 'Ältere Runde (2026-003+)',
    'historisch': 'Historisch (Altmodell)', 'evelyn_beleg': 'Evelyn-/Lexware-Beleg',
    'verworfen': 'Verworfener Beleg',
}


def render_search(business=None, payouts=None, orders=None):
    """Globale Suche: read-only navigation/research only - never writes
    anything (global_search.search() itself never opens a write
    transaction). Renders nothing beyond a single input field until a query
    is actually typed - no empty result table."""
    import global_search

    query = st.text_input('Globale Suche', placeholder='Bestellnummer, SKU, Rechnungsnummer oder Payoutnummer suchen …',
                          label_visibility='collapsed', key='global-search-query')
    if not query or not query.strip():
        return
    results = global_search.search(query, business=business, payouts=payouts, orders=orders)
    if not results:
        st.caption('Kein passender Fall gefunden.')
        return
    st.caption(f'{len(results)} Treffer')
    for result in results:
        label = result['partner'] or result['round_label']
        amount_text = euros(float(result['amount'])) if result['amount'] is not None else '–'
        positions_text = f"{result['positions']} Pos." if result['positions'] is not None else ''
        header = (f"{result['overall_icon']} {label} · {result['round_label']} · {positions_text} · "
                  f"{amount_text} · {result['overall_text']}")
        with st.expander(header, expanded=False):
            st.caption('Trefferart: ' + _SEARCH_KIND_LABELS.get(result['kind'], result['kind']))
            st.caption('Gefunden über: ' + ', '.join(
                f'{match_type} „{value}"' for match_type, value in result['match_types'].items()))
            if result['partner']:
                st.write(f"Partner: {result['partner']}" + (f" · Gruppe: {result['gruppe']}" if result['gruppe'] else ''))
            st.write(f"Round-ID: {result['round_id'] or '–'}")
            st.write(f"Einzelabrechnung/Rechnung: {result['invoice_icon']} {result['invoice_text']}"
                     + (f" · Rechnungsnr. {result['invoice_number']}" if result['invoice_number'] else ''))
            st.write(f"Zahlung: {result['payment_icon']} {result['payment_text']}")
            st.write(f"Gutschrift/Recovery: {result['credit_icon']} {result['credit_text']}")
            if result['open_points']:
                st.caption('Offene Punkte: ' + ' · '.join(result['open_points']))
            nav = result['nav']
            if nav and nav['target'] == 'partnerkarte':
                st.info(f"→ Fall öffnen: Partnerkarte · {nav['gruppe']} · {nav['partner']}")
            elif nav and nav['target'] == 'archiv':
                partner_bit = f" · {nav['partner']}" if nav['partner'] else ''
                st.info(f"→ In Historie öffnen: Abrechnungsarchiv · {nav['round_id']}{partner_bit}")
            elif result['kind'] == 'verworfen':
                st.caption('Verworfen · keine operative Wirkung · nicht auffindbar als aktiver Fall.')


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
