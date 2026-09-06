"""Streamlit presentation only. Existing settlement modules are not called here."""
import json
from datetime import timedelta

import pandas as pd
import streamlit as st

import trust_risk as risk
import audit_case_store
from ebay_readonly import Client, EbayError, secrets_config


CATEGORY_LABELS = {
    'not_as_described': 'Nicht wie beschrieben', 'wrong_item': 'Falscher Artikel',
    'defective': 'Defekt', 'used_instead_of_new': 'Gebraucht statt neu',
    'opened_used': 'Geöffnet/benutzt', 'empty_consumed': 'Leer/verbraucht',
    'incomplete_parts': 'Teile fehlen', 'wrong_variant': 'Falsche Variante',
    'item_not_received': 'Nicht erhalten', 'other_complaint': 'Sonstige Beschwerde',
}


@st.cache_data(ttl=300, show_spinner=False)
def load_audit_cases():
    return audit_case_store.load()


def _local_times(series):
    values = pd.to_datetime(series, errors='coerce', utc=True)
    return values.dt.tz_convert('Europe/Berlin').dt.strftime('%d.%m.%Y %H:%M').fillna('')


def render_case_check():
    st.subheader('Trust/Risk Check')
    st.caption('Fallbasierte Supabase-Auswertung · jede Kombination aus Bestellung, Line-Item, SKU und Partner wird genau einmal gezählt.')
    try:
        data = load_audit_cases()
    except Exception:
        st.warning('Die Supabase-Prüfdaten sind derzeit nicht lesbar. Zugangsdaten und Verbindung prüfen.')
        return
    cases = pd.DataFrame(data['cases'])
    if cases.empty:
        st.info('Noch keine Trust/Risk-Fälle in Supabase vorhanden.')
        return
    filters = st.columns(2)
    partners = sorted(cases.partner_id.dropna().unique())
    selected_partners = filters[0].multiselect('Partner filtern', partners, key='audit-case-partners')
    available_skus = sorted(cases.loc[cases.partner_id.isin(selected_partners), 'sku'].unique() if selected_partners else cases.sku.dropna().unique())
    selected_skus = filters[1].multiselect('SKU filtern', available_skus, key='audit-case-skus')
    visible = cases
    if selected_partners:
        visible = visible[visible.partner_id.isin(selected_partners)]
    if selected_skus:
        visible = visible[visible.sku.isin(selected_skus)]
    st.caption(f'{len(visible)} von {len(cases)} Fällen angezeigt.')
    table = pd.DataFrame({
        'Bestellnummer': visible.order_id, 'Line-Item': visible.line_item_id,
        'Partner': visible.partner_id, 'SKU': visible.sku, 'Artikel': visible.title,
        'Problem erkannt': visible.is_problem.map({True: 'Ja', False: 'Nein'}),
        'Status': visible.case_status.map({'offen': 'Offen', 'geschlossen': 'Geschlossen'}).fillna(visible.case_status),
        'Rückgabe': visible.has_return.map({True: 'Ja', False: '—'}),
        'Nachricht': visible.has_message.map({True: 'Ja', False: '—'}),
        'Dispute': visible.has_dispute.map({True: 'Ja', False: '—'}),
        'Hold': visible.has_hold.map({True: 'Ja', False: '—'}),
        'Negative Bewertung': visible.has_negative_feedback.map({True: 'Ja', False: '—'}),
        'Rückgabegrund': visible.return_reason_de, 'Käuferkommentar': visible.buyer_comment,
        'Problemkategorien': visible.apply(lambda row: ', '.join(label for key, label in CATEGORY_LABELS.items() if bool(row.get(key))) or '—', axis=1),
        'Erstes Ereignis': _local_times(visible.first_event_at),
        'Letzter Kontakt': _local_times(visible.last_contact_at),
    })
    st.dataframe(table, hide_index=True, use_container_width=True, height=520)

    st.subheader('Probleme nach Partner')
    partner_rows = pd.DataFrame(data['partners'])
    if not partner_rows.empty:
        partner_rows = partner_rows.rename(columns={
            'partner_id': 'Partner', 'problem_cases': 'Fälle', 'returns': 'Rückgaben',
            'messages': 'Nachrichten', 'disputes': 'Disputes', 'holds': 'Holds',
            'negative_feedback': 'Negative Bewertungen', **CATEGORY_LABELS,
            'affected_orders': 'Betroffene Bestellungen', 'affected_skus': 'Betroffene SKUs',
            'order_ids': 'Bestellnummern', 'sku_list': 'SKU-Liste',
        })
        st.dataframe(partner_rows, hide_index=True, use_container_width=True)

    st.subheader('Auffällige SKUs')
    sku_rows = pd.DataFrame(data['skus'])
    if sku_rows.empty:
        st.info('Keine SKU mit mehreren Fällen im aktuellen Datenstand.')
    else:
        sku_rows = sku_rows.rename(columns={'sku': 'SKU', 'partner_id': 'Partner', 'problem_cases': 'Fälle',
                                            'repeat_count': 'Wiederholungen', 'affected_orders': 'Betroffene Bestellungen',
                                            'order_ids': 'Bestellnummern', **CATEGORY_LABELS})
        st.dataframe(sku_rows, hide_index=True, use_container_width=True)
        st.caption('Aufgeführt werden ausschließlich SKU-/Partner-Kombinationen mit mehr als einem Fall.')


def render(data_dir, catalogue, orders, raw):
    st.subheader('Trust / Risk')
    st.caption('Durchstartaccount · eBay lesen, Risiken prüfen, nächste Schritte vorbereiten')
    configured = True
    try:
        secrets_config()
    except EbayError as exc:
        configured = False
        st.info(str(exc))
    with st.container(border=True):
        left, right = st.columns([1, 2])
        refresh = left.button('eBay-Daten aktualisieren', type='primary', disabled=not configured, key='ebay-risk-refresh', use_container_width=True)
        right.caption('Manueller Fallback zum täglichen Finances-Import. Neue Payouts und Bewegungen werden geprüft importiert; bestehende Belege und Holds bleiben geschützt. Keine Lexware-Aufrufe.')
    if refresh:
        if 'ebay_readonly_client' not in st.session_state:
            st.session_state.ebay_readonly_client = Client()
        try:
            import ebay_sync
            with st.spinner('Payouts und Finanztransaktionen werden abgerufen und geprüft …'):
                result=ebay_sync.run(data_dir,'manual',st.session_state.ebay_readonly_client)
            st.session_state['ebay_sync_result']=result
            st.rerun()
        except (EbayError, OSError):
            st.error('API-Datenstand konnte nicht gespeichert werden. Bitte den Datenzugriff prüfen.')
    result=st.session_state.pop('ebay_sync_result',None)
    if result:
        if result['status']=='success':st.success('API-Import erfolgreich. Details unter Historie → Payouts.')
        else:st.warning(result.get('error') or 'API-Import noch nicht vollständig.')
    try:
        snapshot = risk.load_snapshot(data_dir)
    except EbayError as exc:
        st.warning(str(exc))
        snapshot = None
    if not snapshot:
        st.info('Noch kein API-Datenstand vorhanden. Account-Status, Fälle und Holds sind nicht verfügbar.')
        st.write('Payout 7718008497 · Bank-Kontrollwert: **491,80 €**. Ein API-Abgleich liegt noch nicht vor.')
        render_case_check()
        return
    stamp = risk.local_date(snapshot.get('fetched_at'))
    st.caption('Datenstand: ' + (stamp.strftime('%d.%m.%Y %H:%M') if stamp else 'unbekannt') + ' · Finanztransaktionen: letzte 90 Tage; Referenz-Payout zusätzlich separat abgefragt.')
    stale = not stamp or risk.now_utc() - stamp > timedelta(hours=24)
    if stale:
        st.warning('Dieser Datenstand ist älter als 24 Stunden oder undatiert. Fristen und offene Fälle vor einer Handlung in eBay prüfen.')
    missing = [name for name, value in snapshot['resources'].items() if not value.get('available')]
    if missing:
        st.warning('Datenabdeckung unvollständig. Angezeigte Fälle sind nur die erfolgreich abgerufenen Vorgänge; fehlende Daten bedeuten keine Entwarnung.')
    model = risk.audit(snapshot, catalogue, orders)
    standards = risk.resource(snapshot, 'standards')
    profiles = (standards or {}).get('standardsProfiles', [])
    current = [p for p in profiles if (p.get('cycle', {}).get('cycleType') if isinstance(p.get('cycle'), dict) else p.get('cycle')) == 'CURRENT']
    health = ' / '.join(sorted({p.get('standardsLevel', 'nicht verfügbar') for p in current})) or 'nicht verfügbar'
    count = lambda name, kind: str(sum(c['Vorgang'] == kind for c in model['cases'])) if risk.resource(snapshot, name) is not None else '—'
    values = [health, count('returns', 'Rückgabe'), count('disputes', 'Payment Dispute'), count('transactions', 'Einbehalt'), str(model['critical']) if not missing else '≥ ' + str(model['critical']), str(model['today']) if not missing else '≥ ' + str(model['today'])]
    for col, label, value in zip(st.columns(6), ['Account-Status', 'Offene Rückgaben', 'Payment Disputes', 'Aktive Holds¹', 'Kritische Vorgänge', 'Heute bearbeiten'], values):
        col.metric(label, value)
    st.caption('¹ Erkannte Hold-Transaktionen im 90-Tage-Abruf, keine garantierte Gesamtzahl aller aktiven Einbehalte. Fristen beziehen sich auf Europe/Berlin.')
    brief, details, case_check = st.tabs(['Tagesüberblick', 'Fälle & Daten', 'Trust/Risk Check'])
    with brief:
        with st.container(border=True):
            st.subheader('KI-Audit · regelbasierte Auswertung')
            st.caption('Keine angebundene KI. Regeln: Frist heute/überfällig → kritisch; Frist morgen oder ACTION_NEEDED → handeln; übrige Fälle → beobachten.')
            st.write(f"Im verfügbaren Datenstand: **{model['critical']} kritisch**, **{model['today']} mit Frist heute oder früher**, **{len(model['partners'])} betroffene Partner**.")
            st.write(f"{sum(c['Priorität'].startswith('3') for c in model['cases'])} Vorgänge zur Beobachtung. Fehlende Fristen direkt in eBay prüfen.")
            for profile in profiles:
                cycle = profile.get('cycle')
                if isinstance(cycle, dict):
                    cycle = cycle.get('cycleType', '')
                level = profile.get('standardsLevel', 'nicht verfügbar')
                st.write(f"Verkäuferstandard {profile.get('program', '')} · {cycle}: **{level}**")
                if level == 'BELOW_STANDARD':
                    st.warning('Verkäuferstandard unter Mindestniveau: betroffene Qualitätsmetriken und Ursachen in eBay prüfen.')
            if model['repeated']:
                st.write('Mehrere beobachtete Rückgaben/Streitfälle je eindeutig zugeordneter SKU:')
                st.dataframe(pd.DataFrame([{'SKU': k, 'Vorgänge': v} for k, v in model['repeated'].items()]), hide_index=True, use_container_width=True)
                st.caption('Fallhäufigkeit im Datenstand, keine berechnete Retourenquote.')
            service = risk.service_metrics(snapshot)
            if service:
                st.write('**Customer Service · INR / INAD**')
                st.dataframe(pd.DataFrame(service), hide_index=True, use_container_width=True)
                if any(row['eBay-Einstufung'] in ('HIGH', 'VERY_HIGH') for row in service):
                    st.warning('eBay weist eine hohe Service-Fallquote aus. Betroffene Segmente und Ursachen im Verkäuferkonto prüfen.')
            else:
                st.caption('INR-/INAD-Kennzahlen: keine strukturierten Metriken im verfügbaren Datenstand.')
            funds = risk.resource(snapshot, 'funds')
            if funds:
                st.write('**Gelder im eBay-Konto**')
                for col, field, label in zip(st.columns(4), ['totalFunds', 'availableFunds', 'processingFunds', 'fundsOnHold'], ['Gesamt', 'Verfügbar', 'In Bearbeitung', 'Einbehalten']):
                    col.metric(label, risk.euro(risk.money(funds.get(field))))
        show_cases(model['cases'])
        st.subheader('Partner informieren')
        st.caption('Text kopieren: Kopiersymbol rechts im jeweiligen Textblock verwenden. Der Text wird nicht versendet. Käuferangaben und Gründe beschreiben Fälle; daraus wird keine allgemeine Kundenzufriedenheit abgeleitet.')
        for partner, text in model['partners'].items():
            with st.expander(partner + ' · Handlungsempfehlung'):
                st.code(text, language=None, wrap_lines=True)
        if not model['partners']:
            st.info('Keine eindeutig zugeordneten Partnerfälle im verfügbaren Datenstand.')
    with details:
        show_cases(model['cases'])
        st.subheader('Account Health & Datenverfügbarkeit')
        for name, value in snapshot['resources'].items():
            if name.startswith('order_'):
                continue
            with st.expander(name + (' · verfügbar' if value.get('available') else ' · nicht verfügbar')):
                if value.get('available'):
                    st.json(value['data'])
                else:
                    st.warning(value.get('error', 'Nicht verfügbar'))
    with case_check:
        render_case_check()
    with st.container(border=True):
        st.subheader('Finances-Prüfung · Payout 7718008497')
        check = risk.finance_check(snapshot)
        for col, label, value in zip(st.columns(4), ['Bank-Kontrollwert', 'API-Payoutbetrag', 'Eindeutig finale Bewegungen', 'Differenz zum Bankbetrag'], [check['reference'], check['api_amount'], check['final_sum'], check['difference']]):
            col.metric(label, risk.euro(risk.Decimal(value)) if value is not None else '—')
        if check['reconstructed']:
            st.success('491,80 € sind aus den vollständig abgerufenen, eindeutig final zugeordneten API-Bewegungen rechnerisch bestätigt.')
        else:
            st.warning('491,80 € sind durch diesen API-Datenstand noch nicht zuverlässig rekonstruiert.')
            for issue in check['issues']:
                st.write('• ' + issue)
        st.caption(check['note'])
        st.write(f"{len(check['order_holds'])} Transaktionen mit Status FUNDS_ON_HOLD. Zusätzlich {len(check['booked_hold_movements'])} gebuchte Einbehalts-Abbuchungen im Payout erkannt. Bestellbezogene Abfragen: {'vollständig' if check['hold_coverage_complete'] else 'nicht vollständig verfügbar'}.")
        st.caption('Auch Einbehalts-Abbuchungen können den Status PAYOUT tragen. PAYOUT allein bedeutet keine Freigabe einer Bestellung; diese Auswertung ändert keine Abrechnung.')
        with st.expander('Transaktionen, Hold- und Release-Felder'):
            st.json({'Payoutbewegungen': check['transactions'], 'Bestellbezogene Holds': check['order_holds'], 'Gebuchte Einbehalte': check['booked_hold_movements']})
        report = {'fetched_at': snapshot['fetched_at'], 'payout': risk.PAYOUT, **check,
                  'availability': {k: v.get('error', 'verfügbar') for k, v in snapshot['resources'].items()}}
        st.download_button('Prüfbericht herunterladen', json.dumps(report, ensure_ascii=False, indent=2), 'Payout_7718008497_API_Pruefung.json', 'application/json', key='ebay-risk-report')


def show_cases(cases):
    if cases:
        table = pd.DataFrame(cases).drop(columns=['heute'])
        st.dataframe(table, hide_index=True, use_container_width=True)
    else:
        st.info('Keine Fälle im erfolgreich abgerufenen Datenumfang. Datenverfügbarkeit und Aktualität oben beachten.')
