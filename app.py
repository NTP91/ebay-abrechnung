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
    """Entfernt ALLE Sonderzeichen, Bindestriche, Leerzeichen & führende Nullen für 100% Match-Garantie"""
    if pd.isna(val):
        return ""
    s = str(val).strip()
    if s.endswith('.0'):
        s = s[:-2]
    s = re.sub(r'[^A-Za-z0-9]', '', s).upper()
    s = s.lstrip('0')
    return s

def extract_partner_prefix(sku):
    """Extrahiert das Kürzel und fasst z.B. MH43, MH44 -> MH zusammen"""
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

# Definition der Gruppe A (Direktabrechnung mit Evelyn)
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
        
        # Säubern & Grundberechnungen
        df_payout['Bestellnummer_Match'] = df_payout['Bestellnummer'].apply(clean_order_number)
        df_payout = df_payout.drop_duplicates(subset=['Bestellnummer_Match', 'Datum der Transaktionserstellung', 'Betrag abzügl. Kosten'])
        
        df_payout['eBay_Brutto'] = df_payout['Betrag abzügl. Kosten'].apply(parse_german_float)
        df_payout['SKU'] = df_payout['Bestandseinheit'].fillna('OHNE_SKU').astype(str).str.strip()
        df_payout['SKU_Prefix'] = df_payout['SKU'].apply(extract_partner_prefix)
        
        # Zuordnung Gruppe A vs. Gruppe B
        df_payout['Gruppe'] = df_payout['SKU_Prefix'].apply(
            lambda p: 'Gruppe A (Direkt)' if p in GROUP_A_PREFIXES else ('Ohne Zuordnung' if p == 'OHNE_SKU' else 'Gruppe B (Über Dich)')
        )
        
        # Provisionssätze berechnen:
        # Evelyn bekommt immer 0,5%
        df_payout['Evelyn_Prov_Satz'] = 0.005
        df_payout['Evelyn_Prov_EUR'] = (df_payout['eBay_Brutto'] * df_payout['Evelyn_Prov_Satz']).round(2)
        df_payout['Auszahlung_Evelyn_Brutto'] = (df_payout['eBay_Brutto'] - df_payout['Evelyn_Prov_EUR']).round(2)
        
        # Partner-Provision: Gruppe A = 0,5%, Gruppe B = 3,5%
        df_payout['Partner_Prov_Satz'] = df_payout['SKU_Prefix'].apply(lambda p: 0.005 if p in GROUP_A_PREFIXES else 0.035)
        df_payout['Partner_Prov_EUR'] = (df_payout['eBay_Brutto'] * df_payout['Partner_Prov_Satz']).round(2)
        df_payout['Auszahlung_Partner_Brutto'] = (df_payout['eBay_Brutto'] - df_payout['Partner_Prov_EUR']).round(2)
        
        # Deine Marge (Nur bei Gruppe B: 3.0%)
        df_payout['Deine_Marge_EUR'] = df_payout.apply(
            lambda r: (r['Partner_Prov_EUR'] - r['Evelyn_Prov_EUR']) if r['Gruppe'] == 'Gruppe B (Über Dich)' else 0.0, axis=1
        ).round(2)

        # ---------------------------------------------------------
        # SOLL-IST STATUS OVERVIEW
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
                
                payout_orders = set(df_payout['Bestellnummer_Match'].dropna())
                payout_orders.discard('')
                
                inv_orders = set(df_inv['Bestellnummer_Match'].dropna())
                inv_orders.discard('')
                
                paid_orders = inv_orders.intersection(payout_orders)
                unpaid_orders = inv_orders - payout_orders
                
                paid_count = len(paid_orders)
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
                    missing_mask = df_inv['Bestellnummer_Match'].isin(unpaid_orders)
                    df_missing = df_inv[missing_mask]
                    
                    with st.expander(f"🔴 Liste der {unpaid_count} noch nicht ausgezahlten Positionen anzeigen"):
                        st.dataframe(df_missing, use_container_width=True)

        # ---------------------------------------------------------
        # BLOCK 1: GESAMTABRECHNUNG FÜR EVELYN (NUR GRUPPE B)
        # ---------------------------------------------------------
        st.markdown("---")
        st.subheader("📊 1. Gruppe B – Gesamtabrechnung für Evelyn (Über DICH)")
        st.info("ℹ️ **Verwendungszweck:** Diese Rechnung nutzt du für die Abrechnung gegenüber Evelyn. Sie enthält NUR die Umsätze aus Gruppe B, die über dich verteilt werden. Evelyn behält 0,5 % Provision.")

        df_grp_b = df_payout[df_payout['Gruppe'] == 'Gruppe B (Über Dich)'].copy()

        if not df_grp_b.empty:
            summary_b = df_grp_b.groupby('SKU_Prefix').agg(
                Anzahl_Transaktionen=('SKU', 'count'),
                eBay_Brutto_Gesamt=('eBay_Brutto', 'sum'),
                Evelyn_Provision_0_5=('Evelyn_Prov_EUR', 'sum'),
                Auszahlung_von_Evelyn_an_Dich=('Auszahlung_Evelyn_Brutto', 'sum'),
                Deine_Marge_3_0=('Deine_Marge_EUR', 'sum')
            ).reset_index()
            
            total_row_b = pd.DataFrame([{
                'SKU_Prefix': 'GESAMTSUMME (Gruppe B)',
                'Anzahl_Transaktionen': summary_b['Anzahl_Transaktionen'].sum(),
                'eBay_Brutto_Gesamt': summary_b['eBay_Brutto_Gesamt'].sum(),
                'Evelyn_Provision_0_5': summary_b['Evelyn_Provision_0_5'].sum(),
                'Auszahlung_von_Evelyn_an_Dich': summary_b['Auszahlung_von_Evelyn_an_Dich'].sum(),
                'Deine_Marge_3_0': summary_b['Deine_Marge_3_0'].sum()
            }])
            
            summary_b_final = pd.concat([summary_b, total_row_b], ignore_index=True)
            
            st.dataframe(
                summary_b_final.style.format({
                    'eBay_Brutto_Gesamt': '{:.2f} €',
                    'Evelyn_Provision_0_5': '{:.2f} €',
                    'Auszahlung_von_Evelyn_an_Dich': '{:.2f} €',
                    'Deine_Marge_3_0': '{:.2f} €'
                }),
                use_container_width=True
            )

            export_evelyn_b_details = df_grp_b[[
                'Datum der Transaktionserstellung',
                'Bestellnummer',
                'SKU_Prefix',
                'SKU',
                'Angebotstitel',
                'eBay_Brutto',
                'Evelyn_Prov_EUR',
                'Auszahlung_Evelyn_Brutto'
            ]].rename(columns={
                'SKU_Prefix': 'Partner',
                'eBay_Brutto': 'eBay Erlös Brutto (€)',
                'Evelyn_Prov_EUR': 'Evelyn Provision 0,5% (€)',
                'Auszahlung_Evelyn_Brutto': 'Auszahlungsbetrag von Evelyn (€)'
            })

            buffer_evelyn_b = io.BytesIO()
            with pd.ExcelWriter(buffer_evelyn_b, engine='openpyxl') as writer:
                summary_b_final.to_excel(writer, index=False, sheet_name='Übersicht_Gruppe_B')
                export_evelyn_b_details.to_excel(writer, index=False, sheet_name='Alle_Positionen_Gruppe_B')
                
            st.download_button(
                label="📥 Gesamtabrechnung Gruppe B für Evelyn herunterladen (Excel)",
                data=buffer_evelyn_b.getvalue(),
                file_name="Gesamtabrechnung_GruppeB_fuer_Evelyn.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary"
            )
        else:
            st.write("Keine Positionen für Gruppe B in den aktuellen Payouts gefunden.")

        # ---------------------------------------------------------
        # BLOCK 2: GRUPPE A – DIREKTABRECHNUNG MIT EVELYN
        # ---------------------------------------------------------
        st.markdown("---")
        st.subheader("🏷️ 2. Gruppe A – Direktabrechnungen für Evelyn (PP, BA, MK, 001)")
        st.info("ℹ️ **Verwendungszweck:** Diese Partner rechnen mit 0,5 % Provision direkt mit Evelyn ab (laufen nicht über deine Marge).")

        df_grp_a = df_payout[df_payout['Gruppe'] == 'Gruppe A (Direkt)'].copy()

        if not df_grp_a.empty:
            summary_a = df_grp_a.groupby('SKU_Prefix').agg(
                Anzahl_Transaktionen=('SKU', 'count'),
                eBay_Brutto_Gesamt=('eBay_Brutto', 'sum'),
                Evelyn_Provision_0_5=('Evelyn_Prov_EUR', 'sum'),
                Direkt_Auszahlung_Evelyn=('Auszahlung_Evelyn_Brutto', 'sum')
            ).reset_index()

            total_row_a = pd.DataFrame([{
                'SKU_Prefix': 'GESAMTSUMME (Gruppe A)',
                'Anzahl_Transaktionen': summary_a['Anzahl_Transaktionen'].sum(),
                'eBay_Brutto_Gesamt': summary_a['eBay_Brutto_Gesamt'].sum(),
                'Evelyn_Provision_0_5': summary_a['Evelyn_Provision_0_5'].sum(),
                'Direkt_Auszahlung_Evelyn': summary_a['Direkt_Auszahlung_Evelyn'].sum()
            }])

            summary_a_final = pd.concat([summary_a, total_row_a], ignore_index=True)

            st.dataframe(
                summary_a_final.style.format({
                    'eBay_Brutto_Gesamt': '{:.2f} €',
                    'Evelyn_Provision_0_5': '{:.2f} €',
                    'Direkt_Auszahlung_Evelyn': '{:.2f} €'
                }),
                use_container_width=True
            )

            buffer_evelyn_a = io.BytesIO()
            with pd.ExcelWriter(buffer_evelyn_a, engine='openpyxl') as writer:
                summary_a_final.to_excel(writer, index=False, sheet_name='Übersicht_Gruppe_A')
                df_grp_a.to_excel(writer, index=False, sheet_name='Alle_Positionen_Gruppe_A')

            st.download_button(
                label="📥 Übersicht Gruppe A für Evelyn herunterladen (Excel)",
                data=buffer_evelyn_a.getvalue(),
                file_name="Direktabrechnungen_GruppeA_fuer_Evelyn.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        else:
            st.success("✅ Keine Positionen für Gruppe A (PP, BA, MK, 001) in den hochgeladenen Payouts enthalten.")

        # ---------------------------------------------------------
        # BLOCK 3: GUTSCHRIFTEN & ERSTATTUNGEN (OPTION B + GEBÜHREN)
        # ---------------------------------------------------------
        st.markdown("---")
        st.subheader("🔻 3. Gutschriften, Erstattungen & Gebühren (Für Lexoffice)")
        st.info("ℹ️ **Verwendungszweck:** Alle negativen Beträge (Retouren/Gutschriften) sowie reine Gebühren ohne SKU. Nutze diese Übersicht für Lexoffice-Gutschriften.")

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
        else:
            st.success("✅ Keine negativen Gutschriften oder ungeklärten Gebühren enthalten.")

        # ---------------------------------------------------------
        # BLOCK 4: EINZELABRECHNUNGEN PRO PARTNER
        # ---------------------------------------------------------
        st.markdown("---")
        st.subheader("🔍 4. Einzelabrechnung pro Partner")
        st.info("ℹ️ **Hinweis:** Wähle hier einen Partner aus, um dessen spezifische Einzel-Abrechnung einzusehen und als Excel herunterzuladen.")

        all_partners = [p for p in df_payout['SKU_Prefix'].unique() if p not in ['OHNE_SKU', 'FEHLT', '--', '']]
        if not all_partners:
            all_partners = list(df_payout['SKU_Prefix'].unique())
            
        selected_partner = st.selectbox("Partner / Kürzel auswählen:", all_partners)
        
        filtered_p = df_payout[df_payout['SKU_Prefix'] == selected_partner].copy()
        is_grp_a = selected_partner in GROUP_A_PREFIXES
        
        partner_display = filtered_p[[
            'Datum der Transaktionserstellung',
            'Bestellnummer',
            'SKU_Prefix',
            'SKU',
            'Angebotstitel',
            'eBay_Brutto',
            'Partner_Prov_Satz',
            'Auszahlung_Partner_Brutto'
        ]].copy()
        
        partner_display['Provision (%)'] = (partner_display['Partner_Prov_Satz'] * 100).round(2)
        
        partner_display = partner_display.rename(columns={
            'Datum der Transaktionserstellung': 'Datum',
            'SKU_Prefix': 'Partner',
            'eBay_Brutto': 'eBay Erlös Brutto (€)',
            'Auszahlung_Partner_Brutto': 'Auszahlungsbetrag Brutto (€)'
        })[[
            'Datum', 'Bestellnummer', 'Partner', 'SKU', 'Angebotstitel', 
            'eBay Erlös Brutto (€)', 'Provision (%)', 'Auszahlungsbetrag Brutto (€)'
        ]]
        
        sum_row_p = pd.DataFrame([{
            'Datum': 'GESAMTSUMME',
            'Bestellnummer': '',
            'Partner': selected_partner,
            'SKU': '',
            'Angebotstitel': '',
            'eBay Erlös Brutto (€)': partner_display['eBay Erlös Brutto (€)'].sum(),
            'Provision (%)': partner_display['Provision (%)'].iloc[0] if len(partner_display) > 0 else 0,
            'Auszahlungsbetrag Brutto (€)': partner_display['Auszahlungsbetrag Brutto (€)'].sum()
        }])
        
        partner_final = pd.concat([partner_display, sum_row_p], ignore_index=True)
        
        st.dataframe(
            partner_final.style.format({
                'eBay Erlös Brutto (€)': '{:.2f} €',
                'Auszahlungsbetrag Brutto (€)': '{:.2f} €'
            }, na_rep=''),
            use_container_width=True
        )
        
        buffer_partner_excel = io.BytesIO()
        with pd.ExcelWriter(buffer_partner_excel, engine='openpyxl') as writer:
            partner_final.to_excel(writer, index=False, sheet_name=f'Abrechnung_{selected_partner}')
            
        grp_label = "Gruppe A (Direktabrechnung Evelyn)" if is_grp_a else "Gruppe B (Partner-Abrechnung an Dich)"
        st.download_button(
            label=f"📥 Excel-Abrechnung für Partner '{selected_partner}' [{grp_label}] herunterladen",
            data=buffer_partner_excel.getvalue(),
            file_name=f"Abrechnung_Partner_{selected_partner}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    except Exception as e:
        st.error(f"Fehler beim Verarbeiten der Dateien: {e}")
