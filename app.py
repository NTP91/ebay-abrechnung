import os
import pandas as pd
import streamlit as st
from importer import import_payout_files

# Ordner-Konfiguration für Streamlit
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

MASTER_CSV_PATH = os.path.join(os.path.dirname(__file__), 'Master_Payouts.csv')

# Streamlit UI Konfiguration
st.set_page_config(page_title="eBay Payout Import System", layout="centered")

st.title("eBay Payout Importer")
st.write("Wähle eine oder mehrere eBay-Payout CSV-Dateien aus:")

# Datei-Uploader
uploaded_files = st.file_uploader(
    "CSV-Dateien auswählen", 
    type=["csv"], 
    accept_multiple_files=True
)

if uploaded_files:
    if st.button("Dateien hochladen & verarbeiten"):
        # Uploads im Ordner 'uploads' speichern
        for uploaded_file in uploaded_files:
            filepath = os.path.join(UPLOAD_FOLDER, uploaded_file.name)
            with open(filepath, "wb") as f:
                f.write(uploaded_file.getbuffer())

        # Automatische Verarbeitung & Dublettenprüfung starten
        import_payout_files(
            input_directory=UPLOAD_FOLDER, 
            output_master_csv=MASTER_CSV_PATH
        )

        st.success("Dateien erfolgreich verarbeitet! Dubletten wurden automatisch gefiltert.")

# Optional: Master-Tabelle anzeigen, falls vorhanden
if os.path.exists(MASTER_CSV_PATH):
    st.markdown("---")
    st.subheader("Aktuelle Master-Datei")
    try:
        master_df = pd.read_csv(MASTER_CSV_PATH, sep=';', dtype=str)
        st.dataframe(master_df)
    except Exception:
        st.info("Master-Datei vorhanden, konnte aber noch nicht geladen werden.")
