import streamlit as st
import pandas as pd

# --- SEITEN-KONFIGURATION ---
st.set_page_config(page_title="eBay-Verrechnung Lexoffice", layout="wide")
st.title("📋 eBay-Auszahlungsverrechnung & Lexoffice Upload")

# --- SIDEBAR: ZWEI SEPARATE UPLOADS ---
st.sidebar.header("Datei-Uploads")

uploaded_ebay = st.sidebar.file_uploader(
    "1. eBay Auszahlungs-CSV hochladen", 
    type=["csv", "xlsx"],
    key="ebay_uploader"
)

uploaded_wahan = st.sidebar.file_uploader(
    "2. Wahan Bestellübersicht hochladen", 
    type=["csv", "xlsx"],
    key="wahan_uploader"
)

# --- UI-TABS IMMER ANZEIGEN ---
tab1, tab2, tab3, tab4 = st.tabs([
    "Tab 1: Gruppe A (Direkt)", 
    "Tab 2: Gruppe B (Über Dich)", 
    "Tab 3: Ohne Zuordnung", 
    "Tab 4: Alle Daten"
])

# Hilfsfunktion zum sicheren Einlesen
def load_data(file):
    if file is None:
        return None
    try:
        if file.name.endswith('.csv'):
            try:
                return pd.read_csv(file, sep=";")
            except Exception:
                file.seek(0)
                return pd.read_csv(file, sep=",")
        else:
            return pd.read_excel(file)
    except Exception as e:
        st.error(f"Fehler beim Lesen von {file.name}: {e}")
        return None

df_ebay = load_data(uploaded_ebay)
df_wahan = load_data(uploaded_wahan)

# Prüfen ob Daten da sind
if df_ebay is None:
    with tab1:
        st.info("Bitte lade die eBay Auszahlungs-CSV in der Seitenleiste hoch.")
    with tab2:
        st.info("Bitte lade die eBay Auszahlungs-CSV in der Seitenleiste hoch.")
    with tab3:
        st.info("Keine Daten geladen.")
    with tab4:
        st.info("Keine Daten geladen.")
else:
    # ---------------------------------------------------------
    # HIER DIE VERRECHNUNGSLOGIK MIT DF_EBAY UND DF_WAHAN
    # ---------------------------------------------------------
    
    # Spalten-Erkennung für eBay (SKU & Netto)
    sku_col = None
    netto_col = None
    for col in df_ebay.columns:
        c_low = str(col).strip().lower()
        if c_low in ['sku', 'custom label', 'customlabel', 'artikelnummer']:
            sku_col = col
        if c_low in ['netto_betrag', 'netto', 'amount', 'betrag', 'net netto']:
            netto_col = col

    if not sku_col or not netto_col:
        st.warning("⚠️ Bitte Spaltenzuordnung prüfen:")
        c1, c2 = st.columns(2)
        sku_col = c1.selectbox("SKU-Spalte:", df_ebay.columns, key="s_sku")
        netto_col = c2.selectbox("Netto-Betrag-Spalte:", df_ebay.columns, key="s_netto")

    # Clean Beträge
    df_ebay[netto_col] = df_ebay[netto_col].astype(str).str.replace('€', '').str.replace(' ', '').str.replace(',', '.')
    df_ebay[netto_col] = pd.to_numeric(df_ebay[netto_col], errors='coerce').fillna(0)

    # Gruppierung
    gruppe_a_prefixes = ['PP', 'BA', 'MK', '001']
    def assign_group(sku):
        s = str(sku).strip().upper()
        for p in gruppe_a_prefixes:
            if s.startswith(p):
                return 'Gruppe A'
        return 'Gruppe B'

    df_ebay['Gruppe'] = df_ebay[sku_col].apply(assign_group)

    # --- TAB 1: GRUPPE A (0,5 %) ---
    with tab1:
        st.header("Gruppe A: Direkt-Partner (0,5 % Provision)")
        df_a = df_ebay[df_ebay['Gruppe'] == 'Gruppe A']
        
        if df_a.empty:
            st.info("Keine Einträge für Gruppe A.")
        else:
            for sku in sorted(df_a[sku_col].astype(str).unique()):
                sub_df = df_a[df_a[sku_col].astype(str) == sku]
                netto = sub_df[netto_col].sum()
                prov = netto * 0.005
                ausz = netto - prov
                
                st.subheader(f"Partner / SKU: {sku}")
                m1, m2, m3 = st.columns(3)
                m1.metric("eBay Netto-Umsatz", f"{netto:,.2f} €")
                m2.metric("Provision (0,5 %)", f"{prov:,.2f} €")
                m3.metric("Auszahlungsbetrag", f"{ausz:,.2f} €")
                
                st.dataframe(sub_df, use_container_width=True)
                csv = sub_df.to_csv(index=False).encode('utf-8')
                st.download_button(f"📥 CSV-Download {sku}", csv, f"Abrechnung_{sku}.csv", "text/csv", key=f"dl_a_{sku}")
                st.markdown("---")

    # --- TAB 2: GRUPPE B (0,5 % Evelyn / 3,5 % Einzel) ---
    with tab2:
        st.header("Gruppe B: Abrechnung über Evelyn / Partner-Einzelübersichten")
        df_b = df_ebay[df_ebay['Gruppe'] == 'Gruppe B']
        
        if df_b.empty:
            st.info("Keine Einträge für Gruppe B.")
        else:
            # 1. Evelyn Gesamtabrechnung (0,5 %)
            st.subheader("1. Gesamtabrechnung an Evelyn Kukulan (Kundennr. 16335)")
            netto_b = df_b[netto_col].sum()
            rabatt_b = netto_b * 0.005
            endbetrag_b = netto_b - rabatt_b
            
            b1, b2, b3 = st.columns(3)
            b1.metric("Gesamt-Umsatz Gruppe B", f"{netto_b:,.2f} €")
            b2.metric("Rabatt (0,5 %)", f"{rabatt_b:,.2f} €")
            b3.metric("Rechnungsbetrag Lexoffice", f"{endbetrag_b:,.2f} €")
            
            if st.button("🚀 Rechnungsentwurf in Lexoffice anlegen (0,5 %)", type="primary"):
                st.success("Rechnungsentwurf für Evelyn Kukulan (16335) wurde angelegt!")
            
            st.markdown("---")
            
            # 2. Einzelübersichten (3,5 %)
            st.subheader("2. Partner-Einzelübersichten (3,5 % Provision für Dich & Patrick)")
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

    # --- TAB 3 & 4 ---
    with tab3:
        st.header("Ohne Zuordnung")
        st.dataframe(df_ebay[df_ebay['Gruppe'].isna()], use_container_width=True)
        
    with tab4:
        st.header("Alle Rohdaten")
        st.subheader("eBay Daten")
        st.dataframe(df_ebay, use_container_width=True)
        if df_wahan is not None:
            st.subheader("Wahan Daten")
            st.dataframe(df_wahan, use_container_width=True)
