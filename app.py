import streamlit as st
import pandas as pd
import io

st.set_page_config(page_title="eBay Payout & Provision Tool", layout="wide")

st.title("📦 eBay Payout & SKU-Abrechnungs Tool")
st.write("Lade deinen eBay Auszahlungsbericht (CSV) hoch. Das Tool filtert automatisch nach SKU, verrechnet Retouren/Rückerstattungen und wendet die passenden Provisionssätze an (0,5 % für BA, MK, PP, 001 / 3,5 % für den Rest).")

uploaded_file = st.file_uploader("eBay CSV-Bericht hochladen", type=["csv"])

def parse_german_float(val):
    if pd.isna(val) or val == '--' or str(val).strip() == '':
        return 0.0
    return float(str(val).replace('.', '').replace(',', '.'))

def get_commission_rate(sku):
    if pd.isna(sku):
        return 0.035
    sku_clean = str(sku).strip().upper()
    prefix = sku_clean.split('/')[0].strip()
    
    # 0,5 % Regel für Spezial-SKUs
    if prefix in ['BA', 'MK', 'PP', '001'] or prefix.startswith('001'):
        return 0.005
    
    # 3,5 % Standard für alle übrigen SKUs
    return 0.035

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
        
        # Flexibler Provisionssatz
        df['Provisionssatz'] = df['SKU'].apply(get_commission_rate)
        df['Provision_EUR'] = (df['Auszahlung_Netto_eBay'] * df['Provisionssatz']).round(2)
        df['Auszahlung_Partner_EUR'] = (df['Auszahlung_Netto_eBay'] - df['Provision_EUR']).round(2)
        
        st.success(f"Datei erfolgreich geladen: {len(df)} Transaktionen gefunden.")
        
        # Gesamtübersicht
        st.subheader("📊 Gesamtübersicht nach SKU")
        df['SKU_Prefix'] = df['SKU'].apply(lambda x: str(x).split('/')[0].strip().upper() if pd.notna(x) else 'FEHLT')
        
        summary = df.groupby('SKU_Prefix').agg(
            Anzahl_Transaktionen=('SKU', 'count'),
            eBay_Auszahlung_Gesamt=('Auszahlung_Netto_eBay', 'sum'),
            Provision_Gesamt=('Provision_EUR', 'sum'),
            Partner_Auszahlung_Gesamt=('Auszahlung_Partner_EUR', 'sum')
        ).reset_index()
        
        st.dataframe(summary.style.format({
            'eBay_Auszahlung_Gesamt': '{:.2f} €',
            'Provision_Gesamt': '{:.2f} €',
            'Partner_Auszahlung_Gesamt': '{:.2f} €'
        }))
        
        # Detailansicht
        st.subheader("🔍 Detailansicht & Export pro Partner/SKU")
        selected_sku = st.selectbox("SKU / Partner auswählen", summary['SKU_Prefix'].unique())
        
        filtered_df = df[df['SKU_Prefix'] == selected_sku]
        st.dataframe(filtered_df[['Datum der Transaktionserstellung', 'Typ', 'Bestellnummer', 'Angebotstitel', 'Auszahlung_Netto_eBay', 'Provision_EUR', 'Auszahlung_Partner_EUR']])
        
    except Exception as e:
        st.error(f"Fehler beim Verarbeiten der Datei: {e}")
