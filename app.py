import streamlit as st
import pandas as pd
import requests
import io

# --- PAGE CONFIG & CUSTOM CSS ---
st.set_page_config(
    page_title="eBay Payment Tool",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom Styling
st.markdown("""
<style>
    .main-title { font-size: 2.2rem; font-weight: 700; color: #1E293B; margin-bottom: 0.2rem; }
    .sub-title { font-size: 1.0rem; color: #64748B; margin-bottom: 1.5rem; }
    
    div[data-testid="stMetric"] {
        background-color: #F8FAFC;
        border: 1px solid #E2E8F0;
        padding: 15px;
        border-radius: 10px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }
    div[data-testid="stMetricLabel"] { font-size: 0.85rem !important; color: #475569 !important; font-weight: 600; }
    div[data-testid="stMetricValue"] { font-size: 1.5rem !important; color: #0F172A !important; font-weight: 700; }

    .stTabs [data-baseweb="tab-list"] { gap: 8px; }
    .stTabs [data-baseweb="tab"] { height: 45px; border-radius: 6px 6px 0px 0px; padding: 10px; }
    .stButton>button { border-radius: 8px; font-weight: 600; }
</style>
""", unsafe_allow_html=True)


# --- HILFSFUNKTIONEN ---
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


def clean_sku(val):
    s = str(val).strip()
    if '/' in s:
        s = s.split('/')[0].strip()
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
            
            # Mapping auf einheitliche Namen
            col_map = {}
            for col in parsed.columns:
                col_low = col.lower()
                if col_low in ['bestandseinheit', 'custom label', 'sku', 'artikelnummer']:
                    col_map[col] = 'Custom Label'
                elif col_low in ['betrag abzügl. kosten', 'net amount', 'netto', 'auszahlung']:
                    col_map[col] = 'Net amount'
                elif col_low in ['bestellnummer', 'transaction id', 'transaktionsnummer']:
                    col_map[col] = 'Transaction ID'
                elif col_low in ['datum der transaktionserstellung', 'date', 'datum']:
                    col_map[col] = 'Datum'
                elif col_low in ['typ', 'type']:
                    col_map[col] = 'Typ'
            
            parsed = parsed.rename(columns=col_map)
            parsed = parsed.loc[:, ~parsed.columns.duplicated()]
            
            if 'Custom Label' in parsed.columns and 'Net amount' in parsed.columns:
                dfs.append(parsed)

    if not dfs:
        return None, "SKU- oder Netto-Spalte wurde nicht erkannt."
        
    df_raw = pd.concat(dfs, ignore_index=True)
    df_raw = df_raw.loc[:, ~df_raw.columns.duplicated()]

    # Duplikate filtern
    if 'Transaction ID' in df_raw.columns:
        df = df_raw.drop_duplicates(subset=['Transaction ID', 'Custom Label']).copy()
    else:
        df = df_raw.drop_duplicates().copy()

    # Beträge säubern
    df['Net amount'] = df['Net amount'].astype(str).str.replace('€', '').str.replace(' ', '').str.replace(',', '.')
    df['Net amount'] = pd.to_numeric(df['Net amount'], errors='coerce').fillna(0)

    # SKU kürzen auf das Kürzel vor dem '/'
    df['Partner_SKU'] = df['Custom Label'].apply(clean_sku)

    # Gruppen zuweisen
    gruppe_a = ['PP', 'BA', 'MK', '001']
    def assign_group(val):
        sku = str(val).strip().upper()
        if not sku or sku in ['NAN', 'NONE', '', '—', '--', '-']:
            return 'Ohne Zuordnung'
        for p in gruppe_a:
            if sku.startswith(p):
                return 'Gruppe A'
        return 'Gruppe B'

    df['Gruppe'] = df['Partner_SKU'].apply(assign_group)

    # RELEVANTE SPALTEN AUSWÄHLEN & SORTIEREN
    display_cols = []
    preferred_order = ['Datum', 'Typ', 'Partner_SKU', 'Net amount', 'Transaction ID', 'Gruppe']
    for c in preferred_order:
        if c in df.columns:
            display_cols.append(c)
    
    # Restliche vorhandene Spalten weglassen, um die Übersicht schlank zu halten
    df_clean = df[display_cols].copy()

    return df_clean, None


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
                "name": "eBay Abrechnung",
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
            return True, "Erfolgreich"
        return False, res.text
    except Exception as e:
        return False, str(e)


# --- HEADER & SIDEBAR ---
st.markdown('<div class="main-title">📋 eBay Payment Tool</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-title">Partnerzuordnung, Abrechnung und Lexware-Office-Rechnungsentwurf</div>', unsafe_allow_html=True)

with st.sidebar:
    st.header("⚙️ Lexware Office Integration")
    api_key = st.text_input("API-Key", type="password", key="lex_api_key")
    customer_id = st.text_input("Kundennummer", value="16335")
    invoice_date = st.date_input("Rechnungsdatum")
    tax_rate = st.number_input("Umsatzsteuer (%)", value=19.0, step=0.5)
    st.markdown("---")
    st.caption("v2.6 · Schlankes Layout")


# --- UPLOAD SECTION ---
col_up1, col_up2 = st.columns(2)
with col_up1:
    ebay_files = st.file_uploader("eBay-Auszahlungen", type=["csv", "xlsx"], accept_multiple_files=True)
with col_up2:
    wahan_files = st.file_uploader("Wahan-Bestellübersicht (optional)", type=["csv", "xlsx"], accept_multiple_files=True)

st.markdown("<br>", unsafe_allow_html=True)


# --- HAUPTVERARBEITUNG ---
if ebay_files:
    df, error_msg = process_uploaded_files(ebay_files)
    
    if error_msg:
        st.warning(error_msg)
    
    if df is not None and not df.empty:
        sku_col = 'Partner_SKU'
        netto_col = 'Net amount'

        tab1, tab2, tab3, tab4 = st.tabs([
            "🤝 Gruppe A (Direkt)", 
            "👤 Gruppe B (Über Dich)", 
            "❓ Ohne Zuordnung", 
            "📊 Alle Daten"
        ])

        # TAB 1: GRUPPE A
        with tab1:
            st.header("Direkt-Partner")
            st.caption("PP, BA, MK und 001 · Netto-Umsatz abzüglich 0,5 % Provision · kein Lexware-Upload")
            df_a = df[df['Gruppe'] == 'Gruppe A']
            
            if df_a.empty:
                st.info("Keine Datensätze für Gruppe A vorhanden.")
            else:
                for sku in sorted(df_a[sku_col].astype(str).unique()):
                    sub = df_a[df_a[sku_col].astype(str) == sku]
                    netto = sub[netto_col].sum()
                    prov = netto * 0.005
                    ausz = netto - prov
                    
                    st.subheader(f"Partner / SKU: {sku}")
                    m1, m2, m3 = st.columns(3)
                    m1.metric("Netto-Umsatz", f"{netto:,.2f} €")
                    m2.metric("Provision (0,5 %)", f"{prov:,.2f} €")
                    m3.metric("Auszahlungsbetrag", f"{ausz:,.2f} €")
                    
                    st.dataframe(sub, use_container_width=True, hide_index=True)
                    csv = sub.to_csv(index=False).encode('utf-8')
                    st.download_button(f"📥 CSV {sku}", csv, f"Abrechnung_{sku}.csv", "text/csv", key=f"dl_a_{sku}")
                    st.markdown("---")

        # TAB 2: GRUPPE B
        with tab2:
            st.header("Evelyn-Gesamtübersicht")
            st.caption("NB und alle übrigen Standard-SKUs · 0,5 % Rabatt")
            df_b = df[df['Gruppe'] == 'Gruppe B']
            
            if df_b.empty:
                st.info("Keine Datensätze für Gruppe B vorhanden.")
            else:
                netto_b = df_b[netto_col].sum()
                rabatt_b = netto_b * 0.005
                endbetrag_b = netto_b - rabatt_b
                
                b1, b2, b3 = st.columns(3)
                b1.metric("Netto-Umsatz", f"{netto_b:,.2f} €")
                b2.metric("Nach 0,5 % Rabatt", f"{endbetrag_b:,.2f} €")
                b3.metric("Rabatt-Betrag", f"{rabatt_b:,.2f} €")
                
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("🚀 Rechnungsentwurf in Lexware Office erstellen", type="primary"):
                    if not api_key:
                        st.error("Bitte gib zuerst links den Lexware-Office-API-Key ein.")
                    else:
                        res, msg = create_lexoffice_draft(api_key, customer_id, invoice_date, endbetrag_b, tax_rate)
                        if res:
                            st.success("Rechnungsentwurf erfolgreich in Lexware Office angelegt!")
                        else:
                            st.error(f"Fehler beim Upload: {msg}")
                
                st.markdown("---")
                st.subheader("Einzelabrechnungen für dich & Patrick")
                st.caption("Je SKU: Netto-Umsatz abzüglich 3,5 % Provision")
                
                for sku in sorted(df_b[sku_col].astype(str).unique()):
                    sub_b = df_b[df_b[sku_col].astype(str) == sku]
                    n_b = sub_b[netto_col].sum()
                    p_b = n_b * 0.035
                    a_b = n_b - p_b
                    
                    st.markdown(f"### SKU: **{sku}**")
                    x1, x2, x3 = st.columns(3)
                    x1.metric("Umsatz SKU", f"{n_b:,.2f} €")
                    x2.metric("Provision (3,5 %)", f"{p_b:,.2f} €")
                    x3.metric("Auszahlung", f"{a_b:,.2f} €")
                    
                    st.dataframe(sub_b, use_container_width=True, hide_index=True)
                    csv_b = sub_b.to_csv(index=False).encode('utf-8')
                    st.download_button(f"📥 CSV {sku}", csv_b, f"Abrechnung_3.5_{sku}.csv", "text/csv", key=f"dl_b_{sku}")
                    st.markdown("---")

        # TAB 3: OHNE ZUORDNUNG
        with tab3:
            st.header("Ohne Zuordnung")
            st.caption("Transaktionen ohne gültige SKU oder allgemeine Gebühren")
            df_none = df[df['Gruppe'] == 'Ohne Zuordnung']
            
            if df_none.empty:
                st.info("Alle Transaktionen konnten einer Gruppe zugeordnet werden!")
            else:
                sum_none = df_none[netto_col].sum()
                st.metric("Gesamtsumme ungeklärte Posten", f"{sum_none:,.2f} €")
                st.dataframe(df_none, use_container_width=True, hide_index=True)

        # TAB 4: ALLE DATEN
        with tab4:
            st.header("Alle zusammengefassten Daten")
            st.dataframe(df, use_container_width=True, hide_index=True)
else:
    st.info("👆 Bitte lade oben mindestens eine eBay-Auszahlungsdatei hoch.")
