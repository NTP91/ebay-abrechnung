import streamlit as st
import pandas as pd
import io

st.set_page_config(page_title="eBay Payout & Provision Tool", layout="wide")

st.title("📦 eBay Payout & SKU-Abrechnungs Tool")
st.write("Lade deine eBay Auszahlungsberichte (CSV) sowie deine Soll-Rechnung (Excel/CSV) hoch.")

col1, col2 = st.columns(2)
with col1:
    uploaded_payout = st.file_uploader("1. eBay Auszahlungsberichte (CSV)", type=["csv"], accept_multiple_files=True, key="payout")
with col2:
    uploaded_invoice = st.file_uploader("2. Soll-Rechnung / Referenz (Excel/CSV)", type=["xlsx", "csv"], key="invoice")

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

if uploaded_payout:
    try:
        all_dfs = []
        processed_file_names = set()
        
        for file in uploaded_payout:
            if file.name in processed_file_names:
                continue
            processed_file_names.add(file.name)
            content = file.getvalue().decode('utf-8', errors='ignore')
            lines = content.splitlines()
            header_idx = 0
            for i, line in enumerate(lines):
                if "Datum der Transaktionserstellung" in line:
                    header_idx = i
                    break
            df_temp = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])), sep=';')
            all_dfs.append(df_temp)
        
        df_payout = pd.concat(all_dfs, ignore_index=True)
        df_payout = df_payout.drop_duplicates(subset=['Bestellnummer', 'Datum der Transaktionserstellung', 'Betrag abzügl. Kosten'])
        
        # Aufbereitung Payout
        df_payout['Auszahlung_Netto_eBay'] = df_payout['Betrag abzügl. Kosten'].apply(parse_german_float)
        df_payout['SKU'] = df_payout['Bestandseinheit'].fillna('OHNE_SKU').astype(str).str.strip()
        df_payout['Provisionssatz'] = df_payout['SKU'].apply(get_commission_rate)
        df_payout['Provision_EUR'] = (df_payout['Auszahlung_Netto_eBay'] * df_payout['Provisionssatz']).round(2)
        df_payout['Auszahlung_Partner_EUR'] = (df_payout['Auszahlung_Netto_eBay'] - df_payout['Provision_EUR']).round(2)
        df_payout['SKU_Prefix'] = df_payout['SKU'].apply(lambda x: str(x).split('/')[0].strip().upper() if pd.notna(x) else 'FEHLT')

        # --- RECHNUNG EINLESEN & SOLL-IST-KACHELN ---
        if uploaded_invoice is not None:
            if uploaded_invoice.name.endswith('.xlsx'):
                df_inv = pd.read_excel(uploaded_invoice, header=2)
            else:
                df_inv = pd.read_csv(uploaded_invoice)
            
            payout_orders = set(df_payout['Bestellnummer'].dropna().astype(str).str.strip())
            inv_orders = set(df_inv['Bestellnummer'].dropna().astype(str).str.strip())
            
            paid_count = len(inv_orders.intersection(payout_orders))
            unpaid_count = len(inv_orders - payout_orders)
            
            st.markdown("---")
            st.subheader("⚖️ Soll-Ist Statusübersicht")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Rechnung Positionen", len(inv_orders))
            m2.metric("✅ Ausbezahlt", f"{paid_count} Pos.")
            m3.metric("⏳ noch Offen", f"{unpaid_count} Pos.")
            m4.metric("💰 Bereits Ausgezahlt", f"{df_payout['Auszahlung_Netto_eBay'].sum():,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.'))
            
            if unpaid_count > 0:
                with st.expander(f"🔴 Liste der {unpaid_count} noch nicht ausgezahlten Positionen anzeigen"):
                    missing_mask = df_inv['Bestellnummer'].astype(str).str.strip().isin(inv_orders - payout_orders)
                    df_missing = df_inv[missing_mask]
                    st.dataframe(df_missing, use_container_width=True)

        st.markdown("---")
        st.subheader("📊 Gesamtübersicht nach SKU / Partner")
        
        summary = df_payout.groupby('SKU_Prefix').agg(
            Anzahl_Transaktionen=('SKU', 'count'),
            eBay_Auszahlung_Gesamt=('Auszahlung_Netto_eBay', 'sum'),
            Provision_Gesamt=('Provision_EUR', 'sum'),
            Partner_Auszahlung_Gesamt=('Auszahlung_Partner_EUR', 'sum')
        ).reset_index()
        
        # SUMMENZEILE ANFÜGEN
        total_row = pd.DataFrame([{
            'SKU_Prefix': 'GESAMT',
            'Anzahl_Transaktionen': summary['Anzahl_Transaktionen'].sum(),
            'eBay_Auszahlung_Gesamt': summary['eBay_Auszahlung_Gesamt'].sum(),
            'Provision_Gesamt': summary['Provision_Gesamt'].sum(),
            'Partner_Auszahlung_Gesamt': summary['Partner_Auszahlung_Gesamt'].sum()
        }])
        
        summary_with_total = pd.concat([summary, total_row], ignore_index=True)
        
        st.dataframe(
            summary_with_total.style.format({
                'eBay_Auszahlung_Gesamt': '{:.2f} €',
                'Provision_Gesamt': '{:.2f} €',
                'Partner_Auszahlung_Gesamt': '{:.2f} €'
            }),
            use_container_width=True
        )

        # DETAILANSICHT
        st.subheader("🔍 Detailansicht pro Partner/SKU")
        selected_sku = st.selectbox("SKU / Partner auswählen", summary['SKU_Prefix'].unique())
        filtered_df = df_payout[df_payout['SKU_Prefix'] == selected_sku]
        st.dataframe(
            filtered_df[['Datum der Transaktionserstellung', 'Typ', 'Bestellnummer', 'Angebotstitel', 'Auszahlung_Netto_eBay', 'Provision_EUR', 'Auszahlung_Partner_EUR']],
            use_container_width=True
        )

    except Exception as e:
        st.error(f"Fehler beim Verarbeiten der Dateien: {e}")
