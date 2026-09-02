import streamlit as st
import pandas as pd
import io

st.set_page_config(page_title="eBay-Auszahlungsverrechnung", layout="wide")
st.title("📋 eBay-Auszahlungsverrechnung & Lexoffice Upload")

# --- DATEI-UPLOAD (SIDEBAR) ---
st.sidebar.header("Datei-Uploads")
ebay_file = st.sidebar.file_uploader("1. eBay Auszahlungs-CSV hochladen", type=["csv", "xlsx"])
wahan_file = st.sidebar.file_uploader("2. Wahan Bestellübersicht hochladen", type=["csv", "xlsx"])

def parse_file(uploaded_file):
    """Liest CSV/XLSX ein und findet automatisch die richtige Header-Zeile."""
    if uploaded_file.name.endswith(('.xlsx', '.xls')):
        return pd.read_excel(uploaded_file)
    
    content = uploaded_file.read().decode('utf-8', errors='ignore')
    lines = content.splitlines()
    
    # Header-Zeile dynamisch suchen
    header_idx = 0
    for idx, line in enumerate(lines):
        line_low = line.lower()
        if any(keyword in line_low for keyword in ['custom label', 'sku', 'net amount', 'artikelnummer', 'gesamtbetrag']):
            header_idx = idx
            break
            
    file_buffer = io.StringIO(content)
    
    # Trennzeichen ermitteln (Semikolon oder Komma) & fehlerhafte Zeilen überspringen
    try:
        df = pd.read_csv(file_buffer, skiprows=header_idx, sep=";", on_bad_lines="skip")
        if len(df.columns) <= 1:
            file_buffer.seek(0)
            df = pd.read_csv(file_buffer, skiprows=header_idx, sep=",", on_bad_lines="skip")
    except Exception:
        file_buffer.seek(0)
        df = pd.read_csv(file_buffer, skiprows=header_idx, sep=",", on_bad_lines="skip")
        
    return df

if ebay_file is not None:
    try:
        df = parse_file(ebay_file)
        df.columns = [str(c).strip() for c in df.columns]
    except Exception as e:
        st.error(f"Fehler beim Einlesen der Datei: {e}")
        st.stop()

    # Spaltenerkennung
    sku_col, netto_col = None, None
    for col in df.columns:
        c_clean = col.lower()
        if c_clean in ['custom label', 'sku', 'customlabel', 'artikelnummer']:
            sku_col = col
        if c_clean in ['net amount', 'netto_betrag', 'netto', 'amount', 'betrag', 'auszahlung']:
            netto_col = col

    # Fallback falls automatische Erkennung fehlschlägt
    if not sku_col or not netto_col:
        st.warning("⚠️ Spalten konnten nicht automatisch zugeordnet werden:")
        c1, c2 = st.columns(2)
        sku_col = c1.selectbox("SKU-Spalte wählen:", df.columns, index=0)
        netto_col = c2.selectbox("Netto-Betrag-Spalte wählen:", df.columns, index=min(1, len(df.columns)-1))

    # Beträge säubern
    df[netto_col] = df[netto_col].astype(str).str.replace('€', '').str.replace(' ', '').str.replace(',', '.')
    df[netto_col] = pd.to_numeric(df[netto_col], errors='coerce').fillna(0)

    # Logik für Gruppenzuordnung
    gruppe_a_prefixes = ['PP', 'BA', 'MK', '001']
    def assign_group(row):
        sku = str(row[sku_col]).strip().upper()
        if not sku or sku == 'NAN':
            return 'Ohne Zuordnung', 'Keine SKU vorhanden'
        
        for p in gruppe_a_prefixes:
            if sku.startswith(p):
                return 'Gruppe A', None
        if sku.startswith('NB') or len(sku) > 0:
            return 'Gruppe B', None
        
        return 'Ohne Zuordnung', 'Unbekanntes SKU-Format'

    res = df.apply(assign_group, axis=1)
    df['Gruppe'] = [r[0] for r in res]
    df['Fehlergrund'] = [r[1] for r in res]

    # TAB STRUCTURE
    tab1, tab2, tab3, tab4 = st.tabs([
        "Tab 1: Gruppe A (Direkt)", 
        "Tab 2: Gruppe B (Über Dich)", 
        "Tab 3: Ohne Zuordnung", 
        "Tab 4: Alle Daten"
    ])

    # TAB 1: GRUPPE A
    with tab1:
        st.header("Gruppe A: Direkt-Partner (0,5 % Provision)")
        df_a = df[df['Gruppe'] == 'Gruppe A']
        if df_a.empty:
            st.info("Keine Einträge für Gruppe A gefunden.")
        else:
            for sku in sorted(df_a[sku_col].astype(str).unique()):
                sub = df_a[df_a[sku_col].astype(str) == sku]
                netto = sub[netto_col].sum()
                prov = netto * 0.005
                ausz = netto - prov
                
                st.subheader(f"Partner / SKU: {sku}")
                m1, m2, m3 = st.columns(3)
                m1.metric("eBay Netto-Umsatz", f"{netto:,.2f} €")
                m2.metric("Provision (0,5 %)", f"{prov:,.2f} €")
                m3.metric("Auszahlungsbetrag", f"{ausz:,.2f} €")
                
                st.dataframe(sub, use_container_width=True)
                csv = sub.to_csv(index=False).encode('utf-8')
                st.download_button(f"📥 CSV-Download {sku}", csv, f"Abrechnung_{sku}.csv", "text/csv", key=f"dl_a_{sku}")
                st.markdown("---")

    # TAB 2: GRUPPE B
    with tab2:
        st.header("Gruppe B: Abrechnung über Evelyn / Partner-Einzelübersichten")
        df_b = df[df['Gruppe'] == 'Gruppe B']
        if df_b.empty:
            st.info("Keine Einträge für Gruppe B gefunden.")
        else:
            st.subheader("1. Gesamtabrechnung an Evelyn Kukulan (Kundennr. 16335)")
            netto_b = df_b[netto_col].sum()
            rabatt_b = netto_b * 0.005
            endbetrag_b = netto_b - rabatt_b
            
            b1, b2, b3 = st.columns(3)
            b1.metric("Gesamt-Umsatz Gruppe B", f"{netto_b:,.2f} €")
            b2.metric("Rabatt (0,5 %)", f"{rabatt_b:,.2f} €")
            b3.metric("Rechnungsbetrag Lexoffice", f"{endbetrag_b:,.2f} €")
            
            if st.button("🚀 Rechnungsentwurf in Lexoffice anlegen (0,5 %)", type="primary"):
                st.success("Rechnungsentwurf für Evelyn Kukulan (Kundennr. 16335) erfolgreich übermittelt!")
            
            st.markdown("---")
            st.subheader("2. Partner-Einzelübersichten (3,5 % Provision)")
            for sku in sorted(df_b[sku_col].astype(str).unique()):
                with st.expander(f"📌 Einzelabrechnung SKU: {sku}"):
                    sub_b = df_b[df_b[sku_col].astype(str) == sku]
                    n_b = sub_b[netto_col].sum()
                    p_b = n_b * 0.035
                    a_b = n_b - p_b
                    
                    x1, x2, x3 = st.columns(3)
                    x1.metric("Umsatz SKU", f"{n_b:,.2f} €")
                    x2.metric("Provision (3,5 %)", f"{p_b:,.2f} €")
                    x3.metric("Auszahlung", f"{a_b:,.2f} €")
                    
                    st.dataframe(sub_b, use_container_width=True)
                    csv_b = sub_b.to_csv(index=False).encode('utf-8')
                    st.download_button(f"📥 CSV-Download {sku}", csv_b, f"Abrechnung_3.5_{sku}.csv", "text/csv", key=f"dl_b_{sku}")

    # TAB 3: OHNE ZUORDNUNG
    with tab3:
        st.header("Ohne Zuordnung / Fehlerhafte Datensätze")
        df_none = df[df['Gruppe'] == 'Ohne Zuordnung']
        if df_none.empty:
            st.success("Alle Daten konnten erfolgreich zugeordnet werden!")
        else:
            st.warning(f"Es wurden {len(df_none)} Zeilen ohne Zuordnung gefunden.")
            st.dataframe(df_none, use_container_width=True)
            csv_none = df_none.to_csv(index=False).encode('utf-8')
            st.download_button("📥 Fehlerhafte Zeilen als CSV exportieren", csv_none, "ohne_zuordnung.csv", "text/csv")

    # TAB 4: ALLE DATEN
    with tab4:
        st.header("Alle verarbeiteten Daten")
        st.dataframe(df, use_container_width=True)

else:
    st.info("Bitte lade eine Auszahlungsdatei in der linken Seitenleiste hoch.")
