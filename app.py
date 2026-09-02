import streamlit as st
import pandas as pd
import io
import re
import requests
import json
import datetime

st.set_page_config(page_title="eBay Payout & Lexoffice Automatisierung", layout="wide")

st.title("⚡ eBay Payout & Lexoffice Direct-Upload")

# Lexoffice API Konfiguration
lexoffice_api_key = "Wciy230Sw_pNI7.yFDyNsWuvvXIB2sxJ2MKLk2jfMowyWJKU"
TARGET_CUSTOMER_NUMBER = "16335"

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
    match = re.match(r'^([A-Z]+)', raw_prefix)
    if match:
        return match.group(1)
    return raw_prefix

# Präzise Kontaktsuche in Lexoffice
def get_lexoffice_contact_id_exact(api_key, customer_number):
    headers = {"Authorization": f"Bearer {api_key}"}
    res = requests.get(f"https://api.lexoffice.io/v1/contacts?customerNumber={customer_number}", headers=headers)
    if res.status_code == 200:
        data = res.json()
        if data.get('content'):
            for contact in data['content']:
                if str(contact.get('roles', {}).get('customer', {}).get('number')) == str(customer_number):
                    return contact['id']
            return data['content'][0]['id']
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
        
        df_payout['Bestellnummer_Match'] = df_payout['Bestellnummer'].apply(clean_order_number)
        df_payout = df_payout.drop_duplicates(subset=['Bestellnummer_Match', 'Datum der Transaktionserstellung', 'Betrag abzügl. Kosten'])
        
        df_payout['eBay_Brutto'] = df_payout['Betrag abzügl. Kosten'].apply(parse_german_float)
        
        # 1. Gutschriften / Rückerstattungen herausfiltern
        df_payout = df_payout[df_payout['eBay_Brutto'] > 0].copy()
        
        df_payout['eBay_Netto'] = (df_payout['eBay_Brutto'] / 1.19).round(2)
        df_payout['SKU'] = df_payout['Bestandseinheit'].fillna('OHNE_SKU').astype(str).str.strip()
        df_payout['SKU_Prefix'] = df_payout['SKU'].apply(extract_partner_prefix)
        df_payout['Menge'] = 1

        df_payout['Gruppe'] = df_payout['SKU_Prefix'].apply(
            lambda p: 'Gruppe A (Direkt)' if p in GROUP_A_PREFIXES else ('Ohne Zuordnung' if p == 'OHNE_SKU' else 'Gruppe B (Über Dich)')
        )

        df_grp_b = df_payout[df_payout['Gruppe'] == 'Gruppe B (Über Dich)'].copy()

        if not df_grp_b.empty:
            st.markdown("---")
            st.subheader("📊 Vorschau der Einzelpositionen (nur Verkäufe)")
            st.dataframe(df_grp_b[['Datum der Transaktionserstellung', 'Bestellnummer', 'SKU', 'eBay_Netto']], use_container_width=True)

            st.markdown("---")
            if st.button("🚀 JETZT AUTOMATISCH IN LEXOFFICE ANLEGEN", type="primary"):
                with st.spinner(f"Suche exakte Kundennummer {TARGET_CUSTOMER_NUMBER} in Lexoffice..."):
                    contact_id = get_lexoffice_contact_id_exact(lexoffice_api_key, TARGET_CUSTOMER_NUMBER)
                
                if not contact_id:
                    st.error(f"Kunde mit Kundennummer '{TARGET_CUSTOMER_NUMBER}' wurde nicht in Lexoffice gefunden!")
                else:
                    line_items = []
                    for _, r in df_grp_b.iterrows():
                        # Artikelbezeichnung ohne "Artikel"-Präfix
                        title_str = str(r.get('Artikelbezeichnung', r['SKU']))
                        if pd.isna(title_str) or title_str.strip() == '' or title_str == 'nan':
                            title_str = r['SKU']

                        order_date = str(r.get('Datum der Transaktionserstellung', 'n/a'))
                        
                        # Beschreibungstext unter dem Artikel
                        description_str = (
                            f"eBay Bestellnummer: {r['Bestellnummer']}\n"
                            f"SKU: {r['SKU']} | Transaktionsdatum: {order_date}"
                        )
                        
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
                        st.success(f"🎉 Erfolgreich angelegt unter Kundennummer {TARGET_CUSTOMER_NUMBER}! (ID: {inv_id})")

    except Exception as e:
        st.error(f"Fehler: {e}")
