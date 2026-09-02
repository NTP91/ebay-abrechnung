import streamlit as st
import pandas as pd

st.set_page_config(page_title="eBay-Verrechnung Lexoffice", layout="wide")
st.title("📋 eBay-Auszahlungsverrechnung & Lexoffice Upload")

# --- SIDEBAR ---
st.sidebar.header("Datei-Upload")
uploaded_file = st.sidebar.file_uploader("eBay CSV hochladen", type=["csv", "xlsx"])

# --- UI-TABS IMMER ANZEIGEN ---
tab1, tab2, tab3, tab4 = st.tabs([
    "Tab 1: Gruppe A (Direkt)", 
    "Tab 2: Gruppe B (Über Dich)", 
    "Tab 3: Ohne Zuordnung", 
    "Tab 4: Alle Daten"
])

if uploaded_file is None:
    # Hinweistext in den Tabs anzeigen, wenn keine Datei da ist
    with tab1:
        st.info("Bitte lade eine CSV-Datei in der Seitenleiste hoch, um die Daten für Gruppe A zu sehen.")
    with tab2:
        st.info("Bitte lade eine CSV-Datei in der Seitenleiste hoch, um die Daten für Gruppe B zu sehen.")
    with tab3:
        st.info("Bitte lade eine CSV-Datei hoch.")
    with tab4:
        st.info("Bitte lade eine CSV-Datei hoch.")
else:
    # --- AB HIER DEIN VERARBEITUNGS-CODE ---
    # CSV/Excel einlesen, Gruppierung anwenden und Tabs befüllen
    pass
