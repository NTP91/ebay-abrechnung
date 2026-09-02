import streamlit as st
import pandas as pd
import io

st.set_page_config(page_title="eBay Payout & Provision Tool", layout="wide")

st.title("📦 eBay Payout & SKU-Abrechnungs Tool")
st.write("Lade deinen eBay Auszahlungsbericht (CSV) sowie optional deine Soll-Rechnung (Excel/CSV) hoch.")

# 1. Zwei Upload-Felder nebeneinander
col1, col2 = st.columns(2)
with col1:
    uploaded_payout = st.file_uploader("1. eBay Auszahlungsbericht (CSV hochladen)", type=["csv"], key="payout")
with col2:
    uploaded_invoice = st.file_uploader("2. Soll-Rechnung mit 154 Pos. (Excel/CSV - optional)", type=["xlsx", "csv"], key="invoice")

def parse_german_float(val):
    if pd.isna(val) or val == '--' or str(val).strip() == '':
        return 0.0
    return float(str(val).replace('.', '').replace(',', '.'))

def get_commission_rate(sku):
    if pd.isna(sku):
        return 0.035
    sku_clean = str(sku).strip().upper()
    prefix = sku_clean.split('/')[0].strip()
    if prefix in ['BA', 'MK', 'PP', '001'] or prefix.startswith('001'):
        return 0.005
    return 0.035

if uploaded_payout is not None:
    try:
        content = uploaded_payout.getvalue().decode('utf-8', errors='ignore')
        lines = content.splitlines()
        
        header_idx = 0
        for i, line in enumerate(lines):
            if "Datum der Transaktionserstellung" in line:
                header_idx = i
                break
                
        df_payout = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])), sep=';')
        
        # --- 1. DUBLETTENPRÜFUNG ---
        initial_count = len(df_payout)
        df_payout = df_payout.drop_duplicates(subset=['Bestellnummer', 'Datum der Transaktionserstellung', 'Betrag abzügl. Kosten'])
        duplicates_removed = initial_count - len(df_payout)
        
        if duplicates_removed > 0:
            st.warning(f"⚠️ Dublettenprüfung: Es wurden {duplicates_removed} doppelte Transaktionen automatisch entfernt!")
        else:
            st.success(f"✅ Auszahlungsbericht geladen: {len(df_payout)} eindeutige Transaktionen gefunden.")
        
        df_payout['Auszahlung_Netto_eBay'] = df_payout['Betrag abzügl. Kosten'].apply(parse_german_float)
        df_payout['SKU'] = df_payout['Bestandseinheit'].fillna('OHNE_SKU').astype(str).str.strip()
        df_payout['Provisionssatz'] = df_payout['SKU'].apply(get_commission_rate)
        df_payout['Provision_EUR'] = (df_payout['Auszahlung_Netto_eBay'] * df_payout['Provisionssatz']).round(2)
        df_payout['Auszahlung_Partner_EUR'] = (df_payout['Auszahlung_Netto_eBay'] - df_payout['Provision_EUR']).round(2)
        df_payout['SKU_Prefix'] = df_payout['SKU'].apply(lambda x: str(x).split('/')[0].strip().upper() if pd.notna(x) else 'FEHLT')

        # --- 2. SOLL-IST ABGLEICH GEGEN DIE RECHNUNG ---
        if uploaded_invoice is not None:
            if uploaded_invoice.name.endswith('.xlsx'):
                df_inv = pd.read_excel(uploaded_invoice, header=2)
            else:
                df_inv = pd.read_csv(uploaded_invoice)
            
            payout_orders = set(df_payout['Bestellnummer'].dropna().astype(str).str.strip())
            inv_orders = set(df_inv['Bestellnummer'].dropna().astype(str).str.strip())
            
            missing_in_payout = inv_orders - payout_orders
            
            st.subheader("⚖️ Abgleich: Rechnung vs. eBay-Auszahlung")
            st.info(f"Von {len(inv_orders)} Rechnungs-Positionen wurden **{len(inv_orders - missing_in_payout)}** bereits bei eBay ausgezahlt.")
            
            if len(missing_in_payout) > 0:
                st.error(f"⏳ **{len(missing_in_payout)} Bestellungen fehlen noch im Auszahlungsbericht!** (Geld steht noch aus)")
                st.write("Diese Bestellnummern aus deiner Rechnung wurden noch nicht ausgezahlt:", list(missing_in_payout))

        # Gesamtübersicht nach SKU
        st.subheader("📊 Gesamtübersicht nach SKU")
        summary = df_payout.groupby('SKU_Prefix').agg(
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
        st.subheader("🔍 Detailansicht pro Partner/SKU")
        selected_sku = st.selectbox("SKU / Partner auswählen", summary['SKU_Prefix'].unique())
        filtered_df = df_payout[df_payout['SKU_Prefix'] == selected_sku]
        st.dataframe(filtered_df[['Datum der Transaktionserstellung', 'Typ', 'Bestellnummer', 'Angebotstitel', 'Auszahlung_Netto_eBay', 'Provision_EUR', 'Auszahlung_Partner_EUR']])

    except Exception as e:
        st.error(f"Fehler beim Verarbeiten der Dateien: {e}")
