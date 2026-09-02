import streamlit as st
import pandas as pd
import io

st.set_page_config(page_title="eBay-Verrechnung Lexoffice", layout="wide")
st.title("📋 eBay-Auszahlungs verrechnung & Lexoffice Upload")

# --- MOCK / CSV DATA INPUT ---
uploaded_file = st.sidebar.file_drop_target if hasattr(st.sidebar, 'file_drop_target') else st.sidebar.file_uploader("eBay CSV hochladen", type=["csv"])

if uploaded_file is not None:
    # Hier Anpassung je nach Trennzeichen (z.B. sep=";")
    df = pd.read_csv(uploaded_file)
    
    # --- GRUPPIERUNGS-LOGIK ---
    gruppe_a_prefixes = ['PP', 'BA', 'MK', '001']
    
    def assign_group(sku):
        sku_str = str(sku).upper()
        for prefix in gruppe_a_prefixes:
            if sku_str.startswith(prefix):
                return 'Gruppe A'
        return 'Gruppe B'

    # SKU Spalte ermitteln (Beispielname 'SKU' anpassen falls nötig)
    if 'SKU' in df.columns and 'Netto_Betrag' in df.columns:
        df['Gruppe'] = df['SKU'].apply(assign_group)
        
        # Tabs definieren
        tab1, tab2, tab3, tab4 = st.tabs([
            "Tab 1: Gruppe A (Direkt)", 
            "Tab 2: Gruppe B (Über Dich)", 
            "Tab 3: Ohne Zuordnung", 
            "Tab 4: Alle Daten"
        ])
        
        # ==========================================
        # TAB 1: GRUPPE A (DIREKT-PARTNER)
        # ==========================================
        with tab1:
            st.header("Gruppe A: Direkt-Partner (0,5 % Provision)")
            df_a = df[df['Gruppe'] == 'Gruppe A'].copy()
            
            if df_a.empty:
                st.info("Keine Positionen für Gruppe A gefunden.")
            else:
                # Nach SKU/Partner aufteilen
                skus_a = df_a['SKU'].unique()
                
                for sku in skus_a:
                    st.subheader(f"Partner / SKU: {sku}")
                    sub_df = df_a[df_a['SKU'] == sku].copy()
                    
                    netto_summe = sub_df['Netto_Betrag'].sum()
                    provision = netto_summe * 0.005
                    auszahlung = netto_summe - provision
                    
                    # Kennzahlen anzeigen
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Brutto-Umsatz (Netto-eBay-Basis)", f"{netto_summe:,.2f} €")
                    c2.metric("Provision (0,5 %)", f"{provision:,.2f} €")
                    c3.metric("Auszahlungsbetrag", f"{auszahlung:,.2f} €")
                    
                    st.dataframe(sub_df, use_container_width=True)
                    
                    # CSV Export für diesen spezifischen Partner
                    csv_buffer = sub_df.to_csv(index=False).encode('utf-8')
                    st.download_button(
                        label=f"📥 CSV-Download für {sku}",
                        data=csv_buffer,
                        file_name=f"Abrechnung_{sku}_GruppeA.csv",
                        mime="text/csv",
                        key=f"dl_{sku}"
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
                gesamt_netto_b = df_b['Netto_Betrag'].sum()
                evelyn_provision = gesamt_netto_b * 0.005
                evelyn_endbetrag = gesamt_netto_b - evelyn_provision
                
                m1, m2, m3 = st.columns(3)
                m1.metric("Gesamt-Umsatz Gruppe B", f"{gesamt_netto_b:,.2f} €")
                m2.metric("Abzug / Provision (0,5 %)", f"{evelyn_provision:,.2f} €")
                m3.metric("Rechnungsbetrag Lexoffice", f"{evelyn_endbetrag:,.2f} €")
                
                if st.button("🚀 Rechnungsentwurf in Lexoffice anlegen (0,5 %)", type="primary"):
                    # API-Call zu Lexoffice einfügen
                    st.success("Rechnungsentwurf für Evelyn Kukulan (16335) erfolgreich in Lexoffice angelegt!")
                
                st.markdown("---")
                
                # --- 2. EINZELÜBERSICHTEN PRO SKU (3,5 %) ---
                st.subheader("2. Partner-Einzelübersichten (3,5 % Provision für Dich & Patrick)")
                
                skus_b = df_b['SKU'].unique()
                for sku in skus_b:
                    with st.expander(f"📌 Einzelabrechnung SKU: {sku}"):
                        sku_df = df_b[df_b['SKU'] == sku].copy()
                        sku_netto = sku_df['Netto_Betrag'].sum()
                        sku_provision = sku_netto * 0.035
                        sku_auszahlung = sku_netto - sku_provision
                        
                        p1, p2, p3 = st.columns(3)
                        p1.metric("Umsatz SKU", f"{sku_netto:,.2f} €")
                        p2.metric("Provision (3,5 %)", f"{sku_provision:,.2f} €")
                        p3.metric("Auszahlung an Partner", f"{sku_auszahlung:,.2f} €")
                        
                        st.dataframe(sku_df, use_container_width=True)
                        
                        # CSV Export pro SKU für Partner
                        csv_b_buffer = sku_df.to_csv(index=False).encode('utf-8')
                        st.download_button(
                            label=f"📥 Abrechnung {sku} als CSV herunterladen",
                            data=csv_b_buffer,
                            file_name=f"Abrechnung_{sku}_3.5%_Provision.csv",
                            mime="text/csv",
                            key=f"dl_b_{sku}"
                        )

        # ==========================================
        # TAB 3 & 4: OHNE ZUORDNUNG & ALLE DATEN
        # ==========================================
        with tab3:
            st.header("Ohne Zuordnung")
            # Logik für fehlerhafte/unbekannte SKUs
            df_unassigned = df[df['Gruppe'].isna()]
            st.dataframe(df_unassigned, use_container_width=True)
            
        with tab4:
            st.header("Alle Rohdaten")
            st.dataframe(df, use_container_width=True)

else:
    st.info("Bitte lade eine eBay-CSV-Datei in der Seitenleiste hoch, um die Verrechnung zu starten.")
