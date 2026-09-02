from __future__ import annotations

import re
from datetime import date

import pandas as pd
import streamlit as st

from payment_tool.core import calculate_overviews, csv_bytes, detect_column, prepare_transactions_detailed, read_upload
from payment_tool.lexware import LexwareError, build_invoice_payload, create_draft, find_customer

CUSTOMER_NUMBER = 16335
EMPTY = pd.DataFrame(columns=["SKU", "Netto-Umsatz", "Gruppe", "Provision/Rabatt 0,5 %", "Abrechnung nach 0,5 %", "Provision 3,5 %", "Abrechnung nach 3,5 %"])


def safe_filename(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._") or "ohne_sku"


st.set_page_config(page_title="eBay Payment Tool", page_icon="💶", layout="wide")
st.title("eBay Payment Tool")
st.caption("Partnerzuordnung, Abrechnung und Lexware-Office-Rechnungsentwurf")

with st.sidebar:
    st.header("Lexware Office")
    api_key = st.text_input("API-Key", type="password", help="Wird nur für den API-Aufruf verwendet und nicht gespeichert.")
    st.text_input("Kundennummer", value=str(CUSTOMER_NUMBER), disabled=True)
    invoice_date = st.date_input("Rechnungsdatum", value=date.today())
    tax_rate = st.number_input("Umsatzsteuer (%)", value=19.0, min_value=0.0, max_value=100.0, step=1.0)

left, right = st.columns(2)
with left:
    ebay_file = st.file_uploader("eBay-Auszahlung", type=("csv", "txt", "xlsx"))
with right:
    wahan_file = st.file_uploader("Wahan-Bestellübersicht (optional)", type=("csv", "txt", "xlsx"))

ebay_result = wahan_result = None
import_error = None
try:
    if ebay_file:
        ebay_result = read_upload(ebay_file, ebay_file.name)
        if ebay_result.frame.empty:
            raise ValueError("Die eBay-Datei enthält keine Datensätze.")
    if wahan_file:
        wahan_result = read_upload(wahan_file, wahan_file.name)
        if wahan_result.frame.empty:
            raise ValueError("Die Wahan-Datei enthält keine Datensätze.")
except Exception as exc:
    import_error = str(exc)
    st.error(f"Die Datei konnte nicht gelesen werden: {exc}")

transactions = pd.DataFrame(columns=["SKU", "Netto-Umsatz"])
unassigned = pd.DataFrame(columns=["SKU", "Netto-Umsatz", "Grund"])
group_a, group_b = EMPTY.copy(), EMPTY.copy()

if ebay_result is not None and not import_error:
    with st.expander("Importdetails und Spaltenzuordnung", expanded=False):
        source = "Excel" if ebay_result.delimiter == "Excel" else f"CSV · `{ebay_result.delimiter}` · {ebay_result.encoding}"
        st.write(f"eBay: Headerzeile {ebay_result.header_row + 1} · {source}")
        if ebay_result.skipped_rows:
            st.warning(f"Bis zu {ebay_result.skipped_rows} unregelmäßige eBay-Zeilen wurden übersprungen.")
        cols = list(ebay_result.frame.columns)
        sku_detected = detect_column(ebay_result.frame, "sku")
        amount_detected = detect_column(ebay_result.frame, "amount")
        order_detected = detect_column(ebay_result.frame, "order")
        sku_col = st.selectbox("eBay: SKU-Spalte", cols, index=cols.index(sku_detected) if sku_detected in cols else 0)
        amount_col = st.selectbox("eBay: Netto-Spalte", cols, index=cols.index(amount_detected) if amount_detected in cols else 0)
        order_options = ["— nicht verwenden —", *cols]
        order_col = st.selectbox("eBay: Bestellnummer", order_options, index=order_options.index(order_detected) if order_detected in order_options else 0)
        wahan_order_col = wahan_sku_col = None
        if wahan_result is not None:
            wcols = list(wahan_result.frame.columns)
            wo, ws = detect_column(wahan_result.frame, "order"), detect_column(wahan_result.frame, "sku")
            st.write(f"Wahan: Headerzeile {wahan_result.header_row + 1}")
            wahan_order_col = st.selectbox("Wahan: Bestellnummer", wcols, index=wcols.index(wo) if wo in wcols else 0)
            wahan_sku_col = st.selectbox("Wahan: SKU-Spalte", wcols, index=wcols.index(ws) if ws in wcols else 0)
    if not sku_detected or not amount_detected:
        st.warning("SKU- oder Netto-Spalte wurde nicht sicher erkannt. Bitte die Spaltenzuordnung prüfen.")
    transactions, unassigned = prepare_transactions_detailed(
        ebay_result.frame, sku_col, amount_col,
        None if order_col == "— nicht verwenden —" else order_col,
        wahan_result.frame if wahan_result is not None else None, wahan_order_col, wahan_sku_col,
    )
    if not transactions.empty:
        group_a, group_b = calculate_overviews(transactions)

# Always render all four tabs, including before upload and after import errors.
tab_a, tab_b, tab_unassigned, tab_all = st.tabs(("Gruppe A (Direkt)", "Gruppe B (Über Dich)", "Ohne Zuordnung", "Alle Daten"))

with tab_a:
    st.subheader("Direkt-Partner")
    st.caption("PP, BA, MK und 001 · Netto-Umsatz abzüglich 0,5 % Provision · kein Lexware-Upload")
    if group_a.empty:
        st.info("Keine Datensätze für Gruppe A vorhanden." if ebay_file else "Bitte zuerst eine eBay-Auszahlung hochladen.")
    else:
        view = group_a[["SKU", "Netto-Umsatz", "Provision/Rabatt 0,5 %", "Abrechnung nach 0,5 %"]]
        st.dataframe(view, use_container_width=True, hide_index=True)
        metric_a, metric_b = st.columns(2)
        metric_a.metric("Netto gesamt", f"{view['Netto-Umsatz'].sum():,.2f} €")
        metric_b.metric("Abrechnung gesamt", f"{view['Abrechnung nach 0,5 %'].sum():,.2f} €")
        st.markdown("**CSV je Partner-SKU**")
        buttons = st.columns(3)
        for index, (_, row) in enumerate(view.iterrows()):
            buttons[index % 3].download_button(f"{row['SKU']}.csv", csv_bytes(pd.DataFrame([row])), f"gruppe_a_{safe_filename(row['SKU'])}.csv", "text/csv", key=f"a-{index}")

with tab_b:
    st.subheader("Evelyn-Gesamtübersicht")
    st.caption("NB und alle übrigen Standard-SKUs · 0,5 % Rabatt")
    if group_b.empty:
        st.info("Keine Datensätze für Gruppe B vorhanden." if ebay_file else "Bitte zuerst eine eBay-Auszahlung hochladen.")
    else:
        summary_cols = ["SKU", "Netto-Umsatz", "Provision/Rabatt 0,5 %", "Abrechnung nach 0,5 %"]
        st.dataframe(group_b[summary_cols], use_container_width=True, hide_index=True)
        metric_a, metric_b = st.columns(2)
        metric_a.metric("Netto-Umsatz", f"{group_b['Netto-Umsatz'].sum():,.2f} €")
        metric_b.metric("Nach 0,5 % Rabatt", f"{group_b['Abrechnung nach 0,5 %'].sum():,.2f} €")
        st.download_button("Evelyn-Gesamtübersicht als CSV", csv_bytes(group_b[summary_cols]), "gruppe_b_evelyn_gesamt.csv", "text/csv")
        if st.button("Rechnungsentwurf in Lexware Office erstellen", type="primary", disabled=not api_key, help=f"Entwurf für Kundennummer {CUSTOMER_NUMBER}"):
            try:
                contact = find_customer(api_key, CUSTOMER_NUMBER)
                result = create_draft(api_key, build_invoice_payload(group_b, contact["id"], invoice_date, tax_rate))
                st.success(f"Rechnungsentwurf erstellt. ID: {result.get('id', 'unbekannt')}")
                if result.get("id"):
                    st.link_button("Entwurf in Lexware Office öffnen", f"https://app.lexware.de/permalink/invoices/view/{result['id']}")
            except LexwareError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"Unerwarteter API-Fehler: {exc}")
        if not api_key:
            st.caption("Für den Upload bitte links den Lexware-Office-API-Key eingeben.")
        st.divider()
        st.subheader("Einzelabrechnungen für dich & Patrick")
        st.caption("Je SKU: Netto-Umsatz abzüglich 3,5 % Provision")
        detail_cols = ["SKU", "Netto-Umsatz", "Provision 3,5 %", "Abrechnung nach 3,5 %"]
        st.dataframe(group_b[detail_cols], use_container_width=True, hide_index=True)
        buttons = st.columns(3)
        for index, (_, row) in enumerate(group_b[detail_cols].iterrows()):
            buttons[index % 3].download_button(f"{row['SKU']}.csv", csv_bytes(pd.DataFrame([row])), f"gruppe_b_{safe_filename(row['SKU'])}.csv", "text/csv", key=f"b-{index}")

with tab_unassigned:
    st.subheader("Ohne Zuordnung")
    st.caption("Zeilen ohne verwertbare SKU oder gültigen Netto-Betrag")
    if unassigned.empty:
        st.info("Keine unzugeordneten Zeilen vorhanden." if ebay_file else "Bitte zuerst eine eBay-Auszahlung hochladen.")
    else:
        st.warning(f"{len(unassigned)} Zeilen benötigen eine manuelle Prüfung.")
        st.dataframe(unassigned, use_container_width=True, hide_index=True)
        st.download_button("Unzugeordnete Zeilen als CSV", csv_bytes(unassigned), "ohne_zuordnung.csv", "text/csv")

with tab_all:
    st.subheader("Alle Daten")
    st.caption("Bereinigte, für die Abrechnung verwendete eBay-Datensätze")
    if transactions.empty:
        st.info("Keine gültigen Datensätze vorhanden." if ebay_file else "Bitte zuerst eine eBay-Auszahlung hochladen.")
    else:
        st.dataframe(transactions, use_container_width=True, hide_index=True)
        st.download_button("Alle bereinigten Daten als CSV", csv_bytes(transactions), "ebay_alle_daten.csv", "text/csv")
