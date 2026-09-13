"""Streamlit presentation only. Existing settlement modules are not called here."""
import json
from datetime import timedelta

import pandas as pd
import streamlit as st

import trust_risk as risk
import audit_case_store
import trust_risk_reporting as reporting
import mh_reconciliation
import supabase_store
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
    st.subheader('Operative Qualitätsauswertung')
    st.caption('Echte, eindeutig zugeordnete Kundenfälle. Holds, Verkäuferantworten und neutrale Nachrichten sind ausgeschlossen.')
    try:
        data = load_audit_cases()
    except Exception:
        st.warning('Die Supabase-Prüfdaten sind derzeit nicht lesbar. Zugangsdaten und Verbindung prüfen.')
        return
    report=reporting.aggregate(pd.DataFrame(data['cases']),pd.DataFrame(data['orders']))
    if not report['total']:
        st.info('Noch keine eindeutig zugeordneten Qualitätsfälle vorhanden.')
        return
    top=report['partners_by_cases'].iloc[0]
    repeated=int((report['skus'].Kennzeichnung=='Wiederholt auffällig').sum())
    for col,label,value in zip(st.columns(4),['Echte Qualitätsfälle','Top-Partner','Wiederholt auffällige SKUs','Priorität 1'],
                               [report['total'],f"{top.Partner} · {int(top['Fälle'])}",repeated,len(report['priority'])]):
        col.metric(label,value)
    st.caption('Absolute Fallzahlen und volumenbereinigte Quoten sind getrennt ausgewiesen. Mehrere Signale desselben Falls erhöhen die Fallzahl nicht.')
    partner_tab,group_tab,sku_tab,problem_tab,priority_tab,negative_tab,cases_tab=st.tabs(
        ['Partnerquoten','Gruppen','SKU-Quoten','Problemarten','Priorität 1','Negative Bewertungen','Alle Fälle'])
    with partner_tab:
        st.markdown('**Nach Fehlerquote**')
        table=report['partners'].copy()
        for column in ('Anteil','Fälle je 100 Bestellungen','Fehlerquote'):
            table[column]=table[column].map(lambda value:f'{value:.2f} %' if pd.notna(value) else '—')
        st.dataframe(table,hide_index=True,width='stretch')
        st.caption(f'Mindestfallzahl für eine belastbare Quotenbewertung: {reporting.MIN_CASES_FOR_RATE}; Mindestvolumen: {reporting.MIN_ORDER_VOLUME} Bestellpositionen.')
        st.markdown('**Nach absoluten Qualitätsfällen**')
        absolute=report['partners_by_cases'][['Partner','Bestellungen','Fälle','Fehlerquote','Datenbasis']].copy()
        absolute['Fehlerquote']=absolute['Fehlerquote'].map(lambda value:f'{value:.2f} %' if pd.notna(value) else '—')
        st.dataframe(absolute,hide_index=True,width='stretch')
    with group_tab:
        st.dataframe(report['groups'],hide_index=True,width='stretch')
    with sku_tab:
        for heading, source in [('Top-SKUs nach Fehlerquote',report['skus_by_rate']),('Top-SKUs nach absoluten Fällen',report['skus_by_cases'])]:
            st.markdown(f'**{heading}**')
            table=source.head(20).copy()
            table['Fehlerquote']=table['Fehlerquote'].map(lambda value:f'{value:.2f} %' if pd.notna(value) else '—')
            st.dataframe(table,hide_index=True,width='stretch')
        if not report['unresolved_skus'].empty:
            st.markdown('**Nicht aufgelöste Partnerpräfixe**')
            st.dataframe(report['unresolved_skus'],hide_index=True,width='stretch')
        st.caption('Wiederholungsfälle und hohe Fehlerquoten werden getrennt gekennzeichnet. Kleine Stichproben bleiben sichtbar, fließen aber nicht allein in Priorität 1 ein.')
    with problem_tab:
        st.dataframe(report['problems'],hide_index=True,width='stretch')
    with priority_tab:
        st.dataframe(report['priority'],hide_index=True,width='stretch')
    with negative_tab:
        st.dataframe(report['negative'],hide_index=True,width='stretch')
    with cases_tab:
        filters=st.columns(2)
        partners=sorted(report['cases'].Partner.unique())
        selected_partners=filters[0].multiselect('Partner filtern',partners,key='audit-case-partners')
        available=report['cases'][report['cases'].Partner.isin(selected_partners)] if selected_partners else report['cases']
        selected_skus=filters[1].multiselect('SKU filtern',sorted(available.SKU.unique()),key='audit-case-skus')
        visible=available[available.SKU.isin(selected_skus)] if selected_skus else available
        st.caption(f'{len(visible)} von {report["total"]} Qualitätsfällen angezeigt.')
        st.dataframe(visible,hide_index=True,width='stretch',height=520)


def _mh_tagged_rows(rows, art_label):
    return [{'Typ': art_label, **row} for row in rows]


def _mh_detail_table(title, rows):
    if rows:
        st.write('**' + title + f' ({len(rows)})**')
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


def render_mh_reconciliation_result(result):
    precondition = result['precondition']
    st.write('**Schritt 1 · Bestand vor dem Rohdatenvergleich (exakt die Menge, die „Einzelabrechnung herunterladen“ für MH jetzt exportieren würde)**')
    pre_cols = st.columns(2)
    pre_cols[0].metric('reguläre Positionen', f"{precondition['regular_count']}/{mh_reconciliation.EXPECTED_REGULAR}")
    pre_cols[1].metric('Refunds', f"{precondition['refund_count']}/{mh_reconciliation.EXPECTED_REFUNDS}")
    if not (precondition['regular_ok'] and precondition['refund_ok']):
        st.warning(
            'Der exportierbare Bestand weicht bereits vor dem Rohdatenvergleich von der Erwartung ab '
            '(z. B. weil einzelne Positionen bereits geprüft/bezahlt/abgeschlossen sind oder ein API-Einbehalt '
            'vorliegt und dadurch nicht mehr im nächsten Download enthalten sind). Der folgende Rohdatenvergleich '
            'bezieht sich auf den tatsächlich exportierbaren Bestand, nicht auf die ursprüngliche Erwartung.'
        )
    st.write('**Schritt 2 · Abgleich dieses exportierbaren Bestands gegen die Supabase-Rohdaten**')
    regular, refunds = result['regular'], result['refunds']
    missing = _mh_tagged_rows(regular['missing'], 'Regulär') + _mh_tagged_rows(refunds['missing'], 'Refund')
    extra = _mh_tagged_rows(regular['extra'], 'Regulär') + _mh_tagged_rows(refunds['extra'], 'Refund')
    duplicates = _mh_tagged_rows(regular['duplicates'], 'Regulär') + _mh_tagged_rows(refunds['duplicates'], 'Refund')
    mismatches = _mh_tagged_rows(regular['amount_mismatches'], 'Regulär') + _mh_tagged_rows(refunds['amount_mismatches'], 'Refund')
    if result['ok']:
        st.success(
            f"{regular['matched']}/{mh_reconciliation.EXPECTED_REGULAR} regulär gematcht, "
            f"{refunds['matched']}/{mh_reconciliation.EXPECTED_REFUNDS} Refunds gematcht, "
            "0 fehlend, 0 zusätzlich, 0 Dubletten, 0 Betragsabweichungen."
        )
    else:
        st.error('Abweichung(en) gefunden — Details unten. Keine Freigabe.')
    cols = st.columns(6)
    cols[0].metric('regulär gematcht', f"{regular['matched']}/{regular['total_reviewed']}")
    cols[1].metric('Refunds gematcht', f"{refunds['matched']}/{refunds['total_reviewed']}")
    cols[2].metric('fehlend', len(missing))
    cols[3].metric('zusätzlich', len(extra))
    cols[4].metric('Dubletten', len(duplicates))
    cols[5].metric('Betragsabweichungen', len(mismatches))
    st.caption('Lauf: ' + result['run_at'] + ' · Erwartung: 59 reguläre Positionen, 7 Refunds, Payouts '
               + ', '.join(mh_reconciliation.PAYOUT_IDS))
    _mh_detail_table('Fehlend (in Rohdaten, nicht in Abrechnung)', missing)
    _mh_detail_table('Zusätzlich (in Abrechnung, nicht in Rohdaten)', extra)
    _mh_detail_table('Dubletten', duplicates)
    _mh_detail_table('Betragsabweichungen', mismatches)


def render_mh_reconciliation():
    with st.container(border=True):
        st.subheader('MH-Rohdatenabgleich – Diagnose')
        st.caption(
            'Read-only 1:1-Abgleich für Partner MH, Payouts 01.09.–08.09.2026: exakt die Positionsmenge, '
            'die „Einzelabrechnung herunterladen“ jetzt exportieren würde (gleiche Auswahl-/Status-/Sperr-/'
            'Offen-Filter wie im echten Download), gegen die rohen eBay-API-Transaktionen (Supabase Postgres '
            'public.orders / public.payout_transactions). Rein lesend — nur SELECT-Abfragen, kein Import, '
            'keine Statusänderung, keine Datenbankänderung.'
        )
        if st.button('Abgleich jetzt ausführen (nur MH, 01.09.–08.09.2026)', key='mh-reconciliation-run'):
            try:
                with st.spinner('Rohdaten und Abrechnungsbestand werden read-only verglichen …'):
                    st.session_state['mh_reconciliation_result'] = mh_reconciliation.check()
            except supabase_store.StoreError as exc:
                st.session_state['mh_reconciliation_result'] = None
                st.error('Abgleich nicht möglich: ' + str(exc))
            except Exception as exc:
                st.session_state['mh_reconciliation_result'] = None
                st.error('Abgleich fehlgeschlagen (' + type(exc).__name__ + '). Bitte erneut versuchen.')
        result = st.session_state.get('mh_reconciliation_result')
        if result:
            render_mh_reconciliation_result(result)


def render(data_dir, catalogue, orders, raw):
    st.subheader('Trust / Risk')
    st.caption('Durchstartaccount · eBay lesen, Risiken prüfen, nächste Schritte vorbereiten')
    render_mh_reconciliation()
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
