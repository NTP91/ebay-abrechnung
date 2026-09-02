import streamlit as st
import os
import pandas as pd
import json
from datetime import datetime
from core import (
    load_master_data, 
    get_group_b_summary, 
    get_group_a_summary, 
    get_refunds_summary, 
    export_to_excel,
    ORDERS_DB_PATH,
    PAYOUTS_DB_PATH
)
from core import read_report, import_reports, build_invoice_payload

st.set_page_config(page_title="eBay Payout & Evelyn Billing Engine", layout="wide")

# ---------------------------------------------------------
# SIDEBAR: FILE UPLOADER & DB MANAGEMENT
# ---------------------------------------------------------
st.sidebar.header("📁 Datei-Upload & Daten")

uploaded_payouts = st.sidebar.file_uploader("1. Payout-Dateien hochladen (CSV)", type=['csv'], accept_multiple_files=True)
uploaded_orders = st.sidebar.file_uploader("2. Bestellberichte hochladen (CSV/XLSX)", type=['csv', 'xlsx'], accept_multiple_files=True)

if st.sidebar.button("Dateien sicher importieren", disabled=not (uploaded_payouts or uploaded_orders)):
    try:
        # Parse every file first. A malformed input cannot silently disappear.
        payout_frames = [read_report(file, 'payout') for file in uploaded_payouts]
        order_frames = [read_report(file, 'orders') for file in uploaded_orders]
        added_orders = import_reports(order_frames, ORDERS_DB_PATH, 'orders')
        added_payouts = import_reports(payout_frames, PAYOUTS_DB_PATH, 'payout')
        st.sidebar.success(f"Importiert: {added_payouts} neue Payout-Zeilen, {added_orders} neue Bestellpositionen.")
    except Exception as exc:
        st.sidebar.error(f"Import nicht vollständig: {exc}")

st.sidebar.markdown("---")
st.sidebar.warning("Lokale CSV-Speicherung benötigt einen dauerhaften Datenträger. Vor einem Cloud-Neustart Daten sichern.")
for label, path in [('Bestellungen', ORDERS_DB_PATH), ('Payouts', PAYOUTS_DB_PATH)]:
    if os.path.exists(path):
        with open(path, 'rb') as saved:
            st.sidebar.download_button(f"{label} sichern", saved.read(), file_name=path)

confirm_archive = st.sidebar.checkbox("Ich möchte beide Masterdateien archivieren und leer beginnen.")
if st.sidebar.button("Datenbestand archivieren", disabled=not confirm_archive):
    suffix = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    for path in (ORDERS_DB_PATH, PAYOUTS_DB_PATH):
        if os.path.exists(path):
            os.replace(path, f"{path}.{suffix}.bak")
    st.sidebar.success("Daten wurden als .bak archiviert, nicht gelöscht.")
    st.rerun()


# ---------------------------------------------------------
# HAUPTANSICHT
# ---------------------------------------------------------
try:
    df_master = load_master_data()
except Exception as exc:
    st.error(f'Gespeicherte Daten können nicht sicher verarbeitet werden: {exc}')
    st.stop()

st.warning('Reparaturstand zur Prüfung: Kein produktiver Lexoffice-Upload. Original-Testdateien und Rechnungsbeispiel fehlen noch.')
if not df_master.empty:
    with st.expander('Zuordnung, Gebühren und Payout-Prüfung', expanded=True):
        st.dataframe(df_master, use_container_width=True)
        fees = df_master[df_master['Art'] == 'Gebühr']
        issues = df_master[df_master['Prüfhinweis'] != '']
        st.write(f'{len(issues)} offene Zuordnungen; {len(fees)} separate eBay-Gebühren.')
        st.download_button('Prüfübersicht herunterladen', export_to_excel(df_master), 'Payout_Pruefung.xlsx')
    with st.expander('Lexoffice-Positionen offline prüfen (kein API-Aufruf)'):
        payout_id = st.selectbox('Eine Auszahlung wählen', sorted(df_master['Auszahlung Nr.'].unique()))
        received = st.checkbox('Für diesen Test ist der tatsächliche Geldeingang bestätigt')
        if st.button('Test-Payload vorbereiten'):
            try:
                payload = build_invoice_payload(df_master, payout_id, '00000000-0000-0000-0000-000000000000', received)
                st.json(payload)
                st.download_button('Test-Payload herunterladen', json.dumps(payload, ensure_ascii=False, indent=2), f'Pruefung_{payout_id}.json')
            except ValueError as exc:
                st.error(str(exc))

st.markdown("### ⚖️ Soll-Ist Statusübersicht")

if not df_master.empty:
    total_pos = len(df_master)
    ausbezahlt_pos = 0  # Receipt cannot be inferred from a transaction status.
    offen_pos = total_pos - ausbezahlt_pos
    erloes_brutto = df_master['Erlös_Brutto'].sum()
    auszahlung_partner = df_master.apply(lambda row: row['Erlös_Brutto'] * (0.995 if row['Gruppe'] == 'Gruppe A' else 0.965 if row['Gruppe'] == 'Gruppe B' else 0), axis=1).sum()

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Rechnung Positionen", f"{total_pos}")
    col2.metric("✅ Ausbezahlt", f"{ausbezahlt_pos} Pos.")
    col3.metric("⏳ Noch Offen", f"{offen_pos} Pos.")
    col4.metric("💰 eBay Erlös Brutto", f"{erloes_brutto:,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.'))
    col5.metric("🤝 Auszahlung Partner Brutto", f"{auszahlung_partner:,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.'))

    with st.expander(f"🔴 Liste der {offen_pos} noch nicht ausgezahlten Positionen anzeigen"):
        st.dataframe(df_master[df_master['Status'] != 'Ausbezahlt'], use_container_width=True)

st.markdown("---")

st.markdown("### 📊 1. Gruppe B – Gesamtabrechnung für Evelyn (Über DICH)")
st.info("ℹ️ **Verwendungszweck:** Diese Rechnung nutzt du für die Abrechnung gegenüber Evelyn. Sie enthält NUR die Umsätze aus Gruppe B, die über dich verteilt werden. Evelyn behält 0,5 % Provision.")

df_b = get_group_b_summary(df_master)
if not df_b.empty:
    df_b_disp = df_b.copy()
    for col in ['eBay_Brutto_Gesamt', 'Evelyn_Provision_0_5', 'Auszahlung_von_Evelyn_an_Dich', 'Deine_Marge_3_0']:
        df_b_disp[col] = df_b_disp[col].apply(lambda x: f"{x:,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.'))

    st.dataframe(df_b_disp, use_container_width=True)

    try:
        excel_b = export_to_excel(df_b)
        st.download_button(
            label="📑 Gesamtabrechnung Gruppe B für Evelyn herunterladen (Excel)",
            data=excel_b,
            file_name="Gesamtabrechnung_Gruppe_B_Evelyn.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary"
        )
    except Exception as e:
        st.error(f"Excel-Export Fehler: {e}")

st.markdown("---")

st.markdown("### 🏷️ 2. Gruppe A – Direktabrechnungen für Evelyn (PP, BA, MK, 001)")
st.info("ℹ️ **Verwendungszweck:** Diese Partner rechnen mit 0,5 % Provision direkt mit Evelyn ab (laufen nicht über deine Marge).")

df_a = get_group_a_summary(df_master)
if not df_a.empty:
    df_a_disp = df_a.copy()
    for col in ['eBay_Brutto_Gesamt', 'Evelyn_Provision_0_5', 'Direkt_Auszahlung_Evelyn']:
        df_a_disp[col] = df_a_disp[col].apply(lambda x: f"{x:,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.'))

    st.dataframe(df_a_disp, use_container_width=True)

    try:
        excel_a = export_to_excel(df_a)
        st.download_button(
            label="📥 Übersicht Gruppe A für Evelyn herunterladen (Excel)",
            data=excel_a,
            file_name="Uebersicht_Gruppe_A_Evelyn.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        st.error(f"Excel-Export Fehler: {e}")

st.markdown("---")

st.markdown("### 🔻 2. Gutschriften & Erstattungen (Für Lexoffice-Gutschriften)")
st.info("ℹ️ **Verwendungszweck:** Hier sind alle negativen Beträge (z. B. Retouren oder Versandgutschriften wie bei 001) aufgeführt. Nutze diese Übersicht, um in Lexoffice saubere Einzel-Gutschriften zu erstellen.")

df_ref = get_refunds_summary(df_master)
if not df_ref.empty:
    df_ref_disp = df_ref.copy()
    for col in ['Gutschrift_Brutto', 'Provision', 'Gutschrift_Netto_Auszahlung']:
        df_ref_disp[col] = df_ref_disp[col].apply(lambda x: f"{x:,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.'))

    st.dataframe(df_ref_disp, use_container_width=True)
else:
    st.write("Keine Gutschriften/Erstattungen vorhanden.")
