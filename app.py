import streamlit as st
import pandas as pd
import io

st.set_page_config(page_title="eBay Payout & Provision Tool", layout="wide")

st.title("📦 eBay Payout & SKU-Abrechnungs Tool")
st.write("Lade deinen eBay Auszahlungsbericht (CSV) hoch. Das Tool filtert automatisch nach SKU, verrechnet Retouren/Rückerstattungen und zieht 3,5 % Provision ab.")

uploaded_file = st.file_uploader("eBay CSV-Bericht hochladen", type=["csv"])

PROVISION_RATE = 0.035  # 3.5%

def parse_german_float(val):
    if pd.isna(val) or val == '--' or str(val).strip() == '':
        return 0.0
    return float(str(val).replace('.', '').replace(',', '.'))

if uploaded_file is not None:
    try:
        content = uploaded_file.getvalue().decode('utf-8', errors='ignore')
        lines = content.splitlines()
        
        header_idx = 0
        for i, line in enumerate(lines):
            if "Datum der Transaktionserstellung" in line:
                header_idx = i
                break
                
        df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])), sep=';')
        
        df['Auszahlung_Netto_eBay'] = df['Betrag abzügl. Kosten'].apply(parse_german_float)
        df['SKU'] = df['Bestandseinheit'].fillna('OHNE_SKU').astype(str).str.strip()
        
        df['Provision_3_5_EUR'] = (df['Auszahlung_Netto_eBay'] * PROVISION_RATE).round(2)
        df['Auszahlung_Partner_EUR'] = (df['Auszahlung_Netto_eBay'] - df['Provision_3_5_EUR']).round(2)
        
        st.success(f"Datei erfolgreich geladen: {len(df)} Transaktionen gefunden.")
        
        st.subheader("📊 Gesamtübersicht nach SKU")
        
        summary = df.groupby('SKU').agg(
            Anzahl_Transaktionen=('Typ', 'count'),
            eBay_Auszahlung_Gesamt=('Auszahlung_Netto_eBay', 'sum'),
            Provision_3_5_Gesamt=('Provision_3_5_EUR', 'sum'),
            Partner_Auszahlung_Gesamt=('Auszahlung_Partner_EUR', 'sum')
        ).reset_index()
        
        st.dataframe(summary, use_container_width=True)
        
        st.subheader("🔍 Detailansicht & Export pro Partner/SKU")
        skus = df['SKU'].unique().tolist()
        selected_sku = st.selectbox("SKU / Partner auswählen", skus)
        
        filtered_df = df[df['SKU'] == selected_sku]
        st.write(f"**Transaktionen für SKU `{selected_sku}`:**")
        st.dataframe(filtered_df[['Datum der Transaktionserstellung', 'Typ', 'Bestellnummer', 'Angebotstitel', 'Auszahlung_Netto_eBay', 'Provision_3_5_EUR', 'Auszahlung_Partner_EUR']], use_container_width=True)
        
        csv_data = filtered_df.to_csv(index=False, sep=';', encoding='utf-8-sig')
        st.download_button(
            label=f"📥 Download CSV für SKU {selected_sku}",
            data=csv_data,
            file_name=f"Abrechnung_SKU_{selected_sku}.csv",
            mime="text/csv"
        )
        
    except Exception as e:
        st.error(f"Fehler beim Verarbeiten der CSV: {e}")
