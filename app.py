import streamlit as st
import os
import pandas as pd
from core import (
    load_master_data, 
    get_group_b_summary, 
    get_group_a_summary, 
    get_refunds_summary, 
    export_to_excel,
    ORDERS_DB_PATH,
    PAYOUTS_DB_PATH
)

st.set_page_config(page_title="eBay Payout & Evelyn Billing Engine", layout="wide")

# ---------------------------------------------------------
# SIDEBAR: FILE UPLOADER & DB MANAGEMENT
# ---------------------------------------------------------
st.sidebar.header("📁 Datei-Upload & Daten")

uploaded_payouts = st.sidebar.file_uploader("1. Payout-Dateien hochladen (CSV)", type=['csv'], accept_multiple_files=True)
uploaded_orders = st.sidebar.file_uploader("2. Bestellberichte hochladen (CSV/XLSX)", type=['csv', 'xlsx', 'xls'], accept_multiple_files=True)

if uploaded_payouts:
    payout_frames = []
    for f in uploaded_payouts:
        try:
            try:
                df_p = pd.read_csv(f, sep=';', dtype=str)
                if len(df_p.columns) <= 1:
                    f.seek(0)
                    df_p = pd.read_csv(f, sep=',', dtype=str)
            except Exception:
                f.seek(0)
                df_p = pd.read_csv(f, sep=None, engine='python', dtype=str)
            payout_frames.append(df_p)
        except Exception:
            pass
    if payout_frames:
        merged_p = pd.concat(payout_frames, ignore_index=True).drop_duplicates()
        merged_p.to_csv(PAYOUTS_DB_PATH, sep=';', index=False, encoding='utf-8-sig')
        st.sidebar.success("Payouts gespeichert!")

if uploaded_orders:
    order_frames = []
    for f in uploaded_orders:
        try:
            if f.name.endswith(('.xlsx', '.xls')):
                df_o = pd.read_excel(f, dtype=str)
            else:
                try:
                    df_o = pd.read_csv(f, sep=';', dtype=str)
                    if len(df_o.columns) <= 1:
                        f.seek(0)
                        df_o = pd.read_csv(f, sep=',', dtype=str)
                except Exception:
                    f.seek(0)
                    df_o = pd.read_csv(f, sep=None, engine='python', dtype=str)
            order_frames.append(df_o)
        except Exception:
            pass
    if order_frames:
        merged_o = pd.concat(order_frames, ignore_index=True).drop_duplicates()
        merged_o.to_csv(ORDERS_DB_PATH, sep=';', index=False, encoding='utf-8-sig')
        st.sidebar.success("Bestellberichte gespeichert!")

st.sidebar.markdown("---")
if st.sidebar.button("🗑️ Datenbank komplett leeren"):
    if os.path.exists(ORDERS_DB_PATH): os.remove(ORDERS_DB_PATH)
    if os.path.exists(PAYOUTS_DB_PATH): os.remove(PAYOUTS_DB_PATH)
    st.sidebar.success("Datenbank geleert!")
    st.rerun()

# ---------------------------------------------------------
# HAUPTANSICHT
# ---------------------------------------------------------
df_master = load_master_data()

st.markdown("### ⚖️ Soll-Ist Statusübersicht")

if not df_master.empty:
    total_pos = len(df_master)
    ausbezahlt_pos = len(df_master[df_master['Status'] == 'Ausbezahlt'])
    offen_pos = total_pos - ausbezahlt_pos
    erloes_brutto = df_master['Erlös_Brutto'].sum()
    auszahlung_partner = erloes_brutto * 0.965

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
