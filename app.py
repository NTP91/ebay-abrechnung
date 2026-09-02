import streamlit as st
import pandas as pd
import io
import re
import requests
import json
import datetime

st.set_page_config(page_title="eBay Payout & Lexoffice Direct-Upload", layout="wide")

st.title("⚡ eBay Payout & Lexoffice Direct-Upload")

# Lexoffice API Konfiguration
lexoffice_api_key = "Wciy230Sw_pNI7.yFDyNsWuvvXIB2sxJ2MKLk2jfMowyWJKU"

# Workflow-Anleitung ganz oben
with st.expander("📖 **Anleitung & Workflow-Erklärung anzeigen**", expanded=True):
    st.markdown("""
    ### **Ablauf & Workflow:**
    1. **Auszahlungsberichte hochladen:** Lade deine eBay-Auszahlungsberichte (CSV) oben hoch.
    2. **Gruppe A (Partner / Zwischenmann-Geschäft):**
       - **Partner-Abrechnung:** Lade die einzelnen CSVs für die Partner (NB, PP, BA, MK, 001) herunter. Die Partner stellen ihre Rechnung an dich (**3,5 % Rabatt**).
       - **Abrechnung an Evelyn:** Lade die Gesamtübersicht für Gruppe A herunter und stelle die Gesamtrechnung an Evelyn (**0,5 % Rabatt**).
    3. **Gruppe B (Direkt-Abwicklung über Evelyn):**
       - Rechnungen werden direkt per Button unten für Kundennummer `16335` in Lexoffice angelegt (**0,5 % Rabatt**).
    """)

# Sidebar Einstellungen
st.sidebar.header("Einstellungen")
target_customer_num = st.sidebar.text_input("Ziel-Kundennummer in Lexoffice:", value="16335")

# Uploads
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
    match = re.match(r'^([A-Z0-9]+)', raw_prefix)
    if match:
        return match.group(1)
    return raw_prefix

def get_lexoffice_contact_id_exact(api_key, target_customer_number):
    headers = {"Authorization": f"Bearer {api_key}"}
    page = 0
    target_str = str(target_customer_number).strip()
    
    while page < 10:
        res = requests.get(f"https://api.lexoffice.io/v1/contacts?page={page}&size=250", headers=headers)
        if res.status_code == 200:
            data = res.json()
            content = data.get('content', [])
            if not content:
                break
                
            for contact in content:
                cust_num = str(contact.get('roles', {}).get('customer', {}).get('number', '')).strip()
                supp_num = str(contact.get('roles', {}).get('vendor', {}).get('number', '')).strip()
                
                if cust_num == target_str or supp_num == target_str:
                    return contact['id']
            page += 1
        else:
            break
    return None

def create_lexoffice_invoice(api_key, contact_id, line_items, remark):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    now_utc_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    
    payload = {
        "archived": False,
        "voucherDate": now_utc_iso,
        "address": {"contactId": contact_id},
        "lineItems": line_items,
        "totalPrice": {"currency": "EUR"},
        "taxConditions": {"taxType": "net"},
        "shippingConditions": {
            "shippingDate": now_utc_iso,
            "shippingEndDate": now_utc_iso,
            "shippingType": "serviceperiod"
        },
        "remark": remark
    }
    res = requests.post("https://api.lexoffice.io/v1/invoices?finalize=false", headers=headers, json=payload)
    if res.status_code in [200, 201]:
        return res.json()['id']
    else:
        st.error(f"Fehler beim Erstellen der Rechnung in Lexoffice: {res.text}")
        return None

GROUP_A_PREFIXES = ['PP', 'BA', 'MK', '001', 'NB']

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
        
        df_payout['Bestellnummer_Match'] = df_payout['Bestellnummer'].apply(clean_order_number)
        df_payout = df_payout.drop_duplicates(subset=['Bestellnummer_Match', 'Datum der Transaktionserstellung', 'Betrag abzügl. Kosten'])
        
        df_payout['eBay_Brutto'] = df_payout['Betrag abzügl. Kosten'].apply(parse_german_float)
        
        # Nur positive Verkäufe berücksichtigen
        df_payout = df_payout[df_payout['eBay_Brutto'] > 0].copy()
        
        df_payout['eBay_Netto'] = (df_payout['eBay_Brutto'] / 1.19).round(2)
        df_payout['SKU'] = df_payout['Bestandseinheit'].fillna('OHNE_SKU').astype(str).str.strip()
        df_payout['SKU_Prefix'] = df_payout['SKU'].apply(extract_partner_prefix)
        df_payout['Menge'] = 1

        df_payout['Gruppe'] = df_payout['SKU_Prefix'].apply(
            lambda p: 'Gruppe A (Direkt)' if p in GROUP_A_PREFIXES else ('Ohne Zuordnung' if p == 'OHNE_SKU' else 'Gruppe B (Über Dich)')
        )

        st.markdown("---")
        st.subheader("📊 Übersicht aller Transaktionen nach Gruppen")

        # Gruppe A an ERSTER Stelle
        tab_a, tab_b, tab_none, tab_all = st.tabs([
            "Gruppe A (Direkt / Partner)", 
            "Gruppe B (Über Dich)", 
            "Ohne Zuordnung", 
            "Alle Daten"
        ])

        df_grp_a = df_payout[df_payout['Gruppe'] == 'Gruppe A (Direkt)'].copy()
        df_grp_b = df_payout[df_payout['Gruppe'] == 'Gruppe B (Über Dich)'].copy()
        df_grp_none = df_payout[df_payout['Gruppe'] == 'Ohne Zuordnung'].copy()

        with tab_a:
            st.info("""
            **Gruppe A (Partner-Geschäft mit Zwischenmann):**
            - **An Partner senden:** Lade unten pro Partner die CSV herunter. Der Partner stellt seine Rechnung mit **3,5 % Rabatt** an dich.
            - **An Evelyn berechnen:** Lade die Gesamtübersicht Gruppe A herunter und erstelle deine Gesamtrechnung an Evelyn mit **0,5 % Rabatt**.
            """)
            st.write(f"**Anzahl Gesamt:** {len(df_grp_a)} Positionen | **Gesamtsumme Netto:** {df_grp_a['eBay_Netto'].sum():.2f} €")
            
            # Button für Gesamt-Download Gruppe A (für Evelyn mit 0,5% Rabatt)
            if not df_grp_a.empty:
                df_grp_a_evelyn = df_grp_a[['Bestellnummer', 'SKU', 'eBay_Netto', 'Datum der Transaktionserstellung']].copy()
                df_grp_a_evelyn['Netto_abzgl_0_5_Prozent'] = (df_grp_a_evelyn['eBay_Netto'] * 0.995).round(2)
                
                csv_data_a_all = df_grp_a_evelyn.to_csv(index=False, sep=';').encode('utf-8')
                st.download_button(
                    label="📥 Gesamtexport Gruppe A für Evelyn (0,5 % Rabatt) herunterladen (CSV)",
                    data=csv_data_a_all,
                    file_name="Abrechnung_Gruppe_A_Gesamt_fuer_Evelyn.csv",
                    mime="text/csv",
                    key="dl_grp_a_all"
                )
            
            st.markdown("---")
            st.subheader("📁 Aufschlüsselung für einzelne Partner (3,5 % Rabatt)")
            
            partner_prefixes = df_grp_a['SKU_Prefix'].unique()
            if len(partner_prefixes) > 0:
                for prefix in sorted(partner_prefixes):
                    df_partner = df_grp_a[df_grp_a['SKU_Prefix'] == prefix].copy()
                    df_partner['Netto_abzgl_3_5_Prozent'] = (df_partner['eBay_Netto'] * 0.965).round(2)
                    
                    st.markdown(f"#### 📦 Partner: **{prefix}**")
                    st.write(f"Anzahl: {len(df_partner)} | Summe Netto: {df_partner['eBay_Netto'].sum():.2f} € | Summe Auszahlung Partner (-3,5 %): {df_partner['Netto_abzgl_3_5_Prozent'].sum():.2f} €")
                    
                    export_cols = ['Bestellnummer', 'SKU', 'eBay_Netto', 'Netto_abzgl_3_5_Prozent', 'Datum der Transaktionserstellung']
                    st.dataframe(df_partner[export_cols], use_container_width=True)
                    
                    csv_data = df_partner[export_cols].to_csv(index=False, sep=';').encode('utf-8')
                    st.download_button(
                        label=f"📥 Partner-CSV herunterladen für ({prefix})",
                        data=csv_data,
                        file_name=f"Abrechnung_Partner_{prefix}.csv",
                        mime="text/csv",
                        key=f"dl_{prefix}"
                    )
                    st.markdown("---")
            else:
                st.write("Keine Positionen für Gruppe A vorhanden.")

        with tab_b:
            st.info("""
            **Gruppe B (Über Dich / Evelyn Kukulan Onlinehandel):**
            Diese Artikel werden direkt über deinen Account/Kunden abgewickelt. Die Rechnungen hierfür werden direkt per Button unten an **Lexoffice (Kundennummer 16335)** übertragen (mit 0,5 % Rabatt).
            """)
            st.write(f"**Anzahl:** {len(df_grp_b)} Positionen | **Summe Netto:** {df_grp_b['eBay_Netto'].sum():.2f} €")
            st.dataframe(df_grp_b[['Bestellnummer', 'SKU', 'eBay_Netto', 'Datum der Transaktionserstellung']], use_container_width=True)

        with tab_none:
            st.warning("""
            **Ohne Zuordnung:**
            Bestellungen ohne bekannte SKU oder Prefix. Diese müssen manuell geprüft werden.
            """)
            st.write(f"**Anzahl:** {len(df_grp_none)} Positionen | **Summe Netto:** {df_grp_none['eBay_Netto'].sum():.2f} €")
            st.dataframe(df_grp_none[['Bestellnummer', 'SKU', 'eBay_Netto', 'Datum der Transaktionserstellung']], use_container_width=True)

        with tab_all:
            st.write(f"**Gesamtanzahl:** {len(df_payout)} Positionen | **Gesamtsumme Netto:** {df_payout['eBay_Netto'].sum():.2f} €")
            st.dataframe(df_payout[['Bestellnummer', 'SKU', 'Gruppe', 'eBay_Netto', 'Datum der Transaktionserstellung']], use_container_width=True)

        # Lexoffice-Aktion für Gruppe B
        st.markdown("---")
        st.subheader("🚀 Lexoffice Direct-Upload (Nur Gruppe B)")

        if not df_grp_b.empty:
            if st.button("🚀 JETZT GRUPPE B AUTOMATISCH IN LEXOFFICE ANLEGEN", type="primary"):
                with st.spinner(f"Suche exakt Kundennummer {target_customer_num} in Lexoffice..."):
                    contact_id = get_lexoffice_contact_id_exact(lexoffice_api_key, target_customer_num)
                
                if not contact_id:
                    st.error(f"❌ Kunde mit Kundennummer '{target_customer_num}' wurde in Lexoffice nicht gefunden! Vorgang abgebrochen.")
                else:
                    line_items = []
                    for _, r in df_grp_b.iterrows():
                        title_str = str(r.get('Artikelbezeichnung', '')).strip()
                        if not title_str or title_str == 'nan':
                            title_str = r['SKU']

                        description_str = f"eBay Bestellnummer: {r['Bestellnummer']}"
                        
                        line_items.append({
                            "type": "custom",
                            "name": title_str,
                            "description": description_str,
                            "quantity": 1,
                            "unitName": "Stück",
                            "unitPrice": {
                                "currency": "EUR", 
                                "netAmount": round(r['eBay_Netto'], 2), 
                                "taxRatePercentage": 19
                            },
                            "discountPercentage": 0.5
                        })
                    
                    remark_text = "Einzelabrechnung der eBay-Bestellungen laut Auszahlungsbericht."
                    
                    with st.spinner("Erstelle Rechnungsentwurf in Lexoffice..."):
                        inv_id = create_lexoffice_invoice(lexoffice_api_key, contact_id, line_items, remark_text)
                    
                    if inv_id:
                        st.balloons()
                        st.success(f"🎉 Erfolgreich angelegt für Kundennummer {target_customer_num}! (Entwurfs-ID: {inv_id})")
        else:
            st.info("Keine Positionen für Gruppe B in den hochgeladenen Dateien enthalten.")

    except Exception as e:
        st.error(f"Fehler bei der Verarbeitung: {e}")
