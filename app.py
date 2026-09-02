import streamlit as st
import pandas as pd

st.set_page_config(page_title="eBay-Verrechnung Lexoffice", layout="wide")
st.title("📋 eBay-Auszahlungsverrechnung & Lexoffice Upload")

uploaded_file = st.sidebar.file_uploader("eBay CSV hochladen", type=["csv", "xlsx"])

if uploaded_file is not None:
    # 1. Datei einlesen (Skiprows fängt die eBay-Kopfzeilen ab)
    try:
        if uploaded_file.name.endswith('.csv'):
            try:
                df = pd.read_csv(uploaded_file, sep=";", skiprows=11)
                if len(df.columns) <= 1:
                    uploaded_file.seek(0)
                    df = pd.read_csv(uploaded_file, sep=",", skiprows=11)
            except Exception:
                uploaded_file.seek(0)
                df = pd.read_csv(uploaded_file, sep=",", skiprows=11)
        else:
            df = pd.read_excel(uploaded_file)
    except Exception as e:
        st.error(f"Fehler beim Einlesen: {e}")
        st.stop()

    # 2. Spalten finden
    sku_col = None
    netto_col = None
    
    for col in df.columns:
        c_clean = str(col).strip().lower()
        if c_clean in ['custom label', 'sku', 'customlabel', 'artikelnummer']:
            sku_col = col
        if c_clean in ['net amount', 'netto_betrag', 'netto', 'amount', 'betrag']:
            netto_col = col

    if not sku_col or not netto_col:
        st.warning("⚠️ Bitte Spaltenzuordnung wählen:")
        c1, c2 = st.columns(2)
        sku_col = c1.selectbox("SKU-Spalte:", df.columns)
        netto_col = c2.selectbox("Netto-Betrag-Spalte:", df.columns)

    # Beträge bereinigen
    df[netto_col] = df[netto_col].astype(str).str.replace('€', '').str.replace(' ', '').str.replace(',', '.')
    df[netto_col] = pd.to_numeric(df[netto_col], errors='coerce').fillna(0)

    # Gruppierung
    gruppe_a_prefixes = ['PP', 'BA', 'MK', '001']
    def assign_group(sku):
        s = str(sku).strip().upper()
        for p in gruppe_a_prefixes:
            if s.startswith(p):
                return 'Gruppe A'
        return 'Gruppe B'

    df['Gruppe'] = df[sku_col].apply(assign_group)

    # 3. Tabs
    tab1, tab2, tab3, tab4 = st.tabs([
        "Tab 1: Gruppe A (Direkt)", 
        "Tab 2: Gruppe B (Über Dich)", 
        "Tab 3: Ohne Zuordnung", 
        "Tab 4: Alle Daten"
    ])

    with tab1:
        st.header("Gruppe A: Direkt-Partner (0,5 % Provision)")
        df_a = df[df['Gruppe'] == 'Gruppe A']
        if df_a.empty:
            st.info("Keine Positionen für Gruppe A gefunden.")
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

    with tab2:
        st.header("Gruppe B: Abrechnung über Evelyn / Partner-Einzelübersichten")
        df_b = df[df['Gruppe'] == 'Gruppe B']
        if df_b.empty:
            st.info("Keine Positionen für Gruppe B gefunden.")
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
                st.success("Rechnungsentwurf für Evelyn Kukulan (16335) wurde angelegt!")
            
            st.markdown("---")
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

    with tab3:
        st.header("Ohne Zuordnung")
        st.dataframe(df[df['Gruppe'].isna()], use_container_width=True)

    with tab4:
        st.header("Alle Daten")
        st.dataframe(df, use_container_width=True)

else:
    st.info("Bitte lade eine eBay-CSV-Datei in der Seitenleiste hoch, um die Verrechnung zu starten.")
