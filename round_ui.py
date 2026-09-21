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
import partner_round_invoices
import partner_snapshot
import recovery_cases
import round_status

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


def render_round_matrix(result):
    import pandas as pd
    columns = {}
    for p in result['partners']:
        columns[p['partner']] = [
            invoice_label(p['invoice_status']),
            payment_label(p['payment_status'], p['paid_at']),
            credit_label(p['credit_status']),
            overall_label(p['overall_status']),
        ]
    frame = pd.DataFrame(columns, index=['Rechnung', 'Zahlung', 'Gutschrift', 'Status'])
    st.dataframe(frame, use_container_width=True)


def render_historical_rounds():
    with core.ledger() as db:
        group_b_rounds.initialize(db)
        rows = db.execute("SELECT id, evelyn_amount, created_at FROM group_b_rounds "
                           "WHERE source_kind != 'neutral_weekly' ORDER BY sequence").fetchall()
    if not rows:
        return
    with st.expander(f'Historische Runden (altes Modell) · {len(rows)}', expanded=False):
        st.caption('001/002 laufen weiterhin nach der alten Geschäftslogik - hier nur zur Einordnung, '
                    'keine neue 003+-Statuslogik.')
        for row in rows:
            st.write(f"**{row['id']}** · Evelyn-Betrag {euros(float(row['evelyn_amount']))} · "
                     f"angelegt {display_date(row['created_at'])}")


def render_round_overview(active_round_id=None):
    """Returns the round_id the caller should show a partner-card workspace
    for (the one the user picked, or the first non-abgeschlossen round)."""
    st.subheader('Wochenrunden 2026-003+')
    round_ids = _neutral_round_ids()
    if not round_ids:
        st.caption('Noch keine neutrale Wochenrunde vorhanden.')
        render_historical_rounds()
        return None

    results = {rid: round_status.round_status(rid) for rid in round_ids}
    selected = active_round_id if active_round_id in results else next(
        (rid for rid in round_ids if results[rid]['round_status'] != 'abgeschlossen'), round_ids[0])

    for round_id in round_ids:
        result = results[round_id]
        header = f"{round_id} · {_period(result)} · {round_label(result['round_status'])}"
        with st.expander(header, expanded=(result['round_status'] != 'abgeschlossen')):
            render_round_matrix(result)
            if result['blockers']:
                st.caption('Offene Punkte: ' + ' · '.join(result['blockers']))
            if st.button('Partnerkarten öffnen', key=f'open-round-{round_id}'):
                selected = round_id
                st.session_state['round_ui_active_round'] = round_id
                st.rerun()

    render_historical_rounds()
    return selected


def render_statement_panel(round_id, partner, status):
    st.markdown('**Einzelabrechnung**')
    st.write(f"Positionen: {status['positions']}")
    st.write('Partneranspruch: ' + (euros(float(status['claim'])) if status['claim'] is not None else 'noch nicht final'))
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


def render_partner_card(round_id, partner):
    status = round_status.partner_status(round_id, partner)
    st.markdown(f"## {partner} · {round_id}")
    cols = st.columns(4)
    cols[0].metric('Positionen', status['positions'])
    cols[1].metric('Partneranspruch', euros(float(status['claim'])) if status['claim'] is not None else '–')
    cols[2].metric('Status', overall_label(status['overall_status']))
    cols[3].metric('Gruppe', status['group'] or '–')

    left, right = st.columns(2)
    with left:
        render_invoice_and_payment_panel(round_id, partner, status)
    with right:
        render_statement_panel(round_id, partner, status)
    render_recovery_panel(round_id, partner)


def render_partner_picker(round_id):
    result = round_status.round_status(round_id)
    names = [p['partner'] for p in result['partners']]
    if not names:
        return
    default = st.session_state.get('round_ui_active_partner')
    index = names.index(default) if default in names else 0
    partner = st.selectbox('Partner', names, index=index, key=f'partner-picker-{round_id}')
    st.session_state['round_ui_active_partner'] = partner
    render_partner_card(round_id, partner)


def render_open_documents():
    """'Offene Belege': every partner/round still missing a reviewed
    invoice or an open recovery credit, across all neutral rounds - MH's
    paid-without-invoice historical case belongs to the old 001/002 model
    and is deliberately out of scope here (round_status.py refuses it)."""
    round_ids = _neutral_round_ids()
    missing_invoice, missing_credit = [], []
    for round_id in round_ids:
        result = round_status.round_status(round_id)
        for p in result['partners']:
            if p['invoice_status'] == 'fehlt':
                missing_invoice.append((round_id, p['partner']))
            if p['credit_status'] == 'fehlt':
                missing_credit.append((round_id, p['partner']))
    total_open = len(missing_invoice) + len(missing_credit)
    with st.expander(f'Offene Belege · {"⚠ " + str(total_open) + " offen" if total_open else "✅ keine offen"}',
                      expanded=bool(total_open)):
        if not total_open:
            st.caption('Keine offenen Belege.')
        for round_id, partner in missing_invoice:
            st.write(f'⚠ {partner} · {round_id} · Rechnung fehlt')
        for round_id, partner in missing_credit:
            st.write(f'⚠ {partner} · {round_id} · Gutschrift fehlt')


def render_invoice_history():
    round_ids = _neutral_round_ids()
    rows = []
    with core.ledger() as db:
        partner_round_invoices.initialize(db)
        for round_id in round_ids:
            rows.extend(dict(r) for r in db.execute(
                'SELECT * FROM partner_round_invoices WHERE round_id=? ORDER BY uploaded_at DESC', (round_id,)))
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


def render(business=None):
    st.header('Runde 2026-003+')
    st.caption('Zentrale Statuslogik (round_status.py) - keine eigene Berechnung in der Oberfläche.')
    active = render_round_overview(st.session_state.get('round_ui_active_round'))
    if active:
        st.divider()
        render_partner_picker(active)
    st.divider()
    render_open_documents()
    render_invoice_history()
