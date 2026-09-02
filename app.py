import os
import pandas as pd
import streamlit as st
from importer import import_payout_files

# --- PFAD-KONFIGURATION ---
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
MASTER_CSV_PATH = os.path.join(os.path.dirname(__file__), 'Master_Payouts.csv')

st.set_page_config(page_title="eBay Abrechnung & Dashboard", layout="wide")

# --- SEITENLEISTE (DATENMANAGER) ---
st.sidebar.title("📁 Dateimanager")

uploaded_files = st.sidebar.file_uploader(
    "Payout CSVs hochladen", 
    type=["csv"], 
    accept_multiple_files=True
)

if uploaded_files:
    if st.sidebar.button("Speichern & Verarbeiten"):
        for uploaded_file in uploaded_files:
            filepath = os.path.join(UPLOAD_FOLDER, uploaded_file.name)
            with open(filepath, "wb") as f:
                f.write(uploaded_file.getbuffer())

        # Payouts entdoppeln & in Datenbank speichern
        import_payout_files(
            input_directory=UPLOAD_FOLDER, 
            output_master_csv=MASTER_CSV_PATH
        )
        st.sidebar.success("Payouts gespeichert & aktualisiert!")
        st.rerun()

st.sidebar.markdown("---")
st.sidebar.markdown("### 📄 Gespeicherte Payout-Dateien")
if os.path.exists(UPLOAD_FOLDER):
    files = [f for f in os.listdir(UPLOAD_FOLDER) if f.endswith('.csv')]
    if files:
        for f in sorted(files):
            st.sidebar.text(f"• {f}")
    else:
        st.sidebar.caption("Keine Dateien im Speicher.")

# --- HAUPTDASHBOARD ---
st.title("📊 eBay Abrechnung & Provisionsberechnung")

# Master-Datenbank laden
payout_df = pd.DataFrame()
if os.path.exists(MASTER_CSV_PATH):
    try:
        payout_df = pd.read_csv(MASTER_CSV_PATH, sep=';', dtype=str)
    except Exception:
        pass

# DASHBOARD TABS
tab_prov, tab_cust, tab_data = st.tabs([
    "💰 Verkäuferprovisionen", 
    "👤 Kunden & Personen", 
    "🗃️ Payout-Datenbank"
])

# 1. TAB: VERKÄUFERPROVISIONEN & RECHNUNGSDATEN
with tab_prov:
    st.subheader("Verkäuferprovisions-Berechnung")
    if not payout_df.empty:
        # Sicherstellen, dass Betrags-Spalten numerisch sind
        val_col = None
        for col in ['Betrag', 'Nettobetrag', 'Gesamtbetrag']:
            if col in payout_df.columns:
                val_col = col
                break
        
        if val_col:
            payout_df_calc = payout_df.copy()
            payout_df_calc['Betrag_Num'] = payout_df_calc[val_col].str.replace(',', '.').astype(float)
            
            total_sum = payout_df_calc['Betrag_Num'].sum()
            col1, col2 = st.columns(2)
            col1.metric("Gesamter Umsatz / Payouts", f"{total_sum:,.2f} €")
            
            # Beispielhafte Provisionsberechnung (anpassbar)
            prov_rate = st.slider("Provisionssatz (%)", min_value=0.0, max_value=30.0, value=10.0, step=0.5)
            provision = total_sum * (prov_rate / 100.0)
            col2.metric(f"Berechnete Provision ({prov_rate}%)", f"{provision:,.2f} €")
            
            st.markdown("---")
            st.write("### Rechnungsrelevante Übersichten")
            st.dataframe(payout_df_calc, use_container_width=True)
        else:
            st.dataframe(payout_df, use_container_width=True)
    else:
        st.info("Keine Payout-Daten in der Datenbank vorhanden. Bitte lade links Dateien hoch.")

# 2. TAB: KUNDEN UND PERSONENOVERVIEW
with tab_cust:
    st.subheader("Personen- & Kundenübersicht")
    if not payout_df.empty:
        buyer_col = None
        for col in ['Nutzername des Käufers', 'Nutzersuche / Käufername', 'Name des Käufers', 'Käufer Name']:
            if col in payout_df.columns:
                buyer_col = col
                break
                
        if buyer_col:
            search = st.text_input("🔍 Kunde oder Person suchen:")
            if search:
                res = payout_df[payout_df[buyer_col].str.contains(search, case=False, na=False)]
                st.write(f"Gefundene Einträge: {len(res)}")
                st.dataframe(res, use_container_width=True)
            else:
                st.write("### Alle erfassten Personen")
                persons = payout_df[[buyer_col]].drop_duplicates().reset_index(drop=True)
                st.dataframe(persons, use_container_width=True)
        else:
            st.warning("Keine Käufer-/Personenspalte in den Daten gefunden.")
    else:
        st.info("Keine Kundendaten vorhanden.")

# 3. TAB: GESAMTE PAYOUT-DATENBANK
with tab_data:
    st.subheader("Konsolidierte Master_Payouts.csv")
    if not payout_df.empty:
        st.dataframe(payout_df, use_container_width=True)
        csv_bytes = payout_df.to_csv(sep=';', index=False, encoding='utf-8-sig').encode('utf-8-sig')
        st.download_button(
            label="📥 Master_Payouts.csv herunterladen",
            data=csv_bytes,
            file_name="Master_Payouts.csv",
            mime="text/csv"
        )
    else:
        st.info("Datenbank ist noch leer.")
