import streamlit as st
import pandas as pd
import requests
import io

# --- PAGE CONFIG ---
st.set_page_config(
    page_title="eBay Payment Tool",
    page_icon="📋",
    layout="wide"
)

# Custom Styling für Buttons
st.markdown("""
<style>
    div.stButton > button[kind="primary"] {
        background-color: #FF4B4B !important;
        color: white !important;
        border: none !important;
        border-radius: 6px !important;
        font-weight: 500 !important;
    }
    h2 {
        font-size: 1.5rem !important;
        font-weight: 600 !important;
        color: #1E293B !important;
    }
</style>
""", unsafe_allow_html=True)


# --- LEXOFFICE API LOGIK ---
def create_lexoffice_draft(api_key, customer_id, invoice_date, amount, tax_rate=19.0):
    url = "https://api.lexoffice.io/v1/invoices"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    payload = {
        "archived": False,
        "voucherDate": str(invoice_date),
        "address": {"contactId": customer_id},
        "lineItems": [
            {
                "type": "custom",
                "name": "eBay Abrechnung (Gruppe B)",
                "quantity": 1,
                "unitName": "Pauschal",
                "unitPrice": {
                    "currency": "EUR",
                    "netAmount": round(amount, 2),
                    "taxRatePercentage": tax_rate
                }
            }
        ]
    }
    
    try:
        res = requests.post(url, json=payload, headers=headers)
        if res.status_code in [200, 201]:
            return True, "Rechnungsentwurf erfolgreich erstellt!"
        return False, f"Fehler von Lexoffice ({res.status_code}): {res.text}"
    except Exception as e:
        return False, f"Verbindungsfehler: {str(e)}"


# --- HILFSFUNKTIONEN FÜR VERARBEITUNG ---
def parse_single_file(uploaded_file):
    try:
        if uploaded_file.name.endswith(('.xlsx', '.xls')):
            return pd.read_excel(uploaded_file)
        
        content = uploaded_file.read().decode('utf-8', errors='ignore')
        lines = content.splitlines()
        
        header_idx = 0
        for idx, line in enumerate(lines):
            line_low = line.lower()
            if any(k in line_low for k in ['bestandseinheit', 'betrag abzügl. kosten', 'custom label', 'net amount']):
                header_idx = idx
                break
                
        file_buffer = io.StringIO(content)
        df = pd.read_csv(file_buffer, skiprows=header_idx, sep=";", on_bad_lines="skip")
        
        if len(df.columns) <= 2:
            file_buffer.seek(0)
            df = pd.read_csv(file_buffer, skiprows=header_idx, sep=",", on_bad_lines="skip")
            
        return df
    except Exception:
        return None


def clean_sku_prefix(val):
    s = str(val).strip().upper()
    
    # Trennung am Schrägstrich
    if '/' in s:
        s = s.split('/')[0].strip()
        
    # Spezifische Zusammenfassung für MH (MH, MH43, MH 44 etc. -> MH)
    if s.startswith('MH'):
        return 'MH'
        
    return s


def process_uploaded_files(uploaded_files):
    if not uploaded_files:
        return None, None
        
    dfs = []
    for f in uploaded_files:
        parsed = parse_single_file(f)
        if parsed is not None and not parsed.empty:
            parsed.columns = [str(c).strip().replace('"', '') for c in parsed.columns]
            parsed = parsed.loc[:, ~parsed.columns.duplicated()]
            
            col_map = {}
            for col in parsed.columns:
                col_low = col.lower()
                if col_low in ['bestandseinheit', 'custom label', 'sku', 'artikelnummer']:
                    col_map[col] = 'Custom Label'
                elif col_low in ['betrag abzügl. kosten', 'net amount', 'netto', 'auszahlung']:
                    col_map[col] = 'Net amount'
                elif col_low in ['bestellnummer', 'transaction id', 'transaktionsnummer']:
                    col_map[col] = 'Transaction ID'
            
            parsed = parsed.rename(columns=col_map)
            parsed = parsed.loc[:, ~parsed.columns.duplicated()]
            
            if 'Custom Label' in parsed.columns and 'Net amount' in parsed.columns:
                dfs.append(parsed)

    if not dfs:
        return None, "SKU- oder Netto-Spalte wurde nicht erkannt."
        
    df_raw = pd.concat(dfs, ignore_index=True)
    df_raw = df_raw.loc[:, ~df_raw.columns.duplicated()]

    if 'Transaction ID' in df_raw.columns:
        df = df_raw.drop_duplicates(subset=['Transaction ID', 'Custom Label']).copy()
    else:
        df = df_raw.drop_duplicates().copy()

    df['Net amount'] = df['Net amount'].astype(str).str.replace('€', '').str.replace(' ', '').str.replace(',', '.')
    df['Net amount'] = pd.to_numeric(df['Net amount'], errors='coerce').fillna(0)

    df['SKU_Prefix'] = df['Custom Label'].apply(clean_sku_prefix)

    # Gruppen-Logik
    gruppe_a_prefixes = ['PP', 'BA', 'MK', '001']
    def assign_group(val):
        if not val or val in ['NAN', 'NONE', '', '—', '--', '-']:
            return 'Ohne Zuordnung'
        for p in gruppe_a_prefixes:
            if val.startswith(p):
                return 'Gruppe A'
        return 'Gruppe B'

    df['Gruppe'] = df['SKU_Prefix'].apply(assign_group)
    return df, None


# --- SIDEBAR ---
with st.sidebar:
    st.header("⚙️ Lexware Office Einstellungen")
    api_key = st.text_input("Lexware API-Key", type="password", key="lex_key")
    customer_id = st.text_input("Kundennummer / Contact-ID", value="16335")
    invoice_date = st.date_input("Rechnungsdatum")
    tax_rate = st.number_input("Umsatzsteuer (%)", value=19.0, step=0.5)


# --- UPLOAD SECTION ---
st.title("📋 eBay Payment Tool")
ebay_files = st.file_uploader("eBay-Auszahlungsdateien hochladen (CSV/XLSX)", type=["csv", "xlsx"], accept_multiple_files=True)

st.markdown("---")

if ebay_files:
    df, error_msg = process_uploaded_files(ebay_files)
    
    if error_msg:
        st.warning(error_msg)
    elif df is not None and not df.empty:
        
        # =========================================================
        # 1. GRUPPE B – GESAMTABRECHNUNG FÜR EVELYN (ÜBER DICH)
        # =========================================================
        st.header("📊 1. Gruppe B – Gesamtabrechnung für Evelyn (Über DICH)")
        st.info("💡 **Verwendungszweck:** Diese Rechnung nutzt du für die Abrechnung gegenüber Evelyn. Sie enthält NUR die Umsätze aus Gruppe B, die über dich verteilt werden. Evelyn behält 0,5 % Provision.")
        
        df_b = df[df['Gruppe'] == 'Gruppe B']
        
        if not df_b.empty:
            summary_b = df_b.groupby('SKU_Prefix').agg(
                Anzahl_Transaktionen=('Net amount', 'count'),
                eBay_Brutto_Gesamt=('Net amount', 'sum')
            ).reset_index()
            
            summary_b['Evelyn_Provision_0_5'] = summary_b['eBay_Brutto_Gesamt'] * 0.005
            summary_b['Auszahlung_von_Evelyn_an_Dich'] = summary_b['eBay_Brutto_Gesamt'] - summary_b['Evelyn_Provision_0_5']
            summary_b['Deine_Marge_3_0'] = summary_b['eBay_Brutto_Gesamt'] * 0.030
            
            tot_trans_b = summary_b['Anzahl_Transaktionen'].sum()
            tot_brutto_b = summary_b['eBay_Brutto_Gesamt'].sum()
            tot_prov_b = summary_b['Evelyn_Provision_0_5'].sum()
            tot_auszahlung_b = summary_b['Auszahlung_von_Evelyn_an_Dich'].sum()
            tot_marge_b = summary_b['Deine_Marge_3_0'].sum()

            total_b = pd.DataFrame([{
                'SKU_Prefix': 'GESAMTSUMME (Gruppe B)',
                'Anzahl_Transaktionen': tot_trans_b,
                'eBay_Brutto_Gesamt': tot_brutto_b,
                'Evelyn_Provision_0_5': tot_prov_b,
                'Auszahlung_von_Evelyn_an_Dich': tot_auszahlung_b,
                'Deine_Marge_3_0': tot_marge_b
            }])
            
            display_b = pd.concat([summary_b, total_b], ignore_index=True)
            
            formatted_b = display_b.copy()
            for col in ['eBay_Brutto_Gesamt', 'Evelyn_Provision_0_5', 'Auszahlung_von_Evelyn_an_Dich', 'Deine_Marge_3_0']:
                formatted_b[col] = formatted_b[col].apply(lambda x: f"{x:,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.'))
            
            st.dataframe(formatted_b, use_container_width=True, hide_index=False)
            
            col_btn1, col_btn2 = st.columns([1, 1])
            
            with col_btn1:
                buffer_b = io.BytesIO()
                with pd.ExcelWriter(buffer_b, engine='openpyxl') as writer:
                    display_b.to_excel(writer, index=False, sheet_name='Gruppe_B')
                
                st.download_button(
                    label="👛 Gesamtabrechnung Gruppe B für Evelyn herunterladen (Excel)",
                    data=buffer_b.getvalue(),
                    file_name="Gesamtabrechnung_Gruppe_B_Evelyn.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    type="primary"
                )
            
            with col_btn2:
                if st.button("🚀 Rechnungsentwurf in Lexware Office anlegen", key="lex_draft_btn"):
                    if not api_key:
                        st.error("Bitte gib zuerst in der linken Seitenleiste deinen Lexware API-Key ein.")
                    else:
                        success, msg = create_lexoffice_draft(
                            api_key=api_key,
                            customer_id=customer_id,
                            invoice_date=invoice_date,
                            amount=tot_auszahlung_b,
                            tax_rate=tax_rate
                        )
                        if success:
                            st.success(f"{msg} Betrag: {tot_auszahlung_b:,.2f} €")
                        else:
                            st.error(msg)
        else:
            st.write("Keine Datensätze für Gruppe B vorhanden.")

        st.markdown("<br><br>", unsafe_allow_html=True)

        # =========================================================
        # 2. GRUPPE A – DIREKTABRECHNUNGEN FÜR EVELYN (PP, BA, MK, 001)
        # =========================================================
        st.header("🏷️ 2. Gruppe A – Direktabrechnungen für Evelyn (PP, BA, MK, 001)")
        st.info("💡 **Verwendungszweck:** Diese Partner rechnen mit 0,5 % Provision direkt mit Evelyn ab (laufen nicht über deine Marge).")
        
        df_a = df[df['Gruppe'] == 'Gruppe A']
        
        if not df_a.empty:
            summary_a = df_a.groupby('SKU_Prefix').agg(
                Anzahl_Transaktionen=('Net amount', 'count'),
                eBay_Brutto_Gesamt=('Net amount', 'sum')
            ).reset_index()
            
            summary_a['Evelyn_Provision_0_5'] = summary_a['eBay_Brutto_Gesamt'] * 0.005
            summary_a['Direkt_Auszahlung_Evelyn'] = summary_a['eBay_Brutto_Gesamt'] - summary_a['Evelyn_Provision_0_5']
            
            total_a = pd.DataFrame([{
                'SKU_Prefix': 'GESAMTSUMME (Gruppe A)',
                'Anzahl_Transaktionen': summary_a['Anzahl_Transaktionen'].sum(),
                'eBay_Brutto_Gesamt': summary_a['eBay_Brutto_Gesamt'].sum(),
                'Evelyn_Provision_0_5': summary_a['Evelyn_Provision_0_5'].sum(),
                'Direkt_Auszahlung_Evelyn': summary_a['Direkt_Auszahlung_Evelyn'].sum()
            }])
            
            display_a = pd.concat([summary_a, total_a], ignore_index=True)
            
            formatted_a = display_a.copy()
            for col in ['eBay_Brutto_Gesamt', 'Evelyn_Provision_0_5', 'Direkt_Auszahlung_Evelyn']:
                formatted_a[col] = formatted_a[col].apply(lambda x: f"{x:,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.'))
            
            st.dataframe(formatted_a, use_container_width=True, hide_index=False)
            
            buffer_a = io.BytesIO()
            with pd.ExcelWriter(buffer_a, engine='openpyxl') as writer:
                display_a.to_excel(writer, index=False, sheet_name='Gruppe_A')
            
            st.download_button(
                label="📥 Übersicht Gruppe A für Evelyn herunterladen (Excel)",
                data=buffer_a.getvalue(),
                file_name="Uebersicht_Gruppe_A_Evelyn.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        else:
            st.write("Keine Datensätze für Gruppe A vorhanden.")
else:
    st.info("Bitte lade oben eine oder mehrere eBay-Auszahlungsdateien hoch.")
