import os
import pandas as pd
import streamlit as st
from importer import import_payout_files

# --- KONFIGURATION & PFADE ---
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
MASTER_CSV_PATH = os.path.join(os.path.dirname(__file__), 'Master_Payouts.csv')

st.set_page_config(page_title="eBay Abrechnung & Payout Manager", layout="wide")

# --- SEITENLEISTE (SIDEBAR) / UPLOAD ---
st.sidebar.title("📁 Datei-Verwaltung")
st.sidebar.markdown("---")

uploaded_files = st.sidebar.file_uploader(
    "Neue Payout CSVs hochladen", 
    type=["csv"], 
    accept_multiple_files=True
)

if uploaded_files:
    if st.sidebar.button("Dateien verarbeiten & speichern"):
        for uploaded_file in uploaded_files:
            filepath = os.path.join(UPLOAD_FOLDER, uploaded_file.name)
            with open(filepath, "wb") as f:
                f.write(uploaded_file.getbuffer())

        import_payout_files(
            input_directory=UPLOAD_FOLDER, 
            output_master_csv=MASTER_CSV_PATH
        )
        st.sidebar.success("Erfolgreich verarbeitet!")
        st.rerun()

# Gespeicherte Dateien in der Seitenleiste auflisten
st.sidebar.markdown("### 📂 Hochgeladene Dateien")
if os.path.exists(UPLOAD_FOLDER):
    files_in_dir = [f for f in os.listdir(UPLOAD_FOLDER) if f.endswith('.csv')]
    if files_in_dir:
        for f in sorted(files_in_dir):
            st.sidebar.text(f"📄 {f}")
    else:
        st.sidebar.caption("Noch keine Einzeldateien vorhanden.")

# --- HAUPTANSICHT / DASHBOARD ---
st.title("📊 eBay Payout & Kunden-Dashboard")

if os.path.exists(MASTER_CSV_PATH):
    try:
        master_df = pd.read_csv(MASTER_CSV_PATH, sep=';', dtype=str)
        
        if not master_df.empty:
            # TABS FÜR ÜBERSICHTEN
            tab_overview, tab_customers, tab_raw = st.tabs([
                "📈 Gesamtsicht", 
                "👤 Kunden & Personen", 
                "📋 Rohdaten (Master-CSV)"
            ])

            # TAB 1: Gesamtsicht & Kennzahlen
            with tab_overview:
                st.subheader("Übersicht")
                col1, col2, col3 = st.columns(3)
                col1.metric("Gesamtanzahl Zeilen", len(master_df))
                
                if 'Auszahlung Nr.' in master_df.columns:
                    unique_payouts = master_df['Auszahlung Nr.'].nunique()
                    col2.metric("Anzahl Auszahlungen", unique_payouts)
                
                if 'Nutzersuche / Käufername' in master_df.columns or 'Nutzername des Käufers' in master_df.columns:
                    buyer_col = 'Nutzername des Käufers' if 'Nutzername des Käufers' in master_df.columns else 'Nutzersuche / Käufername'
                    col3.metric("Eindeutige Kunden", master_df[buyer_col].nunique())

                st.markdown("---")
                st.write("### Letchte Transaktionen")
                st.dataframe(master_df.head(50), use_container_width=True)

            # TAB 2: Kunden & Personen
            with tab_customers:
                st.subheader("Kundenübersicht")
                buyer_col = None
                for col_name in ['Nutzername des Käufers', 'Nutzersuche / Käufername', 'Käufer Name']:
                    if col_name in master_df.columns:
                        buyer_col = col_name
                        break

                if buyer_col:
                    search_term = st.text_input("🔍 Nach Kunde/Person suchen:")
                    if search_term:
                        filtered_df = master_df[master_df[buyer_col].str.contains(search_term, case=False, na=False)]
                        st.write(f"Gefundene Einträge für **'{search_term}'**: {len(filtered_df)}")
                        st.dataframe(filtered_df, use_container_width=True)
                    else:
                        st.write("**Top Kunden nach Transaktionen:**")
                        customer_counts = master_df[buyer_col].value_counts().reset_index()
                        customer_counts.columns = ['Kunde', 'Anzahl Transaktionen']
                        st.dataframe(customer_counts, use_container_width=True)
                else:
                    st.info("Spalte für Kundennamen in den Daten nicht direkt erkannt.")

            # TAB 3: Rohdaten & Download
            with tab_raw:
                st.subheader("Gesamte Master_Payouts.csv")
                st.dataframe(master_df, use_container_width=True)
                
                csv_bytes = master_df.to_csv(sep=';', index=False, encoding='utf-8-sig').encode('utf-8-sig')
                st.download_button(
                    label="📥 Master_Payouts.csv herunterladen",
                    data=csv_bytes,
                    file_name="Master_Payouts.csv",
                    mime="text/csv"
                )
        else:
            st.info("Die Master-Datei ist noch leer. Bitte lade links CSV-Dateien hoch.")

    except Exception as e:
        st.error(f"Fehler beim Laden der Master-Datei: {e}")
else:
    st.info("👋 Willkommen! Noch keine Daten vorhanden. Lade links in der Seitenleiste deine ersten Payout-CSVs hoch.")
