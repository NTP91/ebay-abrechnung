import streamlit as st
import pandas as pd
import io
import re

st.set_page_config(page_title="eBay Payout & SKU-Abrechnung", layout="wide")

st.title("📦 eBay Payout & SKU-Abrechnungs Tool")
st.write("Lade deine eBay Auszahlungsberichte (CSV) sowie deine Soll-Rechnung (Excel/CSV) hoch.")

col1, col2 = st.columns(2)
with col1:
    uploaded_payout = st.file_uploader("1. eBay Auszahlungsberichte (CSV)", type=["csv"], accept_multiple_files=True, key="payout")
with col2:
    uploaded_invoice = st.file_uploader("2. Soll-Rechnung / Referenz (Excel/CSV)", type=["xlsx", "csv"], key="invoice")

# Hilfsfunktionen
def parse_german_float(val):
    if pd.isna(val) or val == '--' or str(val).strip() == '':
        return 0.0
    return float(str(val).replace('.', '').replace(',', '.'))

def clean_order_number(val):
    if pd.isna(val):
        return ""
    s = str(val).strip()
    if s.endswith('.0'):
        s = s[:-2]
    s = re.sub(r'[^A-Za-z0-9]', '', s).upper()
    s = s.lstrip('0')
    return s

def extract_partner_prefix(sku):
    if pd.isna(sku) or str(sku).strip() in ['--', '']:
        return 'OHNE_SKU'
    sku_clean = str(sku).strip().upper()
    raw_prefix = sku_clean.split('/')[0].strip()
    
    if raw_prefix.startswith('001') or raw_prefix == '001':
        return '001'
    
    match = re.match(r'^([A-Z]+)', raw_prefix)
    if match:
        return match.group(1)
    return raw_prefix

GROUP_A_PREFIXES = ['PP', 'BA', 'MK', '001']

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
                if "Bestellnummer" in line or "Datum der Transaktionserstellung" in line:
                    header_idx = i
                    break
            df_temp = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])), sep=';')
            all_dfs.append(df_temp)
        
        df_payout = pd.concat(all_dfs, ignore_index=True)
        
        # Säubern
        df_payout['Bestellnummer_Match'] = df_payout['Bestellnummer'].apply(clean_order_number)
        df_payout = df_payout.drop_duplicates(subset=['Bestellnummer_Match', 'Datum der Transaktionserstellung', 'Betrag abzügl. Kosten'])
        
        df_payout['eBay_Brutto'] = df_payout['Betrag abzügl. Kosten'].apply(parse_german_float)
        df_payout['SKU'] = df_payout['Bestandseinheit'].fillna('OHNE_SKU').astype(str).str.strip()
        df_payout['SKU_Prefix'] = df_payout['SKU'].apply(extract_partner_prefix)
        
        df_payout['Gruppe'] = df_payout['SKU_Prefix'].apply(
            lambda p: 'Gruppe A (Direkt)' if p in GROUP_A_PREFIXES else ('Ohne Zuordnung' if p == 'OHNE_SKU' else 'Gruppe B (Über Dich)')
        )
        
        # Provisionen berechnen
        df_payout['Evelyn_Prov_EUR'] = (df_payout['eBay_Brutto'] * 0.005).round(2)
        df_payout['Auszahlung_Evelyn_Brutto'] = (df_payout['eBay_Brutto'] - df_payout['Evelyn_Prov_EUR']).round(2)
        
        df_payout['Partner_Prov_Satz'] = df_payout['SKU_Prefix'].apply(lambda p: 0.005 if p in GROUP_A_PREFIXES else 0.035)
        df_payout['Partner_Prov_EUR'] = (df_payout['eBay_Brutto'] * df_payout['Partner_Prov_Satz']).round(2)
        df_payout['Auszahlung_Partner_Brutto'] = (df_payout['eBay_Brutto'] - df_payout['Partner_Prov_EUR']).round(2)
        
        df_payout['Deine_Marge_EUR'] = df_payout.apply(
            lambda r: (r['Partner_Prov_EUR'] - r['Evelyn_Prov_EUR']) if r['Gruppe'] == 'Gruppe B (Über Dich)' else 0.0, axis=1
        ).round(2)

        # ---------------------------------------------------------
        # SOLL-IST STATUS OVERVIEW & INTERNE MARGE (NUR FÜR DICH)
        # ---------------------------------------------------------
        if uploaded_invoice is not None:
            if uploaded_invoice.name.endswith('.xlsx'):
                df_inv_raw = pd.read_excel(uploaded_invoice)
                header_row_idx = 0
                for r_idx, row in df_inv_raw.iterrows():
                    if any("Bestellnummer" in str(val) for val in row.values):
                        header_row_idx = r_idx
                        break
                df_inv = pd.read_excel(uploaded_invoice, header=header_row_idx)
            else:
                df_inv = pd.read_csv(uploaded_invoice)
            
            inv_col = None
            for col in df_inv.columns:
                if "Bestellnummer" in str(col) or "Order" in str(col) or "Bestell-Nr" in str(col):
                    inv_col = col
                    break
            
            if inv_col:
                df_inv['Bestellnummer_Match'] = df_inv[inv_col].apply(clean_order_number)
                payout_orders = set(df_payout['Bestellnummer_Match'].dropna()) - {''}
                inv_orders = set(df_inv['Bestellnummer_Match'].dropna()) - {''}
                
                paid_count = len(inv_orders.intersection(payout_orders))
                unpaid_count = len(inv_orders - payout_orders)
                
                st.markdown("---")
                st.subheader("⚖️ Soll-Ist Statusübersicht & Interne Kennzahlen")
                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("Rechnung Positionen", len(inv_orders))
                m2.metric("✅ Ausbezahlt", f"{paid_count} Pos.")
                m3.metric("⏳ Noch Offen", f"{unpaid_count} Pos.")
                m4.metric("💰 Gesamterlös Brutto", f"{df_payout['eBay_Brutto'].sum():,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.'))
                m5.metric("🔒 Deine Marge (3,0 % Gruppe B)", f"{df_payout['Deine_Marge_EUR'].sum():,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.'))

        # ---------------------------------------------------------
        # BLOCK 1: GESAMTABRECHNUNG FÜR EVELYN (NUR GRUPPE B)
        # ---------------------------------------------------------
        st.markdown("---")
        st.subheader("📊 1. Gruppe B – Gesamtabrechnung für Evelyn")
        st.info("ℹ️ **Verwendungszweck:** Abrechnung gegenüber Evelyn (0,5 % Provision). Enthält keine internen Margen.")

        df_grp_b = df_payout[df_payout['Gruppe'] == 'Gruppe B (Über Dich)'].copy()

        if not df_grp_b.empty:
            summary_b = df_grp_b.groupby('SKU_Prefix').agg(
                Anzahl_Transaktionen=('SKU', 'count'),
                eBay_Brutto_Gesamt=('eBay_Brutto', 'sum'),
                Evelyn_Provision=('Evelyn_Prov_EUR', 'sum'),
                Auszahlungsbetrag=('Auszahlung_Evelyn_Brutto', 'sum')
            ).reset_index()
            
            total_row_b = pd.DataFrame([{
                'SKU_Prefix': 'GESAMTSUMME',
                'Anzahl_Transaktionen': summary_b['Anzahl_Transaktionen'].sum(),
                'eBay_Brutto_Gesamt': summary_b['eBay_Brutto_Gesamt'].sum(),
                'Evelyn_Provision': summary_b['Evelyn_Provision'].sum(),
                'Auszahlungsbetrag': summary_b['Auszahlungsbetrag'].sum()
            }])
            
            summary_b_final = pd.concat([summary_b, total_row_b], ignore_index=True)
            
            # Schöne, neutrale Header für UI
            summary_b_display = summary_b_final.rename(columns={
                'SKU_Prefix': 'Partner / Kürzel',
                'Anzahl_Transaktionen': 'Anzahl Transaktionen',
                'eBay_Brutto_Gesamt': 'Erlös Brutto (€)',
                'Evelyn_Provision': 'Provision 0,5 % (€)',
                'Auszahlungsbetrag': 'Auszahlungsbetrag (€)'
            })
            
            st.dataframe(
                summary_b_display.style.format({
                    'Erlös Brutto (€)': '{:.2f} €',
                    'Provision 0,5 % (€)': '{:.2f} €',
                    'Auszahlungsbetrag (€)': '{:.2f} €'
                }),
                use_container_width=True
            )

            buffer_evelyn_b = io.BytesIO()
            with pd.ExcelWriter(buffer_evelyn_b, engine='openpyxl') as writer:
                summary_b_display.to_excel(writer, index=False, sheet_name='Übersicht_Gruppe_B')
                
            st.download_button(
                label="📥 Gesamtabrechnung Gruppe B für Evelyn herunterladen (Excel)",
                data=buffer_evelyn_b.getvalue(),
                file_name="Gesamtabrechnung_GruppeB_fuer_Evelyn.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary"
            )

        # ---------------------------------------------------------
        # BLOCK 2: GRUPPE A – DIREKTABRECHNUNG MIT EVELYN
        # ---------------------------------------------------------
        st.markdown("---")
        st.subheader("🏷️ 2. Gruppe A – Direktabrechnungen für Evelyn (PP, BA, MK, 001)")

        df_grp_a = df_payout[df_payout['Gruppe'] == 'Gruppe A (Direkt)'].copy()

        if not df_grp_a.empty:
            summary_a = df_grp_a.groupby('SKU_Prefix').agg(
                Anzahl_Transaktionen=('SKU', 'count'),
                eBay_Brutto_Gesamt=('eBay_Brutto', 'sum'),
                Evelyn_Provision=('Evelyn_Prov_EUR', 'sum'),
                Auszahlungsbetrag=('Auszahlung_Evelyn_Brutto', 'sum')
            ).reset_index()

            total_row_a = pd.DataFrame([{
                'SKU_Prefix': 'GESAMTSUMME',
                'Anzahl_Transaktionen': summary_a['Anzahl_Transaktionen'].sum(),
                'eBay_Brutto_Gesamt': summary_a['eBay_Brutto_Gesamt'].sum(),
                'Evelyn_Provision': summary_a['Evelyn_Provision'].sum(),
                'Auszahlungsbetrag': summary_a['Auszahlungsbetrag'].sum()
            }])

            summary_a_final = pd.concat([summary_a, total_row_a], ignore_index=True)

            summary_a_display = summary_a_final.rename(columns={
                'SKU_Prefix': 'Partner / Kürzel',
                'Anzahl_Transaktionen': 'Anzahl Transaktionen',
                'eBay_Brutto_Gesamt': 'Erlös Brutto (€)',
                'Evelyn_Provision': 'Provision 0,5 % (€)',
                'Auszahlungsbetrag': 'Auszahlungsbetrag (€)'
            })

            st.dataframe(
                summary_a_display.style.format({
                    'Erlös Brutto (€)': '{:.2f} €',
                    'Provision 0,5 % (€)': '{:.2f} €',
                    'Auszahlungsbetrag (€)': '{:.2f} €'
                }),
                use_container_width=True
            )

            buffer_evelyn_a = io.BytesIO()
            with pd.ExcelWriter(buffer_evelyn_a, engine='openpyxl') as writer:
                summary_a_display.to_excel(writer, index=False, sheet_name='Übersicht_Gruppe_A')

            st.download_button(
                label="📥 Übersicht Gruppe A für Evelyn herunterladen (Excel)",
                data=buffer_evelyn_a.getvalue(),
                file_name="Direktabrechnungen_GruppeA_fuer_Evelyn.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        else:
            st.success("✅ Keine Positionen für Gruppe A in den hochgeladenen Payouts enthalten.")

        # ---------------------------------------------------------
        # BLOCK 3: GUTSCHRIFTEN & ERSTATTUNGEN
        # ---------------------------------------------------------
        st.markdown("---")
        st.subheader("🔻 3. Gutschriften, Erstattungen & Gebühren (Für Lexoffice)")

        df_refunds = df_payout[(df_payout['eBay_Brutto'] < 0) | (df_payout['SKU_Prefix'] == 'OHNE_SKU')].copy()

        if not df_refunds.empty:
            refund_display = df_refunds[[
                'Datum der Transaktionserstellung',
                'Bestellnummer',
                'SKU_Prefix',
                'SKU',
                'Angebotstitel',
                'eBay_Brutto',
                'Partner_Prov_EUR',
                'Auszahlung_Partner_Brutto'
            ]].rename(columns={
                'Datum der Transaktionserstellung': 'Datum',
                'SKU_Prefix': 'Partner',
                'eBay_Brutto': 'Gutschrift Brutto (€)',
                'Partner_Prov_EUR': 'Provision (€)',
                'Auszahlung_Partner_Brutto': 'Gutschrift Netto/Auszahlung (€)'
            })

            sum_refunds = pd.DataFrame([{
                'Datum': 'GESAMTSUMME',
                'Bestellnummer': '',
                'Partner': '',
                'SKU': '',
                'Angebotstitel': '',
                'Gutschrift Brutto (€)': refund_display['Gutschrift Brutto (€)'].sum(),
                'Provision (€)': refund_display['Provision (€)'].sum(),
                'Gutschrift Netto/Auszahlung (€)': refund_display['Gutschrift Netto/Auszahlung (€)'].sum()
            }])

            refund_final = pd.concat([refund_display, sum_refunds], ignore_index=True)

            st.dataframe(
                refund_final.style.format({
                    'Gutschrift Brutto (€)': '{:.2f} €',
                    'Provision (€)': '{:.2f} €',
                    'Gutschrift Netto/Auszahlung (€)': '{:.2f} €'
                }, na_rep=''),
                use_container_width=True
            )

        # ---------------------------------------------------------
        # BLOCK 4: EINZELABRECHNUNGEN PRO PARTNER (3,5% für Gruppe B)
        # ---------------------------------------------------------
        st.markdown("---")
        st.subheader("🔍 4. Einzelabrechnung pro Partner (3,5 % Provision für Gruppe B)")

        all_partners = [p for p in df_payout['SKU_Prefix'].unique() if p not in ['OHNE_SKU', 'FEHLT', '--', '']]
        if all_partners:
            selected_partner = st.selectbox("Partner / Kürzel auswählen:", all_partners)
            filtered_p = df_payout[df_payout['SKU_Prefix'] == selected_partner].copy()
            
            partner_display = filtered_p[[
                'Datum der Transaktionserstellung',
                'Bestellnummer',
                'SKU_Prefix',
                'SKU',
                'Angebotstitel',
                'eBay_Brutto',
                'Partner_Prov_EUR',
                'Auszahlung_Partner_Brutto'
            ]].rename(columns={
                'Datum der Transaktionserstellung': 'Datum',
                'SKU_Prefix': 'Partner',
                'eBay_Brutto': 'Erlös Brutto (€)',
                'Partner_Prov_EUR': 'Provision (€)',
                'Auszahlung_Partner_Brutto': 'Auszahlungsbetrag Netto (€)'
            })
            
            sum_row_p = pd.DataFrame([{
                'Datum': 'GESAMTSUMME',
                'Bestellnummer': '',
                'Partner': selected_partner,
                'SKU': '',
                'Angebotstitel': '',
                'Erlös Brutto (€)': partner_display['Erlös Brutto (€)'].sum(),
                'Provision (€)': partner_display['Provision (€)'].sum(),
                'Auszahlungsbetrag Netto (€)': partner_display['Auszahlungsbetrag Netto (€)'].sum()
            }])
            
            partner_final = pd.concat([partner_display, sum_row_p], ignore_index=True)
            
            st.dataframe(
                partner_final.style.format({
                    'Erlös Brutto (€)': '{:.2f} €',
                    'Provision (€)': '{:.2f} €',
                    'Auszahlungsbetrag Netto (€)': '{:.2f} €'
                }, na_rep=''),
                use_container_width=True
            )
            
            buffer_partner_excel = io.BytesIO()
            with pd.ExcelWriter(buffer_partner_excel, engine='openpyxl') as writer:
                partner_final.to_excel(writer, index=False, sheet_name=f'Abrechnung_{selected_partner}')
                
            st.download_button(
                label=f"📥 Excel-Abrechnung für Partner '{selected_partner}' herunterladen",
                data=buffer_partner_excel.getvalue(),
                file_name=f"Abrechnung_Partner_{selected_partner}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

    except Exception as e:
        st.error(f"Fehler beim Verarbeiten der Dateien: {e}")
