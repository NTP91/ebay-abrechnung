import json
import os
import re
from pathlib import Path

import streamlit as st
import core
import data_status
from partner_export import export_partner_excel

st.set_page_config(page_title="Payout Studio", page_icon="€", layout="wide")
st.markdown("""
<style>
.stApp {background:#0b1220;color:#e5edf8}
[data-testid="stSidebar"] {background:#111d30}
h1,h2,h3 {color:#eef4ff !important;letter-spacing:-.025em}
[data-testid="stMetric"] {background:#15233a;border:1px solid #263a55;border-radius:14px;padding:20px}
[data-testid="stMetricLabel"],[data-testid="stMetricValue"] {color:#eef4ff}
[data-testid="stExpander"] {border:1px solid #263a55;border-radius:12px}
.stButton>button {border-radius:9px}
.block-container {padding-top:2.5rem;max-width:1500px}
[data-testid="stHeader"] {background:#0b1220}
[data-testid="stWidgetLabel"] p, [data-testid="stCaptionContainer"] p,
[role="tab"] {color:#bdcde3 !important}
[data-testid="stMetricValue"] div {font-size:clamp(1.15rem,2vw,1.65rem);white-space:normal}
[data-testid="stFileUploaderDropzone"] {background:#192a43;color:#dce7f8}
[data-testid="stFileUploaderDropzone"] button {background:#294466;color:#f0f5ff;border:1px solid #466789}
[data-testid="stFileUploaderDropzone"] small {color:#bdcde3 !important}
[data-testid="stSidebar"] small {color:#bdcde3}
.stButton button, .stDownloadButton button {background:#203652;color:#e5edf8;border:1px solid #355173}
.stButton button:disabled {color:#899bb3;opacity:.6}
[data-testid="stAlert"] {color:#e5edf8}
</style>
""", unsafe_allow_html=True)
st.caption("PARTNERABRECHNUNG · EBAY → LEXWARE OFFICE")
st.title("Payout Studio")
st.write("1 · Dateien hochladen   →   2 · Zuordnung prüfen   →   3 · Geldeingang bestätigen   →   4 · Entwurf erstellen")


def euros(value):
    return f"{value:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def download(label, frame, filename, key):
    st.download_button(label, core.export_to_excel(frame), filename,
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key=key)


def partner_download(label, frame, filename, key, statement_type='partner'):
    try:
        data = export_partner_excel(frame, statement_type=statement_type)
    except ValueError as exc:
        st.error(f"Partnerexport angehalten: {exc}")
        return
    st.download_button(label, data, filename,
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key=key)


with st.sidebar:
    st.header("Import & Verbindung")
    payouts = st.file_uploader("Payout-Dateien", type=["csv"], accept_multiple_files=True)
    orders = st.file_uploader("Bestellberichte", type=["csv", "xlsx"], accept_multiple_files=True)
    if st.button("Dateien sicher importieren", type="primary", disabled=not (payouts or orders)):
        try:
            receipts = []
            for kind, files in [('orders', orders), ('payout', payouts)]:
                for uploaded in files:
                    receipts.append(data_status.import_file(uploaded, kind))
            st.session_state['import_receipts'] = receipts
        except Exception as exc:
            st.error(f"Import angehalten: {exc}")
    for receipt in st.session_state.get('import_receipts', []):
        st.write(receipt['filename'])
        if receipt.get('transactions'):
            counts = receipt['transactions']
            st.caption(f"Neu ausgezahlte Positionen: {counts['new_paid']} (davon zuvor offen: {counts['assigned_open']}) · Bereits bekannte ausgezahlte Positionen: {counts['known_paid']}")
            st.caption(f"Neue offene Positionen: {counts['new_open']} · Weiterhin offene Positionen: {counts['still_open']} · Offene Zuordnungsfehler: {receipt['issues']}")
        else:
            st.caption(f"Erkannt: {receipt['detected']} · Neu: {receipt['added']} · Bereits vorhanden: {receipt['present']} · Nicht zuordenbar/unvollständig: {receipt['issues']}")
        if receipt['error']:
            st.error('Fehler: ' + receipt['error'])
        else:
            st.success('Import abgeschlossen · keine Fehler')
        for payout in receipt['payouts']:
            text = 'Payout ' + payout['number']
            if payout['known']:
                text += ' bereits vorhanden · keine neuen Daten übernommen'
            text += ' · ' + payout.get('status', 'Import angehalten')
            if payout.get('locked'):
                text += ' · BEREITS ABGERECHNET / GESPERRT' if payout.get('invoice') else ' · GESPERRT'
            st.info(text)
    st.divider()
    api_key = st.text_input("Lexware API-Key", type="password")
    st.caption("Kundennummer 16335 · ausschließlich Entwürfe · kein Versand")
    st.warning("CSV-Dateien und Settlement_State.sqlite3 benötigen einen persistenten Datenträger. Für Cloud-Rebuilds extern sichern.")
    for label, filename in [("Payouts sichern", core.PAYOUTS_DB_PATH), ("Bestellungen sichern", core.ORDERS_DB_PATH)]:
        if Path(filename).exists():
            st.download_button(label, Path(filename).read_bytes(), Path(filename).name)
    st.caption("Das Rechnungsregister darf niemals zum erneuten Abrechnen gelöscht werden. Archivierung/Löschen ist hier gesperrt.")
    if Path(core.PAYOUTS_DB_PATH).exists():
        st.download_button('Vollständiges Backup inkl. Rechnungssperren', core.backup_data(), 'Settlement_Backup.zip', 'application/zip')

try:
    master = core.load_master_data()
    states = core.sync_status(master)
    overview = data_status.overview(master, states)
except Exception as exc:
    st.error(f"Datenprüfung angehalten: {exc}")
    st.stop()

st.subheader('Datenstand')
latest = overview['latest']
st.caption('Letzter bekannter Payout: ' + (latest['Payoutnummer'] + ' · ' + latest['Datum / Zeitraum'] if latest else 'noch keiner'))
st.caption('Bestelldaten vorhanden bis: ' + overview['order_end'] if overview['order_end'] else 'Bestelldatenstand: kein zuverlässig lesbares Bestelldatum vorhanden')
st.caption(f"Noch nicht abgerechnete Payouts: {overview['unbilled']} · Reservierte/unklare Versuche bleiben gesperrt.")
raw_transactions = core.read_master(core.PAYOUTS_DB_PATH)
open_transactions = raw_transactions[raw_transactions['Auszahlung Nr.'] == '']
st.caption(f"Offen / noch keinem Payout zugeordnet: {len(open_transactions)} Positionen · kein Fehlerzustand, nicht Bestandteil der Abrechnung.")
if not open_transactions.empty:
    with st.expander('Offene Transaktionen ohne Payout'):
        st.dataframe(open_transactions[['Datum', 'Bestellnummer', 'Transaktionsnummer', 'Artikelnummer', 'Typ']], hide_index=True, use_container_width=True)
for gap in overview['gaps']:
    st.warning(gap)
with st.expander('Payout- und Importhistorie', expanded=False):
    st.dataframe(core.pd.DataFrame(overview['history']), hide_index=True, use_container_width=True, height=220)
    logs = overview['imports'].copy()
    if not logs.empty:
        logs = logs[logs.kind == 'orders'].head(20)
        logs['at'] = core.pd.to_datetime(logs['at'], utc=True).dt.tz_convert('Europe/Berlin').dt.strftime('%d.%m.%Y %H:%M').fillna('nicht bekannt')
        for column in ('start', 'end'):
            logs[column] = core.pd.to_datetime(logs[column]).dt.strftime('%d.%m.%Y').fillna('nicht bekannt')
        st.dataframe(logs[['filename', 'start', 'end', 'at', 'added', 'present', 'error']].rename(columns={'filename':'Bestellbericht', 'start':'Von', 'end':'Bis', 'at':'Importdatum', 'added':'Neue Positionen', 'present':'Bereits vorhanden', 'error':'Fehler'}), hide_index=True, use_container_width=True, height=220)
    st.caption('Zeiträume beruhen auf lesbaren Datumsfeldern. Für Altimporte ohne Protokoll wird kein Importdatum erfunden. Ein letztes Bestelldatum belegt keine lückenlose Abdeckung.')

if not master.empty:
    cols = st.columns(4)
    cols[0].metric("Payouts", master["Auszahlung Nr."].nunique())
    cols[1].metric("Auszahlung gesamt", euros(master["Erlös_Brutto"].sum()))
    cols[2].metric("Gruppe B · Saldo", euros(master.loc[master.Gruppe == "Gruppe B", "Erlös_Brutto"].sum()))
    cols[3].metric("Offene Zuordnungen", int(master["Prüfhinweis"].astype(bool).sum()))
    st.caption("Die Salden enthalten Erstattungen. Rechnungsentwürfe enthalten nur Bestellungen; Gutschriften werden getrennt geprüft.")

tab_a, tab_b, tab_check, tab_all = st.tabs(["Gruppe A · Direkt", "Gruppe B · Über Patrick", "Zuordnung & Gutschriften", "Alle Daten"])
with tab_a:
    st.subheader("Direkt-Partner")
    st.caption("PP · BA · MK · 001 — 99,5 % an den Partner, ohne Patrick als Zwischeninstanz.")
    if master.empty:
        st.info("Bitte Payouts und Bestellberichte hochladen.")
    else:
        summary = core.get_group_a_summary(master)
        st.dataframe(summary.style.format(precision=2), use_container_width=True, hide_index=True)
        download("Gesamtübersicht", summary, "Gruppe_A.xlsx", "a-summary")
        for partner, rows in master[master.Gruppe == "Gruppe A"].groupby("Partner"):
            partner_download(f"{partner} · Einzelabrechnung", rows, f"Partner_{partner}.xlsx", "a-" + partner)

with tab_b:
    st.subheader("Payout einzeln abrechnen")
    if master.empty:
        st.info("Noch keine Auszahlungen importiert.")
    else:
        payout_id = st.selectbox("Eine Auszahlung wählen", sorted(master["Auszahlung Nr."].unique()))
        block = master[master["Auszahlung Nr."] == payout_id]
        state = states[states.Auszahlung == payout_id].iloc[0]
        locked = bool(state.Sperre)
        if block["Prüfhinweis"].astype(bool).any() or "Prüfung" in state.Status:
            st.error(state.Status + " — Lexoffice-Erstellung gesperrt.")
            for reason in block.loc[block['Prüfhinweis'] != '', 'Prüfhinweis'].unique():
                st.error(reason)
        elif locked:
            st.success(state.Status) if state.Entwurf else st.warning("Versuch reserviert / Ergebnis unklar. Nicht erneut senden; Lexoffice manuell prüfen.")
        else:
            st.info(state.Status)
        st.dataframe(block[["Art", "Partner", "Angebotstitel", "SKU", "Bestellnummer", "Erlös_Brutto", "Prüfhinweis"]], use_container_width=True, hide_index=True)
        received = st.checkbox("Geldeingang für diesen Payout geprüft", key="received-" + payout_id)
        if st.button("Geldeingang speichern", disabled=not received or locked):
            try:
                core.confirm_received(payout_id)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        if st.button("Test-Payload vorbereiten"):
            try:
                payload = core.build_invoice_payload(master, payout_id, "00000000-0000-0000-0000-000000000000", received or state.Status == "Geld eingegangen")
                st.json(payload)
                st.download_button("Offline-JSON", json.dumps(payload, ensure_ascii=False, indent=2), f"Pruefung_{payout_id}.json")
            except ValueError as exc:
                st.error(str(exc))
        prior_checked = st.checkbox("In Lexoffice geprüft: Für diesen Payout besteht noch keine Rechnung (auch kein früherer Testentwurf).", key="prior-" + payout_id)
        confirmed = st.checkbox("Ich möchte genau einen echten, nicht finalisierten Entwurf anlegen.", key="send-" + payout_id)
        if st.button("Lexoffice-Entwurf erstellen", type="primary",
                     disabled=locked or not api_key or not prior_checked or not confirmed or state.Status != "Geld eingegangen"):
            try:
                invoice_id = core.create_invoice_draft(api_key, payout_id, prior_checked)
                st.success(f"Entwurf erstellt: {invoice_id}")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        if state.Entwurf:
            st.write("Gespeicherte Lexoffice-ID:", state.Entwurf)
        target = core.FOLLOWUP.get(state.Status)
        if target:
            checked = st.checkbox(f"Manuell geprüft, inklusive Erstattungen/Gutschriften: {target}", key="status-" + payout_id)
            if st.button("Status bestätigen", disabled=not checked):
                try:
                    core.advance_status(payout_id, target)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        st.divider()
        st.subheader("Partnerabrechnungen · 96,5 %")
        for partner, rows in block[block.Gruppe == "Gruppe B"].groupby("Partner"):
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", partner)
            partner_download(f"{partner} · Auszahlung {payout_id}", rows, f"{safe}_{payout_id}.xlsx", "b-" + payout_id + partner)
        partner_download("Evelyn-Gesamtübersicht (nur Export, kein Sammelupload)",
                         master[master.Gruppe == "Gruppe B"], "Gruppe_B.xlsx", "b-summary",
                         statement_type='group_b_evelyn')

with tab_check:
    st.subheader("Prüfung und Korrekturen")
    if master.empty:
        st.info("Noch keine Daten.")
    else:
        issues = master[master["Prüfhinweis"] != ""]
        if issues.empty:
            st.success("Alle bestellbezogenen Positionen eindeutig mit dem Bestellbericht verknüpft.")
        else:
            st.error(f"{len(issues)} Positionen: Zuordnung fehlt")
            st.dataframe(issues, use_container_width=True)
        st.subheader("Gutschriftenübersicht")
        refunds = core.get_refunds_summary(master)
        st.dataframe(refunds, use_container_width=True, hide_index=True)
        download("Gutschriften separat herunterladen", refunds, "Gutschriften.xlsx", "refunds")
        st.caption("Keine automatische Gutschrifterstellung. Voll-/Teilrückerstattungen anhand der Originalbelege prüfen.")
        st.subheader("Sonstige eBay-Gebühren — kein Partner")
        st.dataframe(master[master.Art == "Gebühr"], use_container_width=True, hide_index=True)

with tab_all:
    if master.empty:
        st.info("Noch keine Daten.")
    else:
        st.dataframe(master, use_container_width=True, hide_index=True)
        download("Alle Positionen", master, "Alle_Positionen.xlsx", "all")
        st.subheader("Persistenter Payout-Status")
        st.dataframe(states, use_container_width=True, hide_index=True)
        with core.ledger() as db:
            events = core.pd.read_sql_query("SELECT payout, at, event FROM audit ORDER BY id DESC", db)
        st.dataframe(events, use_container_width=True, hide_index=True)
