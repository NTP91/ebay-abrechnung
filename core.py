from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import BinaryIO
from zoneinfo import ZoneInfo

import pandas as pd
import requests

DIRECT_PREFIXES = ("PP", "BA", "MK", "001")
BASE_URL = "https://api.lexware.io/v1"
ALIASES = {
    "sku": ("sku", "artikelnummer", "artikel nr", "item id", "custom label", "bestandseinheit"),
    "amount": ("netto", "net amount", "nettobetrag", "betrag netto", "netto umsatz", "auszahlungsbetrag"),
    "order": ("bestellnummer", "order number", "order id", "bestell nr", "auftragsnummer"),
}


@dataclass(frozen=True)
class ParseResult:
    frame: pd.DataFrame
    header_row: int
    delimiter: str
    encoding: str
    skipped_rows: int


class LexwareError(RuntimeError):
    pass


def _norm(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _decode(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace"), "utf-8"


def _delimiter(lines: list[str]) -> str:
    sample = "\n".join(lines[:30])
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
    except csv.Error:
        counts = {char: sample.count(char) for char in (";", ",", "\t", "|")}
        return max(counts, key=counts.get)


def _header_score(cells: list[str]) -> int:
    normalized = [_norm(cell) for cell in cells]
    exact = {alias for aliases in ALIASES.values() for alias in aliases}
    return sum(3 for cell in normalized if cell in exact) + sum(
        1 for cell in normalized if any(word in cell for word in ("sku", "bestell", "order", "netto", "amount"))
    )


def read_csv_robust(file: BinaryIO | bytes) -> ParseResult:
    raw = file if isinstance(file, bytes) else file.read()
    text, encoding = _decode(raw)
    lines = text.splitlines()
    delimiter = _delimiter(lines)
    scored = []
    for index, line in enumerate(lines[:80]):
        try:
            cells = next(csv.reader([line], delimiter=delimiter))
        except csv.Error:
            continue
        scored.append((_header_score(cells), len(cells), index))
    header_row = max(scored, default=(0, 0, 0))[2]
    frame = pd.read_csv(
        io.StringIO(text), sep=delimiter, header=header_row, dtype=str,
        on_bad_lines="skip", engine="python", keep_default_na=False,
    )
    frame.columns = [str(column).strip() for column in frame.columns]
    frame = frame.dropna(how="all").loc[:, ~frame.columns.str.startswith("Unnamed")]
    expected_lines = max(0, len(lines) - header_row - 1)
    return ParseResult(frame, header_row, delimiter, encoding, max(0, expected_lines - len(frame)))


def read_xlsx_robust(file: BinaryIO | bytes) -> ParseResult:
    raw = file if isinstance(file, bytes) else file.read()
    preview = pd.read_excel(io.BytesIO(raw), header=None, dtype=str, keep_default_na=False)
    if preview.empty:
        return ParseResult(pd.DataFrame(), 0, "Excel", "binary", 0)
    candidates = []
    for index, row in preview.head(80).iterrows():
        cells = [str(value) for value in row.tolist() if str(value).strip()]
        candidates.append((_header_score(cells), len(cells), int(index)))
    header_row = max(candidates, default=(0, 0, 0))[2]
    header = [str(value).strip() for value in preview.iloc[header_row].tolist()]
    frame = preview.iloc[header_row + 1:].copy()
    frame.columns = header
    frame = frame.dropna(how="all").loc[:, ~frame.columns.str.startswith("Unnamed")]
    frame = frame.loc[:, [bool(str(column).strip()) for column in frame.columns]]
    return ParseResult(frame.reset_index(drop=True), header_row, "Excel", "binary", 0)


def read_upload(file: BinaryIO | bytes, filename: str) -> ParseResult:
    return read_xlsx_robust(file) if filename.lower().endswith((".xlsx", ".xlsm")) else read_csv_robust(file)


def combine_uploaded_frames(frames: list[pd.DataFrame]) -> tuple[pd.DataFrame, int]:
    """Combine payout exports and remove exact duplicate transaction rows."""
    if not frames:
        return pd.DataFrame(), 0
    combined = pd.concat(frames, ignore_index=True, sort=False)
    before = len(combined)
    combined = combined.drop_duplicates(ignore_index=True)
    return combined, before - len(combined)


def detect_column(frame: pd.DataFrame, kind: str) -> str | None:
    aliases = ALIASES[kind]
    normalized = {_norm(column): column for column in frame.columns}
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    for norm, original in normalized.items():
        if any(alias in norm for alias in aliases):
            return original
    return None


def parse_amount(value: object) -> float:
    text = str(value).strip().replace("€", "").replace("EUR", "").replace(" ", "")
    text = text.replace("−", "-").replace("'", "")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    elif "," in text:
        text = text.replace(".", "").replace(",", ".")
    text = re.sub(r"[^0-9.\-]", "", text)
    try:
        return float(text)
    except ValueError:
        return float("nan")


def prepare_transactions_detailed(
    ebay: pd.DataFrame, sku_column: str, amount_column: str, order_column: str | None = None,
    wahan: pd.DataFrame | None = None, wahan_order_column: str | None = None,
    wahan_sku_column: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    work = pd.DataFrame({"SKU": ebay[sku_column].astype(str).str.strip(), "Netto-Umsatz": ebay[amount_column].map(parse_amount)})
    if order_column:
        work["Bestellnummer"] = ebay[order_column].astype(str).str.strip()
    if wahan is not None and order_column and wahan_order_column and wahan_sku_column:
        lookup = wahan[[wahan_order_column, wahan_sku_column]].copy().drop_duplicates(wahan_order_column)
        lookup.columns = ["Bestellnummer", "Wahan-SKU"]
        lookup["Bestellnummer"] = lookup["Bestellnummer"].astype(str).str.strip()
        work = work.merge(lookup, on="Bestellnummer", how="left")
        blank = work["SKU"].isin(("", "nan", "None"))
        work.loc[blank, "SKU"] = work.loc[blank, "Wahan-SKU"]
        work = work.drop(columns="Wahan-SKU")
    missing_sku = work["SKU"].isin(("", "nan", "None"))
    missing_amount = work["Netto-Umsatz"].isna()
    invalid = missing_sku | missing_amount
    work["Grund"] = ""
    work.loc[missing_sku, "Grund"] = "SKU fehlt"
    work.loc[missing_amount, "Grund"] = work.loc[missing_amount, "Grund"].map(
        lambda value: f"{value}; Netto-Betrag ungültig" if value else "Netto-Betrag ungültig"
    )
    valid_columns = [column for column in work.columns if column != "Grund"]
    return work.loc[~invalid, valid_columns].reset_index(drop=True), work.loc[invalid].reset_index(drop=True)


def prepare_transactions(*args, **kwargs) -> tuple[pd.DataFrame, int]:
    valid, unassigned = prepare_transactions_detailed(*args, **kwargs)
    return valid, len(unassigned)


def calculate_overviews(transactions: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    grouped = transactions.groupby("SKU", as_index=False)["Netto-Umsatz"].sum()
    grouped["Gruppe"] = grouped["SKU"].str.upper().map(
        lambda sku: "A – Direkt-Partner" if sku.startswith(DIRECT_PREFIXES) else "B – Evelyn Kukulan"
    )
    grouped["Provision/Rabatt 0,5 %"] = grouped["Netto-Umsatz"] * 0.005
    grouped["Abrechnung nach 0,5 %"] = grouped["Netto-Umsatz"] * 0.995
    grouped["Provision 3,5 %"] = grouped["Netto-Umsatz"] * 0.035
    grouped["Abrechnung nach 3,5 %"] = grouped["Netto-Umsatz"] * 0.965
    money = [column for column in grouped.columns if "%" in column or column == "Netto-Umsatz"]
    grouped[money] = grouped[money].round(2)
    return grouped[grouped["Gruppe"].str.startswith("A")].reset_index(drop=True), grouped[grouped["Gruppe"].str.startswith("B")].reset_index(drop=True)


def csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig")


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json", "Content-Type": "application/json"}


def find_customer(api_key: str, customer_number: int = 16335) -> dict:
    response = requests.get(f"{BASE_URL}/contacts", params={"number": customer_number, "customer": "true"}, headers=_headers(api_key), timeout=20)
    if not response.ok:
        raise LexwareError(f"Kontaktabfrage fehlgeschlagen ({response.status_code}): {response.text[:500]}")
    contacts = response.json().get("content", [])
    matches = [contact for contact in contacts if contact.get("roles", {}).get("customer", {}).get("number") == customer_number]
    if len(matches) != 1:
        raise LexwareError(f"Kundennummer {customer_number} wurde nicht eindeutig gefunden ({len(matches)} Treffer).")
    return matches[0]


def build_invoice_payload(group_b: pd.DataFrame, contact_id: str, invoice_date: date, tax_rate: float = 19.0) -> dict:
    if group_b.empty:
        raise LexwareError("Gruppe B enthält keine abrechenbaren Positionen.")
    timestamp = datetime.combine(invoice_date, datetime.min.time(), tzinfo=ZoneInfo("Europe/Berlin")).isoformat(timespec="milliseconds")
    items = [{
        "type": "custom", "name": f"eBay-Abrechnung SKU {row['SKU']}", "quantity": 1,
        "unitName": "Stück", "unitPrice": {"currency": "EUR", "netAmount": round(float(row["Netto-Umsatz"]), 2), "taxRatePercentage": tax_rate},
        "discountPercentage": 0.5,
    } for _, row in group_b.iterrows()]
    return {
        "voucherDate": timestamp, "address": {"contactId": contact_id}, "lineItems": items,
        "totalPrice": {"currency": "EUR"}, "taxConditions": {"taxType": "net"},
        "paymentConditions": {"paymentTermLabel": "Zahlbar sofort", "paymentTermDuration": 0},
        "shippingConditions": {"shippingDate": timestamp, "shippingType": "service"},
        "title": "Rechnung", "introduction": "eBay-Abrechnung gemäß beigefügter Übersicht.",
        "remark": "Diese Rechnung wurde als Entwurf über das eBay Payment Tool erstellt.",
    }


def create_draft(api_key: str, payload: dict) -> dict:
    response = requests.post(f"{BASE_URL}/invoices", headers=_headers(api_key), json=payload, timeout=30)
    if not response.ok:
        raise LexwareError(f"Rechnungsentwurf konnte nicht erstellt werden ({response.status_code}): {response.text[:1000]}")
    return response.json()
