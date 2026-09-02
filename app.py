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
        
        # Kaufmännische Aufbereitung für Lexoffice
        df_payout['eBay_Brutto'] = df_payout['Betrag abzügl. Kosten'].apply(parse_german_float)
        df_payout['SKU'] = df_payout['Bestandseinheit'].fillna('OHNE_SKU').astype(str).str.strip()
        df_payout['Provisionssatz'] = df_payout['SKU'].apply(get_commission_rate)
        
        # 1. VK Netto = eBay Brutto / 1.19
        df_payout['VK (Netto)'] = (df_payout['eBay_Brutto'] / 1.19).round(2)
        # 2. Rabatt in Prozent für Lexoffice
        df_payout['Rabatt (%)'] = (df_payout['Provisionssatz'] * 100).round(2)
        # 3. Effektiver Partner-Auszahlungsbetrag (Brutto)
        df_payout['Provision_EUR'] = (df_payout['eBay_Brutto'] * df_payout['Provisionssatz']).round(2)
        df_payout['Auszahlung_Partner_Brutto'] = (df_payout['eBay_Brutto'] - df_payout['Provision_EUR']).round(2)
        
        df_payout['SKU_Prefix'] = df_payout['SKU'].apply(lambda x: str(x).split('/')[0].strip().upper() if pd.notna(x) else 'FEHLT')

        # SOLL-IST STATUS
        if uploaded_invoice is not None:
            if uploaded_invoice.name.endswith('.xlsx'):
                df_inv = pd.read_excel(uploaded_invoice, header=2)
            else:
                df_inv = pd.read_csv(uploaded_invoice)
            
            payout_orders = set(df_payout['Bestellnummer'].dropna().astype(str).str.strip())
            inv_orders = set(df_inv['Bestellnummer'].dropna().astype(str).str.strip())
            
            paid_count = len(inv_orders.intersection(payout_orders))
            unpaid_orders = inv_orders - payout_orders
            unpaid_count = len(unpaid_orders)
            
            st.markdown("---")
            st.subheader("⚖️ Soll-Ist Statusübersicht")
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Rechnung Positionen", len(inv_orders))
            m2.metric("✅ Ausbezahlt", f"{paid_count} Pos.")
            m3.metric("⏳ Noch Offen", f"{unpaid_count} Pos.")
            m4.metric("💰 eBay Erlös Brutto", f"{df_payout['eBay_Brutto'].sum():,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.'))
            m5.metric("🤝 Auszahlung Partner Brutto", f"{df_payout['Auszahlung_Partner_Brutto'].sum():,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.'))
            
            if unpaid_count > 0:
                missing_mask = df_inv['Bestellnummer'].astype(str).str.strip().isin(unpaid_orders)
                df_missing = df_inv[missing_mask]
                
                with st.expander(f"🔴 Liste der {unpaid_count} noch nicht ausgezahlten Positionen anzeigen"):
                    st.dataframe(df_missing, use_container_width=True)

        st.markdown("---")
        st.subheader("📊 Gesamtübersicht nach SKU / Partner")
        
        summary = df_payout.groupby('SKU_Prefix').agg(
            Anzahl_Transaktionen=('SKU', 'count'),
            eBay_Brutto_Gesamt=('eBay_Brutto', 'sum'),
            Provision_Gesamt=('Provision_EUR', 'sum'),
            Partner_Auszahlung_Brutto=('Auszahlung_Partner_Brutto', 'sum')
        ).reset_index()
        
        total_row = pd.DataFrame([{
            'SKU_Prefix': 'GESAMT',
            'Anzahl_Transaktionen': summary['Anzahl_Transaktionen'].sum(),
            'eBay_Brutto_Gesamt': summary['eBay_Brutto_Gesamt'].sum(),
            'Provision_Gesamt': summary['Provision_Gesamt'].sum(),
            'Partner_Auszahlung_Brutto': summary['Partner_Auszahlung_Brutto'].sum()
        }])
        
        summary_with_total = pd.concat([summary, total_row], ignore_index=True)
        
        st.dataframe(
            summary_with_total.style.format({
                'eBay_Brutto_Gesamt': '{:.2f} €',
                'Provision_Gesamt': '{:.2f} €',
                'Partner_Auszahlung_Brutto': '{:.2f} €'
            }),
            use_container_width=True
        )

        st.markdown("---")
        # DETAILANSICHT EXAKT IM LEXOFFICE FORMAT
        st.subheader("🔍 Lexoffice-Abrechnungsvorlage pro Partner")
        
        available_skus = [s for s in summary['SKU_Prefix'].unique() if s != 'FEHLT']
        selected_sku = st.selectbox("SKU / Partner auswählen", available_skus)
        
        filtered_df = df_payout[df_payout['SKU_Prefix'] == selected_sku].copy()
        
        # Aufbau nach Lexoffice-Reihenfolge
        filtered_df['Artikelbezeichnung'] = filtered_df['Angebotstitel'] + " (Bestellnr: " + filtered_df['Bestellnummer'].astype(str) + ")"
        filtered_df['Menge'] = 1
        filtered_df['Einheit'] = 'Stück'
        
        lexoffice_export = filtered_df[[
            'Artikelbezeichnung',
            'Menge',
            'Einheit',
            'VK (Netto)',
            'Rabatt (%)',
            'Auszahlung_Partner_Brutto'
        ]].rename(columns={
            'Auszahlung_Partner_Brutto': 'Auszahlungsbetrag Brutto (€)'
        })
        
        st.dataframe(lexoffice_export, use_container_width=True)
        
        buffer_partner = io.BytesIO()
        with pd.ExcelWriter(buffer_partner, engine='openpyxl') as writer:
            lexoffice_export.to_excel(writer, index=False, sheet_name=f'Lexoffice_{selected_sku}')
            
        st.download_button(
            label=f"📥 Lexoffice-Abrechnung für Partner '{selected_sku}' herunterladen",
            data=buffer_partner.getvalue(),
            file_name=f"Lexoffice_Abrechnung_{selected_sku}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    except Exception as e:
        st.error(f"Fehler beim Verarbeiten der Dateien: {e}")
