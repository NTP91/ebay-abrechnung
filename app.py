import streamlit as st
import pandas as pd
import io
import re
import requests
import json
import datetime
import sqlite3
import os

st.set_page_config(page_title="eBay Payout & Lexoffice Direct-Upload", layout="wide")

# ==========================================
# DATENBANK-INITIALISIERUNG (SQLite)
# ==========================================
DB_FILE = "ebay_data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Tabelle für gespeicherte Artikelbezeichnungen / Bestellberichte
    c.execute('''
        CREATE TABLE IF NOT EXISTS order_items (
            order_id TEXT PRIMARY KEY,
            item_title TEXT,
            sku TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Tabelle für verarbeitete Payout-Transaktionen (Duplikate-Schutz)
    c.execute('''
        CREATE TABLE IF NOT EXISTS payout_history (
            transaction_key TEXT PRIMARY KEY,
            order_id TEXT,
            amount REAL,
            transaction_date TEXT,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def save_orders_to_db(df_orders):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    saved_count = 0
    for _, row in df_orders.iterrows():
        order_id = str(row.get('Match_Key', '')).strip()
        title = str(row.get('Title_Val', '')).strip()
        sku = str(row.get('SKU_Val', '')).strip()
        if order_id and title and title != '-':
            c.execute('''
                INSERT INTO order_items (order_id, item_title, sku)
                VALUES (?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    item_title=excluded.item_title,
                    sku=excluded.sku
            ''', (order_id, title, sku))
            saved_count += 1
    conn.commit()
    conn.close()
    return saved_count

def load_all_order_mappings():
    conn = sqlite3.connect(DB_FILE)
    df_map = pd.read_sql_query("SELECT order_id, item_title FROM order_items", conn)
    conn.close()
    return df_map.set_index('order_id')['item_title'].to_dict() if not df_map.empty else {}

def filter_already_processed_payouts(df_payout):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT transaction_key FROM payout_history")
    existing_keys = set(r[0] for r in c.fetchall())
    conn.close()

    def make_key(row):
        return f"{row['Bestellnummer_Match']}_{row['Datum der Transaktionserstellung']}_{row['eBay_Brutto']}"

    df_payout['Tx_Key'] = df_payout.apply(make_key, axis=1)
    
    df_new = df_payout[~df_payout['Tx_Key'].isin(existing_keys)].copy()
    already_processed_count = len(df_payout) - len(df_new)
    return df_new, already_processed_count

def mark_payouts_as_processed(df_payout):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    for _, row in df_payout.iterrows():
        c.execute('''
            INSERT OR IGNORE INTO payout_history (transaction_key, order_id, amount, transaction_date)
            VALUES (?, ?, ?, ?)
        ''', (row['Tx_Key'], row['Bestellnummer_Match'], row['eBay_Brutto'], str(row['Datum der Transaktionserstellung'])))
    conn.commit()
    conn.close()

def clean_order_number(val):
    if pd.isna(val):
        return ""
    s = str(val).strip()
    if s.endswith('.0'):
        s = s[:-2]
    s = re.sub(r'[^A-Za-z0-9]', '', s).upper()
    s = s.lstrip('0')
    return s

# ==========================================
# APP LAYOUT & SIDEBAR (Datenbank-Verwaltung)
# ==========================================
st.sidebar.header("⚙️ Einstellungen & Datenbank")
target_customer_num = st.sidebar.text_input("Ziel-Kundennummer in Lexoffice:", value="16335")

st.sidebar.markdown("---")
st.sidebar.subheader("🗄️ Lokale Datenbank")

# Upload für Bestellberichte direkt in die DB
uploaded_orders_db = st.sidebar.file_uploader(
    "📥 Bestellbericht in DB importieren:", 
    type=["xlsx", "xls", "csv"], 
    key="db_orders_upload"
)

if uploaded_orders_db:
    try:
        df_ref = None
        
        if uploaded_orders_db.name.endswith(('.xlsx', '.xls')):
            xls = pd.ExcelFile(uploaded_orders_db)
            df_raw = pd.read_excel(uploaded_orders_db, sheet_name=xls.sheet_names[0], header=None)
            
            header_idx = 0
            for idx, row in df_raw.iterrows():
                row_str = " ".join([str(val).lower() for val in row.values if pd.notna(val)])
                if "bestellnummer" in row_str or "order number" in row_str or "angebotstitel" in row_str:
                    header_idx = idx
                    break
            
            df_ref = pd.read_excel(uploaded_orders_db, sheet_name=xls.sheet_names[0], header=header_idx)
        else:
            content = uploaded_orders_db.getvalue().decode('utf-8', errors='ignore')
            lines = content.splitlines()
            header_idx = 0
            for i, line in enumerate(lines):
                line_lower = line.lower()
                if "bestellnummer" in line_lower or "order number" in line_lower or "angebotstitel" in line_lower:
                    header_idx = i
                    break
            df_ref = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])), sep=None, engine='python')

        ref_order_col = next((c for c in df_ref.columns if any(x in str(c).lower() for x in ['bestellnummer', 'order number', 'order id', 'bestell-nr'])), None)
        ref_title_col = next((c for c in df_ref.columns if any(x in str(c).lower() for x in ['angebotstitel', 'artikelbezeichnung', 'item title', 'title', 'bezeichnung']) and 'nummer' not in str(c).lower() and 'id' not in str(c).lower()), None)
        ref_sku_col = next((c for c in df_ref.columns if any(x in str(c).lower() for x in ['bestandseinheit', 'custom label', 'sku'])), None)

        if not ref_title_col:
            ref_title_col = next((c for c in df_ref.columns if 'artikel' in str(c).lower() or 'item' in str(c).lower()), None)

        if ref_order_col and ref_title_col:
            df_ref['Match_Key'] = df_ref[ref_order_col].apply(clean_order_number)
            df_ref['Title_Val'] = df_ref[ref_title_col]
            df_ref['SKU_Val'] = df_ref[ref_sku_col] if ref_sku_col else ''
            
            added = save_orders_to_db(df_ref)
            st.sidebar.success(f"✅ {added} Artikelbezeichnungen erfolgreich importiert!")
        else:
            st.sidebar.error(f"❌ Spalten nicht gefunden. Erkannt wurden: {list(df_ref.columns[:5])}")
    except Exception as ex:
        st.sidebar.error(f"Fehler beim DB-Import: {ex}")

# DB Aufklappbarer Bereich / Statistik
conn = sqlite3.connect(DB_FILE)
db_orders_count = pd.read_sql_query("SELECT COUNT(*) as cnt FROM order_items", conn)['cnt'].iloc[0]
db_payouts_count = pd.read_sql_query("SELECT COUNT(*) as cnt FROM payout_history", conn)['cnt'].iloc[0]
conn.close()

with st.sidebar.expander(f"📦 DB-Status ({db_orders_count} Artikel | {db_payouts_count} Payouts)"):
    st.write(f"• Gespeicherte Titel: **{db_orders_count}**")
    st.write(f"• Verarbeitete Payouts: **{db_payouts_count}**")
    
    if st.button("🔍 DB-Inhalt (Titel) anzeigen"):
        conn = sqlite3.connect(DB_FILE)
        df_show = pd.read_sql_query("SELECT order_id as Bestellnummer, item_title as Artikelname FROM order_items LIMIT 50", conn)
        conn.close()
        st.dataframe(df_show)

    if st.button("🗑️ Datenbank leeren", type="secondary"):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM order_items")
        c.execute("DELETE FROM payout_history")
        conn.commit()
        conn.close()
        st.experimental_rerun()

# ==========================================
# HAUPTANWENDUNG
# ==========================================
st.title("⚡ eBay Payout & Lexoffice Direct-Upload")

lexoffice_api_key = "Wciy230Sw_pNI7.yFDyNsWuvvXIB2sxJ2MKLk2jfMowyWJKU"

with st.expander("📖 **Anleitung & Workflow-Erklärung anzeigen**", expanded=False):
    st.markdown("""
    ### **Ablauf & Workflow:**
    1. **Bestellberichte vorbereiten:** Lade deinen Bestellbericht über die linke Seitenleiste in die Datenbank hoch (einmalig für den Zeitraum).
    2. **Auszahlungsberichte verarbeiten:** Lade hier unten deine eBay-Auszahlungsberichte (CSV) hoch. 
    3. **Automatische Prüfung:** Das System zieht sich Artikelnamen direkt aus der Datenbank und filtert bereits importierte Payouts automatisch heraus.
    """)

uploaded_payout = st.file_uploader("1. eBay Auszahlungsberichte hochladen (CSV)", type=["csv"], accept_multiple_files=True, key="payout")

def parse_german_float(val):
    if pd.isna(val) or val == '--' or str(val).strip() == '':
        return 0.0
    return float(str(val).replace('.', '').replace(',', '.'))

def extract_partner_prefix(sku):
    if pd.isna(sku) or str(sku).strip() in ['--', '']:
        return 'OHNE_SKU'
    sku_clean = str(sku).strip().upper()
    raw_prefix = sku_clean.split('/')[0].strip()
    if raw_prefix.startswith('001') or raw_prefix == '001':
        return '001'
    if raw_prefix.startswith('MH'):
        return 'MH'
    match = re.match(r'^([A-Z0-9]+)', raw_prefix)
    return match.group(1) if match else raw_prefix

def get_lexoffice_contact_id_exact(api_key, target_customer_number):
    headers = {"Authorization": f"Bearer {api_key}"}
    page = 0
    target_str = str(target_customer_number).strip()
    while page < 10:
        res = requests.get(f"https://api.lexoffice.io/v1/contacts?page={page}&size=250", headers=headers)
        if res.status_code == 200:
            content = res.json().get('content', [])
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
    return res.json()['id'] if res.status_code in [200, 201] else None

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
        df_payout = df_payout[df_payout['eBay_Brutto'] > 0].copy()
        
        df_payout, skipped_count = filter_already_processed_payouts(df_payout)
        if skipped_count > 0:
            st.warning(f"ℹ️ **{skipped_count} Transaktion(en)** wurden übersprungen, da sie bereits in einer früheren Sitzung verarbeitet wurden.")

        if df_payout.empty:
            st.info("Alle hochgeladenen Payouts wurden bereits früher verarbeitet.")
        else:
            df_payout['eBay_Netto'] = (df_payout['eBay_Brutto'] / 1.19).round(2)
            df_payout['SKU'] = df_payout['Bestandseinheit'].fillna('OHNE_SKU').astype(str).str.strip()
            df_payout['SKU_Prefix'] = df_payout['SKU'].apply(extract_partner_prefix)

            db_title_map = load_all_order_mappings()
            df_payout['Artikelname'] = df_payout['Bestellnummer_Match'].map(db_title_map).fillna('-')

            possible_qty_cols = ['Stückzahl', 'Menge', 'Anzahl', 'Quantity']
            found_qty_col = next((c for c in df_payout.columns if c.strip() in possible_qty_cols), None)
            df_payout['Stück'] = df_payout[found_qty_col].fillna(1) if found_qty_col else 1

            df_payout['Gruppe'] = df_payout['SKU_Prefix'].apply(
                lambda p: 'Gruppe A (Direkt)' if p in GROUP_A_PREFIXES else ('Ohne Zuordnung' if p == 'OHNE_SKU' else 'Gruppe B (Über Dich)')
            )

            st.markdown("---")
            st.subheader("📊 Übersicht aller Transaktionen nach Gruppen")

            tab_a, tab_b, tab_none, tab_all = st.tabs(["Gruppe A (Direkt)", "Gruppe B (Über Dich)", "Ohne Zuordnung", "Alle Daten"])

            df_grp_a = df_payout[df_payout['Gruppe'] == 'Gruppe A (Direkt)'].copy()
            df_grp_b = df_payout[df_payout['Gruppe'] == 'Gruppe B (Über Dich)'].copy()
            df_grp_none = df_payout[df_payout['Gruppe'] == 'Ohne Zuordnung'].copy()

            # TAB A
            with tab_a:
                st.write(f"**Anzahl Gesamt:** {len(df_grp_a)} Positionen | **Gesamtsumme Netto:** {df_grp_a['eBay_Netto'].sum():.2f} €")
                partner_prefixes_a = df_grp_a['SKU_Prefix'].unique()
                if len(partner_prefixes_a) > 0:
                    for prefix in sorted(partner_prefixes_a):
                        df_partner = df_grp_a[df_grp_a['SKU_Prefix'] == prefix].copy()
                        st.markdown(f"#### 📦 Partner: **{prefix}**")
                        export_cols_a = ['Bestellnummer', 'SKU', 'Artikelname', 'Stück', 'eBay_Netto', 'Datum der Transaktionserstellung']
                        st.dataframe(df_partner[export_cols_a], use_container_width=True)
                        csv_data = df_partner[export_cols_a].to_csv(index=False, sep=';').encode('utf-8')
                        st.download_button(
                            label=f"📥 CSV Exportieren für Partner ({prefix})",
                            data=csv_data,
                            file_name=f"Abrechnung_Partner_{prefix}.csv",
                            mime="text/csv",
                            key=f"dl_a_{prefix}"
                        )

            # TAB B
            with tab_b:
                st.write(f"**Anzahl Gesamt:** {len(df_grp_b)} Positionen | **Gesamtsumme Netto:** {df_grp_b['eBay_Netto'].sum():.2f} €")
                st.dataframe(df_grp_b[['Bestellnummer', 'SKU', 'Artikelname', 'Stück', 'eBay_Netto', 'Datum der Transaktionserstellung']], use_container_width=True)

                st.markdown("---")
                st.markdown("### 📥 Partner-Downloads & Aufschlüsselung (3,5 % Rabatt)")
                partner_prefixes_b = df_grp_b['SKU_Prefix'].unique()
                if len(partner_prefixes_b) > 0:
                    for prefix in sorted(partner_prefixes_b):
                        df_partner_b = df_grp_b[df_grp_b['SKU_Prefix'] == prefix].copy()
                        df_partner_b['Netto_abzgl_3_5_Prozent'] = (df_partner_b['eBay_Netto'] * 0.965).round(2)
                        with st.expander(f"📦 **Partner/Kürzel: {prefix}** — ({len(df_partner_b)} Positionen | Netto: {df_partner_b['eBay_Netto'].sum():.2f} € | **Auszahlung -3,5%: {df_partner_b['Netto_abzgl_3_5_Prozent'].sum():.2f} €**)"):
                            export_cols_b = ['Bestellnummer', 'SKU', 'Artikelname', 'Stück', 'eBay_Netto', 'Netto_abzgl_3_5_Prozent', 'Datum der Transaktionserstellung']
                            csv_data_b = df_partner_b[export_cols_b].to_csv(index=False, sep=';').encode('utf-8')
                            st.download_button(
                                label=f"📥 CSV Herunterladen für {prefix} (inkl. 3,5 % Rabatt)",
                                data=csv_data_b,
                                file_name=f"Abrechnung_Partner_{prefix}.csv",
                                mime="text/csv",
                                key=f"dl_b_{prefix}"
                            )
                            st.dataframe(df_partner_b[export_cols_b], use_container_width=True)

            with tab_none:
                st.dataframe(df_grp_none[['Bestellnummer', 'SKU', 'Artikelname', 'Stück', 'eBay_Netto', 'Datum der Transaktionserstellung']], use_container_width=True)

            with tab_all:
                st.dataframe(df_payout[['Bestellnummer', 'SKU', 'Artikelname', 'Stück', 'Gruppe', 'eBay_Netto', 'Datum der Transaktionserstellung']], use_container_width=True)

            # Lexoffice Direct-Upload Button
            st.markdown("---")
            st.subheader("🚀 Lexoffice Direct-Upload (Nur Gruppe B)")

            if not df_grp_b.empty:
                if st.button("🚀 JETZT GRUPPE B AUTOMATISCH IN LEXOFFICE ANLEGEN", type="primary"):
                    with st.spinner(f"Suche Kundennummer {target_customer_num} in Lexoffice..."):
                        contact_id = get_lexoffice_contact_id_exact(lexoffice_api_key, target_customer_num)
                    
                    if not contact_id:
                        st.error(f"❌ Kundennummer '{target_customer_num}' nicht gefunden.")
                    else:
                        line_items = []
                        for _, r in df_grp_b.iterrows():
                            title_str = str(r.get('Artikelname', '')).strip()
                            if not title_str or title_str == '-' or title_str == 'nan':
                                title_str = r['SKU']

                            line_items.append({
                                "type": "custom",
                                "name": title_str,
                                "description": f"eBay Bestellnummer: {r['Bestellnummer']}",
                                "quantity": int(r.get('Stück', 1)),
                                "unitName": "Stück",
                                "unitPrice": {
                                    "currency": "EUR", 
                                    "netAmount": round(r['eBay_Netto'], 2), 
                                    "taxRatePercentage": 19
                                },
                                "discountPercentage": 0.5
                            })
                        
                        with st.spinner("Erstelle Rechnungsentwurf in Lexoffice..."):
                            inv_id = create_lexoffice_invoice(lexoffice_api_key, contact_id, line_items, "Einzelabrechnung der eBay-Bestellungen laut Auszahlungsbericht.")
                        
                        if inv_id:
                            mark_payouts_as_processed(df_payout)
                            st.balloons()
                            st.success(f"🎉 Erfolgreich in Lexoffice angelegt! (Entwurfs-ID: {inv_id})")
    except Exception as e:
        st.error(f"Fehler bei der Verarbeitung: {e}")
