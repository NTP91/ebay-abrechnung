import streamlit as st
import pandas as pd
import io

st.set_page_config(page_title="eBay Payout & SKU-Abrechnung", layout="wide")

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
        
        # Berechnungen
        df_payout['eBay_Brutto'] = df_payout['Betrag abzügl. Kosten'].apply(parse_german_float)
        df_payout['SKU'] = df_payout['Bestandseinheit'].fillna('OHNE_SKU').astype(str).str.strip()
        df_payout['Provisionssatz'] = df_payout['SKU'].apply(get_commission_rate)
        
        df_payout['VK_Netto'] = (df_payout['eBay_Brutto'] / 1.19).round(2)
        df_payout['Rabatt_Prozent'] = (df_payout['Provisionssatz'] * 100).round(2)
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
        st.subheader("📊 1. Gesamtabrechnung für Evelyn (Intern mit allen Details & Provisionen)")
        
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

        # Interner Download für Evelyn (enthält Deine Provision)
        export_evelyn_details = df_payout[[
            'Datum der Transaktionserstellung',
            'Bestellnummer',
            'SKU_Prefix',
            'SKU',
            'Angebotstitel',
            'eBay_Brutto',
            'VK_Netto',
            'Rabatt_Prozent',
            'Provision_EUR',
            'Auszahlung_Partner_Brutto'
        ]].rename(columns={
            'SKU_Prefix': 'Partner',
            'eBay_Brutto': 'eBay Erlös Brutto (€)',
            'VK_Netto': 'VK Netto (€)',
            'Rabatt_Prozent': 'Provision (%)',
            'Provision_EUR': 'Deine Provision (€)',
            'Auszahlung_Partner_Brutto': 'Auszahlung an Partner Brutto (€)'
        })

        buffer_evelyn = io.BytesIO()
        with pd.ExcelWriter(buffer_evelyn, engine='openpyxl') as writer:
            summary_with_total.to_excel(writer, index=False, sheet_name='Übersicht_Partner')
            export_evelyn_details.to_excel(writer, index=False, sheet_name='Alle_Positionen')
            
        st.download_button(
            label="📥 Gesamtabrechnung für Evelyn herunterladen (Excel)",
            data=buffer_evelyn.getvalue(),
            file_name="Gesamtabrechnung_Evelyn_Alle_Partner.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary"
        )

        st.markdown("---")
        st.subheader("🔍 2. Einzelabrechnung pro Partner (Für Kunden / Partner-Export)")
        
        valid_skus = [s for s in summary['SKU_Prefix'].unique() if s not in ['FEHLT', '--', '']]
        if not valid_skus:
            valid_skus = [s for s in summary['SKU_Prefix'].unique() if s != 'FEHLT']
            
        selected_sku = st.selectbox("Partner / Kürzel auswählen:", valid_skus)
        
        filtered_p = df_payout[df_payout['SKU_Prefix'] == selected_sku].copy()
        
        # Partner-Ansicht: OHNE "Deine Provision (€)"
        partner_display = filtered_p[[
            'Datum der Transaktionserstellung',
            'Bestellnummer',
            'SKU_Prefix',
            'SKU',
            'Angebotstitel',
            'eBay_Brutto',
            'VK_Netto',
            'Rabatt_Prozent',
            'Auszahlung_Partner_Brutto'
        ]].rename(columns={
            'Datum der Transaktionserstellung': 'Datum',
            'SKU_Prefix': 'Partner',
            'eBay_Brutto': 'eBay Erlös Brutto (€)',
            'VK_Netto': 'VK Netto (€)',
            'Rabatt_Prozent': 'Provision (%)',
            'Auszahlung_Partner_Brutto': 'Auszahlungsbetrag Brutto (€)'
        })
        
        # Summenzeile unten drunter
        sum_row_p = pd.DataFrame([{
            'Datum': 'GESAMTSUMME',
            'Bestellnummer': '',
            'Partner': selected_sku,
            'SKU': '',
            'Angebotstitel': '',
            'eBay Erlös Brutto (€)': partner_display['eBay Erlös Brutto (€)'].sum(),
            'VK Netto (€)': partner_display['VK Netto (€)'].sum(),
            'Provision (%)': partner_display['Provision (%)'].iloc[0] if len(partner_display) > 0 else 0,
            'Auszahlungsbetrag Brutto (€)': partner_display['Auszahlungsbetrag Brutto (€)'].sum()
        }])
        
        partner_final = pd.concat([partner_display, sum_row_p], ignore_index=True)
        
        st.dataframe(
            partner_final.style.format({
                'eBay Erlös Brutto (€)': '{:.2f} €',
                'VK Netto (€)': '{:.2f} €',
                'Auszahlungsbetrag Brutto (€)': '{:.2f} €'
            }, na_rep=''),
            use_container_width=True
        )
        
        buffer_partner_excel = io.BytesIO()
        with pd.ExcelWriter(buffer_partner_excel, engine='openpyxl') as writer:
            partner_final.to_excel(writer, index=False, sheet_name=f'Abrechnung_{selected_sku}')
            
        st.download_button(
            label=f"📥 Excel-Abrechnung für Partner '{selected_sku}' herunterladen",
            data=buffer_partner_excel.getvalue(),
            file_name=f"Abrechnung_Partner_{selected_sku}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    except Exception as e:
        st.error(f"Fehler beim Verarbeiten der Dateien: {e}")
