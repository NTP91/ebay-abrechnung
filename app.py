import os
import pandas as pd
import streamlit as st
from importer import import_payout_files
from core import save_and_merge_order_reports, build_transaction_overview

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

MASTER_CSV_PATH = os.path.join(os.path.dirname(__file__), 'Master_Payouts.csv')

st.set_page_config(page_title="eBay Abrechnung", layout="wide")

# --- SIDEBAR: EINSTELLUNGEN & UPLOAD ---
st.sidebar.title("⚙️ Einstellungen & Datenbank")

st.sidebar.markdown("### 📌 Bestellberichte importieren")
st.sidebar.caption("Wähle hier beide Dateien (CSV & XLSX) gleichzeitig aus:")

uploaded_orders = st.sidebar.file_uploader(
    "Bestellberichte (CSV & XLSX)", 
    type=["csv", "xlsx", "xls"], 
    accept_multiple_files=True,
    key="order_uploader"
)

if uploaded_orders:
    if st.sidebar.button("Bestellberichte verarbeiten"):
        save_and_merge_order_reports(uploaded_orders, upload_folder=UPLOAD_FOLDER)
        st.sidebar.success("Bestellberichte verarbeitet!")
        st.rerun()

st.sidebar.markdown("---")
st.sidebar.markdown("### 📌 Payout CSVs importieren")

uploaded_payouts = st.sidebar.file_uploader(
    "Payout CSVs hochladen", 
    type=["csv"], 
    accept_multiple_files=True,
    key="payout_uploader"
)

logs = []
if uploaded_payouts:
    if st.sidebar.button("Payouts speichern & entdoppeln"):
        for file in uploaded_payouts:
            filepath = os.path.join(UPLOAD_FOLDER, file.name)
            with open(filepath, "wb") as f:
                f.write(file.getbuffer())

        _, logs = import_payout_files(
            input_directory=UPLOAD_FOLDER, 
            output_master_csv=MASTER_CSV_PATH
        )
        st.rerun()

# DB-Status Anzeigen
st.sidebar.markdown("---")
st.sidebar.markdown("### 📦 DB-Status")

orders_df_count = 0
if os.path.exists("Master_Orders.csv"):
    try:
        orders_df_count = len(pd.read_csv("Master_Orders.csv", sep=';', dtype=str))
    except Exception:
        pass

payout_count = 0
if os.path.exists(MASTER_CSV_PATH):
    try:
        payout_count = len(pd.read_csv(MASTER_CSV_PATH, sep=';', dtype=str))
    except Exception:
        pass

st.sidebar.text(f"• Artikel in DB: {orders_df_count}")
st.sidebar.text(f"• Gesp. Payout-Zeilen: {payout_count}")

if st.sidebar.button("🗑️ Datenbank komplett leeren"):
    for f in [MASTER_CSV_PATH, "Master_Orders.csv"]:
        if os.path.exists(f):
            os.remove(f)
    st.sidebar.warning("Datenbank zurückgesetzt!")
    st.rerun()


# --- HAUPTANSICHT ---

# Hinweis-Boxen bei Dubletten-Uploads anzeigen
if logs:
    for log in logs:
        if "Übersprungen" in log:
            st.warning(log)
        else:
            st.info(log)

st.title("📊 Übersicht aller Transaktionen (Dauerhaft in DB)")

# Daten für die Haupttabelle generieren
df_display, order_db_size, total_netto = build_transaction_overview(
    master_payout_path=MASTER_CSV_PATH,
    master_orders_path="Master_Orders.csv"
)

st.write(f"**Anzahl Gesamt:** {len(df_display)} Positionen | **Gesamtsumme Netto:** {total_netto:,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.'))

if not df_display.empty:
    st.dataframe(df_display, use_container_width=True)
else:
    st.info("Noch keine Transaktionen vorhanden. Bitte importiere Payouts und Bestellberichte über die Seitenleiste.")
