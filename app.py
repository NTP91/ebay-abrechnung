from __future__ import annotations

import re
from datetime import date

import pandas as pd
import streamlit as st

from core import (
    LexwareError,
    build_invoice_payload,
    calculate_overviews,
    combine_uploaded_frames,
    create_draft,
    csv_bytes,
    detect_column,
    find_customer,
    prepare_transactions_detailed,
    read_upload,
)

CUSTOMER_NUMBER = 16335

EMPTY = pd.DataFrame(
    columns=[
        "SKU",
        "Netto-Umsatz",
        "Gruppe",
        "Provision/Rabatt 0,5 %",
        "Abrechnung nach 0,5 %",
        "Provision 3,5 %",
        "Abrechnung nach 3,5 %",
    ]
)


def safe_filename(value: object) -> str:
    return (
        re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._")
        or "ohne_sku"
    )


st.set_page_config(
    page_title="eBay Payment Tool",
    page_icon="💶",
    layout="wide",
)

st.title("eBay Payment Tool")
st.caption(
    "Partnerzuordnung, Abrechnung und "
    "Lexware-Office-Rechnungsentwurf"
)

with st.sidebar:
    st.header("Lexware Office")

    api_key = st.text_input(
        "API-Key",
        type="password",
        help=(
            "Der API-Key wird nur für den API-Aufruf verwendet "
            "und nicht gespeichert."
        ),
    )

    st.text_input(
        "Kundennummer",
        value=str(CUSTOMER_NUMBER),
        disabled=True,
    )

    invoice_date = st.date_input(
        "Rechnungsdatum",
        value=date.today(),
    )

    tax_rate = st.number_input(
        "Umsatzsteuer (%)",
        value=19.0,
        min_value=0.0,
        max_value=100.0,
        step=1.0,
    )

upload_left, upload_right = st.columns(2)

with upload_left:
    ebay_files = st.file_uploader(
        "eBay-Auszahlungen",
        type=("csv", "txt", "xlsx"),
        accept_multiple_files=True,
        help=(
            "Mehrere Dateien können gemeinsam ausgewählt "
            "oder per Drag-and-drop abgelegt werden."
        ),
    )

with upload_right:
    wahan_file = st.file_uploader(
        "Wahan-Bestellübersicht (optional)",
        type=("csv", "txt", "xlsx"),
    )

ebay_results = []
wahan_result = None
ebay_frame = pd.DataFrame()
duplicate_count = 0
import_error = None

try:
    for ebay_file in ebay_files:
        result = read_upload(
            ebay_file,
            ebay_file.name,
        )

        if result.frame.empty:
            raise ValueError(
                f"Die eBay-Datei „{ebay_file.name}“ "
                "enthält keine Datensätze."
            )

        ebay_results.append(
            (ebay_file.name, result)
        )

    if ebay_results:
        ebay_frame, duplicate_count = combine_uploaded_frames(
            [
                result.frame
                for _, result in ebay_results
            ]
        )

    if wahan_file:
        wahan_result = read_upload(
            wahan_file,
            wahan_file.name,
        )

        if wahan_result.frame.empty:
            raise ValueError(
                "Die Wahan-Datei enthält keine Datensätze."
            )

except Exception as exc:
    import_error = str(exc)

    st.error(
        f"Die Datei konnte nicht gelesen werden: {exc}"
    )

transactions = pd.DataFrame(
    columns=[
        "SKU",
        "Netto-Umsatz",
    ]
)

unassigned = pd.DataFrame(
    columns=[
        "SKU",
        "Netto-Umsatz",
        "Grund",
    ]
)

group_a = EMPTY.copy()
group_b = EMPTY.copy()

if ebay_results and not import_error:
    with st.expander(
        "Importdetails und Spaltenzuordnung",
        expanded=False,
    ):
        for filename, result in ebay_results:
            if result.delimiter == "Excel":
                source = "Excel"
            else:
                source = (
                    f"CSV · `{result.delimiter}` · "
                    f"{result.encoding}"
                )

            st.write(
                f"**{filename}:** "
                f"Headerzeile {result.header_row + 1} · "
                f"{source} · "
                f"{len(result.frame)} Zeilen"
            )

            if result.skipped_rows:
                st.warning(
                    f"In „{filename}“ wurden bis zu "
                    f"{result.skipped_rows} unregelmäßige "
                    "Zeilen übersprungen."
                )

        if duplicate_count:
            st.success(
                f"{duplicate_count} exakte Duplikate wurden "
                "vor der Abrechnung entfernt."
            )

        cols = list(ebay_frame.columns)

        sku_detected = detect_column(
            ebay_frame,
            "sku",
        )

        amount_detected = detect_column(
            ebay_frame,
            "amount",
        )

        order_detected = detect_column(
            ebay_frame,
            "order",
        )

        sku_col = st.selectbox(
            "eBay: SKU-Spalte",
            cols,
            index=(
                cols.index(sku_detected)
                if sku_detected in cols
                else 0
            ),
        )

        amount_col = st.selectbox(
            "eBay: Netto-Spalte",
            cols,
            index=(
                cols.index(amount_detected)
                if amount_detected in cols
                else 0
            ),
        )

        order_options = [
            "— nicht verwenden —",
            *cols,
        ]

        order_col = st.selectbox(
            "eBay: Bestellnummer",
            order_options,
            index=(
                order_options.index(order_detected)
                if order_detected in order_options
                else 0
            ),
        )

        wahan_order_col = None
        wahan_sku_col = None

        if wahan_result is not None:
            wahan_columns = list(
                wahan_result.frame.columns
            )

            detected_wahan_order = detect_column(
                wahan_result.frame,
                "order",
            )

            detected_wahan_sku = detect_column(
                wahan_result.frame,
                "sku",
            )

            st.write(
                f"Wahan: Headerzeile "
                f"{wahan_result.header_row + 1}"
            )

            wahan_order_col = st.selectbox(
                "Wahan: Bestellnummer",
                wahan_columns,
                index=(
                    wahan_columns.index(
                        detected_wahan_order
                    )
                    if detected_wahan_order
                    in wahan_columns
                    else 0
                ),
            )

            wahan_sku_col = st.selectbox(
                "Wahan: SKU-Spalte",
                wahan_columns,
                index=(
                    wahan_columns.index(
                        detected_wahan_sku
                    )
                    if detected_wahan_sku
                    in wahan_columns
                    else 0
                ),
            )

    if not sku_detected or not amount_detected:
        st.warning(
            "Die SKU- oder Netto-Spalte wurde nicht sicher "
            "erkannt. Bitte die Spaltenzuordnung prüfen."
        )

    transactions, unassigned = (
        prepare_transactions_detailed(
            ebay_frame,
            sku_col,
            amount_col,
            (
                None
                if order_col == "— nicht verwenden —"
                else order_col
            ),
            (
                wahan_result.frame
                if wahan_result is not None
                else None
            ),
            wahan_order_col,
            wahan_sku_col,
        )
    )

    if not transactions.empty:
        group_a, group_b = calculate_overviews(
            transactions
        )

tab_a, tab_b, tab_unassigned, tab_all = st.tabs(
    (
        "Gruppe A (Direkt)",
        "Gruppe B (Über Dich)",
        "Ohne Zuordnung",
        "Alle Daten",
    )
)

with tab_a:
    st.subheader("Direkt-Partner")

    st.caption(
        "PP, BA, MK und 001 · Netto-Umsatz abzüglich "
        "0,5 % Provision · kein Lexware-Upload"
    )

    if group_a.empty:
        if ebay_files:
            st.info(
                "Keine Datensätze für Gruppe A vorhanden."
            )
        else:
            st.info(
                "Bitte zuerst eine eBay-Auszahlung hochladen."
            )

    else:
        group_a_columns = [
            "SKU",
            "Netto-Umsatz",
            "Provision/Rabatt 0,5 %",
            "Abrechnung nach 0,5 %",
        ]

        group_a_view = group_a[group_a_columns]

        st.dataframe(
            group_a_view,
            use_container_width=True,
            hide_index=True,
        )

        metric_left, metric_right = st.columns(2)

        metric_left.metric(
            "Netto gesamt",
            (
                f"{group_a_view['Netto-Umsatz'].sum():,.2f} €"
            ),
        )

        metric_right.metric(
            "Abrechnung gesamt",
            (
                f"{group_a_view[
                    'Abrechnung nach 0,5 %'
                ].sum():,.2f} €"
            ),
        )

        st.markdown("**CSV je Partner-SKU**")

        download_columns = st.columns(3)

        for index, (_, row) in enumerate(
            group_a_view.iterrows()
        ):
            filename = safe_filename(row["SKU"])

            download_columns[
                index % 3
            ].download_button(
                label=f"{row['SKU']}.csv",
                data=csv_bytes(
                    pd.DataFrame([row])
                ),
                file_name=(
                    f"gruppe_a_{filename}.csv"
                ),
                mime="text/csv",
                key=f"group-a-{index}",
            )

with tab_b:
    st.subheader("Evelyn-Gesamtübersicht")

    st.caption(
        "NB und alle übrigen Standard-SKUs · "
        "0,5 % Rabatt"
    )

    if group_b.empty:
        if ebay_files:
            st.info(
                "Keine Datensätze für Gruppe B vorhanden."
            )
        else:
            st.info(
                "Bitte zuerst eine eBay-Auszahlung hochladen."
            )

    else:
        summary_columns = [
            "SKU",
            "Netto-Umsatz",
            "Provision/Rabatt 0,5 %",
            "Abrechnung nach 0,5 %",
        ]

        st.dataframe(
            group_b[summary_columns],
            use_container_width=True,
            hide_index=True,
        )

        metric_left, metric_right = st.columns(2)

        metric_left.metric(
            "Netto-Umsatz",
            f"{group_b['Netto-Umsatz'].sum():,.2f} €",
        )

        metric_right.metric(
            "Nach 0,5 % Rabatt",
            (
                f"{group_b[
                    'Abrechnung nach 0,5 %'
                ].sum():,.2f} €"
            ),
        )

        st.download_button(
            label="Evelyn-Gesamtübersicht als CSV",
            data=csv_bytes(
                group_b[summary_columns]
            ),
            file_name="gruppe_b_evelyn_gesamt.csv",
            mime="text/csv",
        )

        create_invoice = st.button(
            "Rechnungsentwurf in Lexware Office erstellen",
            type="primary",
            disabled=not api_key,
            help=(
                f"Entwurf für Kundennummer "
                f"{CUSTOMER_NUMBER}"
            ),
        )

        if create_invoice:
            try:
                contact = find_customer(
                    api_key,
                    CUSTOMER_NUMBER,
                )

                payload = build_invoice_payload(
                    group_b,
                    contact["id"],
                    invoice_date,
                    tax_rate,
                )

                result = create_draft(
                    api_key,
                    payload,
                )

                invoice_id = result.get(
                    "id",
                    "unbekannt",
                )

                st.success(
                    "Rechnungsentwurf erstellt. "
                    f"ID: {invoice_id}"
                )

                if result.get("id"):
                    st.link_button(
                        "Entwurf in Lexware Office öffnen",
                        (
                            "https://app.lexware.de/"
                            "permalink/invoices/view/"
                            f"{result['id']}"
                        ),
                    )

            except LexwareError as exc:
                st.error(str(exc))

            except Exception as exc:
                st.error(
                    f"Unerwarteter API-Fehler: {exc}"
                )

        if not api_key:
            st.caption(
                "Für den Upload bitte links den "
                "Lexware-Office-API-Key eingeben."
            )

        st.divider()

        st.subheader(
            "Einzelabrechnungen für dich & Patrick"
        )

        st.caption(
            "Je SKU: Netto-Umsatz abzüglich "
            "3,5 % Provision"
        )

        detail_columns = [
            "SKU",
            "Netto-Umsatz",
            "Provision 3,5 %",
            "Abrechnung nach 3,5 %",
        ]

        st.dataframe(
            group_b[detail_columns],
            use_container_width=True,
            hide_index=True,
        )

        download_columns = st.columns(3)

        for index, (_, row) in enumerate(
            group_b[detail_columns].iterrows()
        ):
            filename = safe_filename(row["SKU"])

            download_columns[
                index % 3
            ].download_button(
                label=f"{row['SKU']}.csv",
                data=csv_bytes(
                    pd.DataFrame([row])
                ),
                file_name=(
                    f"gruppe_b_{filename}.csv"
                ),
                mime="text/csv",
                key=f"group-b-{index}",
            )

with tab_unassigned:
    st.subheader("Ohne Zuordnung")

    st.caption(
        "Zeilen ohne verwertbare SKU oder "
        "gültigen Netto-Betrag"
    )

    if unassigned.empty:
        if ebay_files:
            st.info(
                "Keine unzugeordneten Zeilen vorhanden."
            )
        else:
            st.info(
                "Bitte zuerst eine eBay-Auszahlung hochladen."
            )

    else:
        st.warning(
            f"{len(unassigned)} Zeilen benötigen "
            "eine manuelle Prüfung."
        )

        st.dataframe(
            unassigned,
            use_container_width=True,
            hide_index=True,
        )

        st.download_button(
            label="Unzugeordnete Zeilen als CSV",
            data=csv_bytes(unassigned),
            file_name="ohne_zuordnung.csv",
            mime="text/csv",
        )

with tab_all:
    st.subheader("Alle Daten")

    st.caption(
        "Bereinigte, für die Abrechnung verwendete "
        "eBay-Datensätze"
    )

    if transactions.empty:
        if ebay_files:
            st.info(
                "Keine gültigen Datensätze vorhanden."
            )
        else:
            st.info(
                "Bitte zuerst eine eBay-Auszahlung hochladen."
            )

    else:
        st.dataframe(
            transactions,
            use_container_width=True,
            hide_index=True,
        )

        st.download_button(
            label="Alle bereinigten Daten als CSV",
            data=csv_bytes(transactions),
            file_name="ebay_alle_daten.csv",
            mime="text/csv",
        )
