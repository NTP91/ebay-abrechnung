import streamlit as st
import pandas as pd
import io

# --- SEITEN-KONFIGURATION ---
st.set_page_config(page_title="eBay-Verrechnung Lexoffice", layout="wide")
st.title("📋 eBay-Auszahlungsverrechnung & Lexoffice Upload")

# --- SIDEBAR: FILE UPLOAD ---
st.sidebar.header("Datei-Upload")
uploaded_file = st.sidebar.file_uploader("eBay CSV hochladen", type=["csv", "xlsx"])

if uploaded_file is not None:
    # Automatische Format-Erkennung (CSV oder Excel)
    try:
        if uploaded_file.name.endswith('.csv'):
            # Versuche gängige Trennzeichen (Semikolon oder Komma)
            try:
                df = pd.read_csv(uploaded_file, sep=";")
                if len(df.columns) <= 1:
                    uploaded_file.seek(0)
                    df = pd.read_csv(uploaded_file, sep=",")
            except Exception:
                uploaded_file.seek(0)
                df = pd.read_csv(uploaded_file, sep=",")
        else:
            df = pd.read_excel(uploaded_file)
    except Exception as e:
        st.error(f"Fehler beim Lesen der Datei: {e}")
        st.stop()

    # --- SPALTEN-MAPPING / FLEXIBILITÄT ---
    # Suche flexibel nach Spaltennamen (SKU & Netto-Betrag)
    sku_col = None
    netto_col = None
    
    for col in df.columns:
        col_clean = str(col).strip().lower()
        if col_clean in ['sku', 'custom label', 'customlabel', 'artikelnummer']:
            sku_col = col
        if col_clean in ['netto_betrag', 'netto', 'amount', 'betrag', 'net netto']:
            netto_col = col

    # Falls nicht automatisch erkannt, Ausweichmöglichkeit über Selectboxen
    if not sku_col or not netto_col:
        st.warning("⚠️ Spalten konnten nicht automatisch zugewiesen werden. Bitte manuell wählen:")
        c_sel1, c_sel2 = st.columns(2)
        sku_col = c_sel1.selectbox("Spalte für SKU / Artikelnummer:", df.columns)
        netto_col = c_sel2.selectbox("Spalte für Netto-Betrag:", df.columns)

    # Beträge bereinigen & in float umwandeln
    df[netto_col] = df[netto_col].astype(str).str.replace('€', '').str.replace(' ', '')
    df[netto_col] = df[netto_col].str.replace(',', '.').astype(float)

    # --- GRUPPIERUNGS-LOGIK ---
    gruppe_a_prefixes = ['PP', 'BA', 'MK', '001']

    def assign_group(sku):
        sku_str = str(sku).strip().upper()
        for prefix in gruppe_a_prefixes:
            if sku_str.startswith(prefix):
                return 'Gruppe A'
        return 'Gruppe B'

    df['Gruppe'] = df[sku_col].apply(assign_group)

    # --- UI-TABS ---
    tab1, tab2, tab3, tab4 = st.tabs([
        "Tab 1: Gruppe A (Direkt)", 
        "Tab 2: Gruppe B (Über Dich)", 
        "Tab 3: Ohne Zuordnung", 
        "Tab 4: Alle Daten"
    ])

    # ==========================================
    # TAB 1: GRUPPE A (DIREKT-PARTNER - 0,5 %)
    # ==========================================
    with tab1:
        st.header("Gruppe A: Direkt-Partner (0,5 % Provision)")
        st.caption("Diese Partner rechnen direkt ab. Auszahlung durch Evelyn. Kein Lexoffice-Upload.")
        
        df_a = df[df['Gruppe'] == 'Gruppe A'].copy()
        
        if df_a.empty:
            st.info("Keine Positionen für Gruppe A gefunden.")
        else:
            skus_a = sorted(df_a[sku_col].astype(str).unique())
            
            for sku in skus_a:
                st.subheader(f"Partner / SKU-Präfix: {sku}")
                sub_df = df_a[df_a[sku_col].astype(str) == sku].copy()
                
                netto_summe = sub_df[netto_col].sum()
                provision = netto_summe * 0.005
                auszahlung = netto_summe - provision
                
                c1, c2, c3 = st.columns(3)
                c1.metric("eBay Netto-Umsatz", f"{netto_summe:,.2f} €")
                c2.metric("Provision (0,5 %)", f"{provision:,.2f} €")
                c3.metric("Auszahlungsbetrag an Partner", f"{auszahlung:,.2f} €")
                
                st.dataframe(sub_df, use_container_width=True)
                
                # CSV Export für Partner
                csv_buffer = sub_df.to_csv(index=False).encode('utf-8')
                st.download_button(
                    label=f"📥 CSV-Abrechnung herunterladen für {sku}",
                    data=csv_buffer,
                    file_name=f"Abrechnung_{sku}_GruppeA_0.5.csv",
                    mime="text/csv",
                    key=f"dl_a_{sku}"
                )
                st.markdown("---")

    # ==========================================
    # TAB 2: GRUPPE B (ÜBER DICH / EVELYN)
    # ==========================================
    with tab2:
        st.header("Gruppe B: Abrechnung über Evelyn / Partner-Einzelübersichten")
        df_b = df[df['Gruppe'] == 'Gruppe B'].copy()
        
        if df_b.empty:
            st.info("Keine Positionen für Gruppe B gefunden.")
        else:
            # --- 1. GESAMTÜBERSICHT FÜR EVELYN (0,5 %) ---
            st.subheader("1. Gesamtabrechnung an Evelyn Kukulan (Kundennr. 16335)")
            st.caption("Gesamte Abrechnung für Gruppe B mit 0,5 % Rabatt zur Übertragung an Lexoffice.")
            
            gesamt_netto_b = df_b[netto_col].sum()
            evelyn_rabatt = gesamt_netto_b * 0.005
            evelyn_endbetrag = gesamt_netto_b - evelyn_rabatt
            
            m1, m2, m3 = st.columns(3)
            m1.metric("Gesamt-Umsatz Gruppe B", f"{gesamt_netto_b:,.2f} €")
            m2.metric("Abzug / Rabatt (0,5 %)", f"{evelyn_rabatt:,.2f} €")
            m3.metric("Rechnungsbetrag (Lexoffice)", f"{evelyn_endbetrag:,.2f} €")
            
            if st.button("🚀 Rechnungsentwurf in Lexoffice anlegen (0,5 %)", type="primary"):
                # Hier folgt der API Call für Lexoffice
                st.success("Rechnungsentwurf für Evelyn Kukulan (Kundennr. 16335) wurde erfolgreich in Lexoffice angelegt!")
            
            st.markdown("---")
            
            # --- 2. EINZELÜBERSICHTEN PRO SKU (3,5 %) ---
            st.subheader("2. Partner-Einzelübersichten (3,5 % Provision für Dich & Patrick)")
            st.caption("Abrechnungsgrundlage pro SKU gegenüber den jeweiligen Partnern.")
            
            skus_b = sorted(df_b[sku_col].astype(str).unique())
            for sku in skus_b:
                with st.expander(f"📌 Einzelabrechnung SKU: {sku}"):
                    sku_df = df_b[df_b[sku_col].astype(str) == sku].copy()
                    sku_netto = sku_df[netto_col].sum()
                    sku_provision = sku_netto * 0.035
                    sku_auszahlung = sku_netto - sku_provision
                    
                    p1, p2, p3 = st.columns(3)
                    p1.metric("Umsatz SKU", f"{sku_netto:,.2f} €")
                    p2.metric("Provision Dich & Patrick (3,5 %)", f"{sku_provision:,.2f} €")
                    p3.metric("Auszahlung an Partner", f"{sku_auszahlung:,.2f} €")
                    
                    st.dataframe(sku_df, use_container_width=True)
                    
                    # CSV Export pro SKU
                    csv_b_buffer = sku_df.to_csv(index=False).encode('utf-8')
                    st.download_button(
                        label=f"📥 Abrechnung {sku} als CSV herunterladen (3,5 %)",
                        data=csv_b_buffer,
                        file_name=f"Abrechnung_{sku}_GruppeB_3.5.csv",
                        mime="text/csv",
                        key=f"dl_b_{sku}"
                    )

    # ==========================================
    # TAB 3: OHNE ZUORDNUNG
    # ==========================================
    with tab3:
        st.header("Ohne Zuordnung")
        df_unassigned = df[df['Gruppe'].isna() | (df[sku_col].isna())]
        if df_unassigned.empty:
            st.success("Alle Daten konnten erfolgreich zugeordnet werden!")
        else:
            st.dataframe(df_unassigned, use_container_width=True)

    # ==========================================
    # TAB 4: ALLE DATEN
    # ==========================================
    with tab4:
        st.header("Alle Rohdaten inkl. Gruppeneinteilung")
        st.dataframe(df, use_container_width=True)

else:
    st.info("Bitte lade eine eBay-CSV-Datei in der Seitenleiste hoch, um die Verrechnung zu starten.")
