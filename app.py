import streamlit as st
import pandas as pd
from core import process_uploaded_files, create_lexoffice_draft

st.set_page_config(page_title="eBay Payment Tool", layout="wide")

# --- HEADER ---
st.title("📋 eBay Payment Tool")
st.caption("Partnerzuordnung, Abrechnung und Lexware-Office-Rechnungsentwurf")

# --- SIDEBAR ---
with st.sidebar:
    st.header("Lexware Office")
    api_key = st.text_input("API-Key", type="password", key="lex_api_key")
    customer_id = st.text_input("Kundennummer", value="16335")
    invoice_date = st.date_input("Rechnungsdatum")
    tax_rate = st.number_input("Umsatzsteuer (%)", value=19.0)

# --- UPLOAD SECTION ---
col_up1, col_up2 = st.columns(2)
with col_up1:
    ebay_files = st.file_uploader("eBay-Auszahlungen", type=["csv", "xlsx"], accept_multiple_files=True)
with col_up2:
    wahan_files = st.file_uploader("Wahan-Bestellübersicht (optional)", type=["csv", "xlsx"], accept_multiple_files=True)

if ebay_files:
    df, error_msg = process_uploaded_files(ebay_files)
    
    if error_msg:
        st.warning(error_msg)
    
    if df is not None and not df.empty:
        sku_col = 'Custom Label' if 'Custom Label' in df.columns else df.columns[0]
        netto_col = 'Net amount' if 'Net amount' in df.columns else df.columns[1]

        # TABS
        tab1, tab2, tab3, tab4 = st.tabs([
            "Gruppe A (Direkt)", 
            "Gruppe B (Über Dich)", 
            "Ohne Zuordnung", 
            "Alle Daten"
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
                    
                    st.dataframe(sub, use_container_width=True)
                    csv = sub.to_csv(index=False).encode('utf-8')
                    st.download_button(f"📥 CSV-Download {sku}", csv, f"Abrechnung_{sku}.csv", "text/csv", key=f"dl_a_{sku}")
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
                
                if st.button("🚀 Rechnungsentwurf in Lexware Office erstellen", type="primary"):
                    if not api_key:
                        st.error("Für den Upload bitte links den Lexware-Office-API-Key eingeben.")
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
                    
                    st.markdown(f"**SKU: {sku}**")
                    x1, x2, x3 = st.columns(3)
                    x1.metric("Umsatz SKU", f"{n_b:,.2f} €")
                    x2.metric("Provision (3,5 %)", f"{p_b:,.2f} €")
                    x3.metric("Auszahlung", f"{a_b:,.2f} €")
                    
                    st.dataframe(sub_b, use_container_width=True)
                    csv_b = sub_b.to_csv(index=False).encode('utf-8')
                    st.download_button(f"📥 CSV {sku}", csv_b, f"Abrechnung_3.5_{sku}.csv", "text/csv", key=f"dl_b_{sku}")
                    st.markdown("---")

        # TAB 3 & 4
        with tab3:
            st.header("Ohne Zuordnung")
            df_none = df[df['Gruppe'] == 'Ohne Zuordnung']
            st.dataframe(df_none, use_container_width=True)

        with tab4:
            st.header("Alle Daten")
            st.dataframe(df, use_container_width=True)
else:
    st.info("Bitte zuerst eine oder mehrere eBay-Auszahlungsdateien oben hochladen.")
