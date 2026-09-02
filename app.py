import streamlit as st
import pandas as pd
from core import (
    load_master_data, 
    get_group_b_summary, 
    get_group_a_summary, 
    get_refunds_summary, 
    export_to_excel
)

st.set_page_config(page_title="eBay Payout & Evelyn Billing Engine", layout="wide")

df_master = load_master_data()

# ---------------------------------------------------------
# SOLL-IST STATUSÜBERSICHT (KPI CARDS)
# ---------------------------------------------------------
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

# ---------------------------------------------------------
# 1. GRUPPE B – GESAMTABRECHNUNG FÜR EVELYN (ÜBER DICH)
# ---------------------------------------------------------
st.markdown("### 📊 1. Gruppe B – Gesamtabrechnung für Evelyn (Über DICH)")
st.info("ℹ️ **Verwendungszweck:** Diese Rechnung nutzt du für die Abrechnung gegenüber Evelyn. Sie enthält NUR die Umsätze aus Gruppe B, die über dich verteilt werden. Evelyn behält 0,5 % Provision.")

df_b = get_group_b_summary(df_master)
if not df_b.empty:
    # Formatiere Währung für Anzeige
    df_b_disp = df_b.copy()
    for col in ['eBay_Brutto_Gesamt', 'Evelyn_Provision_0_5', 'Auszahlung_von_Evelyn_an_Dich', 'Deine_Marge_3_0']:
        df_b_disp[col] = df_b_disp[col].apply(lambda x: f"{x:,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.'))

    st.dataframe(df_b_disp, use_container_width=True)

    excel_b = export_to_excel(df_b)
    st.download_button(
        label="📑 Gesamtabrechnung Gruppe B für Evelyn herunterladen (Excel)",
        data=excel_b,
        file_name="Gesamtabrechnung_Gruppe_B_Evelyn.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary"
    )

st.markdown("---")

# ---------------------------------------------------------
# 2. GRUPPE A – DIREKTABRECHNUNGEN FÜR EVELYN
# ---------------------------------------------------------
st.markdown("### 🏷️ 2. Gruppe A – Direktabrechnungen für Evelyn (PP, BA, MK, 001)")
st.info("ℹ️ **Verwendungszweck:** Diese Partner rechnen mit 0,5 % Provision direkt mit Evelyn ab (laufen nicht über deine Marge).")

df_a = get_group_a_summary(df_master)
if not df_a.empty:
    df_a_disp = df_a.copy()
    for col in ['eBay_Brutto_Gesamt', 'Evelyn_Provision_0_5', 'Direkt_Auszahlung_Evelyn']:
        df_a_disp[col] = df_a_disp[col].apply(lambda x: f"{x:,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.'))

    st.dataframe(df_a_disp, use_container_width=True)

    excel_a = export_to_excel(df_a)
    st.download_button(
        label="📥 Übersicht Gruppe A für Evelyn herunterladen (Excel)",
        data=excel_a,
        file_name="Uebersicht_Gruppe_A_Evelyn.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

st.markdown("---")

# ---------------------------------------------------------
# 3. GUTSCHRIFTEN & ERSTATTUNGEN (LEXOFFICE)
# ---------------------------------------------------------
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
