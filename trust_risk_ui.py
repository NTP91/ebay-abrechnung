"""Streamlit presentation only. Existing settlement modules are not called here."""
import json
from datetime import timedelta

import pandas as pd
import streamlit as st

import trust_risk as risk
from ebay_readonly import Client, EbayError, secrets_config


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
        right.caption('Abruf nur auf Anforderung. Keine eBay-Schreibaktionen, Nachrichten oder Lexware-Aufrufe. Bestehende Freigaben und Sperren bleiben maßgeblich.')
    if refresh:
        if 'ebay_readonly_client' not in st.session_state:
            st.session_state.ebay_readonly_client = Client()
        bar = st.progress(0, text='eBay-Verbindung prüfen')
        payout_orders = raw.loc[raw['Auszahlung Nr.'] == risk.PAYOUT, 'Bestellnummer'].unique() if not raw.empty else []
        try:
            snapshot = risk.collect(st.session_state.ebay_readonly_client, payout_orders,
                                    lambda value, name: bar.progress(value, text='eBay-Daten werden gelesen · ' + name))
            risk.save_snapshot(data_dir, snapshot)
            st.success('Abruf gespeichert. Verfügbarkeit der einzelnen Bereiche siehe unten.')
        except (EbayError, OSError):
            st.error('API-Datenstand konnte nicht gespeichert werden. Bitte den Datenzugriff prüfen.')
        finally:
            bar.empty()
    try:
        snapshot = risk.load_snapshot(data_dir)
    except EbayError as exc:
        st.warning(str(exc))
        snapshot = None
    if not snapshot:
        st.info('Noch kein API-Datenstand vorhanden. Account-Status, Fälle und Holds sind nicht verfügbar.')
        st.write('Payout 7718008497 · Bank-Kontrollwert: **491,80 €**. Ein API-Abgleich liegt noch nicht vor.')
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
    brief, details = st.tabs(['Tagesüberblick', 'Fälle & Daten'])
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
        st.write(f"{len(check['order_holds'])} Hold-Transaktionen mit Bezug zu den importierten Bestellungen erkannt. Bestellbezogene Abfragen: {'vollständig' if check['hold_coverage_complete'] else 'nicht vollständig verfügbar'}.")
        with st.expander('Transaktionen, Hold- und Release-Felder'):
            st.json({'Payoutbewegungen': check['transactions'], 'Bestellbezogene Holds': check['order_holds']})
        report = {'fetched_at': snapshot['fetched_at'], 'payout': risk.PAYOUT, **check,
                  'availability': {k: v.get('error', 'verfügbar') for k, v in snapshot['resources'].items()}}
        st.download_button('Prüfbericht herunterladen', json.dumps(report, ensure_ascii=False, indent=2), 'Payout_7718008497_API_Pruefung.json', 'application/json', key='ebay-risk-report')


def show_cases(cases):
    if cases:
        table = pd.DataFrame(cases).drop(columns=['heute'])
        st.dataframe(table, hide_index=True, use_container_width=True)
    else:
        st.info('Keine Fälle im erfolgreich abgerufenen Datenumfang. Datenverfügbarkeit und Aktualität oben beachten.')
