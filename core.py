from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass
from typing import BinaryIO

import pandas as pd


DIRECT_PREFIXES = ("PP", "BA", "MK", "001")

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
    frame.columns = [str(col).strip() for col in frame.columns]
    frame = frame.dropna(how="all").loc[:, ~frame.columns.str.startswith("Unnamed")]
    expected_lines = max(0, len(lines) - header_row - 1)
    return ParseResult(frame, header_row, delimiter, encoding, max(0, expected_lines - len(frame)))


def read_xlsx_robust(file: BinaryIO | bytes) -> ParseResult:
    """Read an Excel export and locate its real header below optional metadata rows."""
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
    frame = preview.iloc[header_row + 1 :].copy()
    frame.columns = header
    frame = frame.dropna(how="all").loc[:, ~frame.columns.str.startswith("Unnamed")]
    frame = frame.loc[:, [bool(str(column).strip()) for column in frame.columns]]
    return ParseResult(frame.reset_index(drop=True), header_row, "Excel", "binary", 0)


def read_upload(file: BinaryIO | bytes, filename: str) -> ParseResult:
    if filename.lower().endswith((".xlsx", ".xlsm")):
        return read_xlsx_robust(file)
    return read_csv_robust(file)


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


def prepare_transactions(
    ebay: pd.DataFrame,
    sku_column: str,
    amount_column: str,
    order_column: str | None = None,
    wahan: pd.DataFrame | None = None,
    wahan_order_column: str | None = None,
    wahan_sku_column: str | None = None,
) -> tuple[pd.DataFrame, int]:
    valid, unassigned = prepare_transactions_detailed(
        ebay, sku_column, amount_column, order_column, wahan, wahan_order_column, wahan_sku_column
    )
    return valid, len(unassigned)


def prepare_transactions_detailed(
    ebay: pd.DataFrame,
    sku_column: str,
    amount_column: str,
    order_column: str | None = None,
    wahan: pd.DataFrame | None = None,
    wahan_order_column: str | None = None,
    wahan_sku_column: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    work = pd.DataFrame({"SKU": ebay[sku_column].astype(str).str.strip(), "Netto-Umsatz": ebay[amount_column].map(parse_amount)})
    if order_column:
        work["Bestellnummer"] = ebay[order_column].astype(str).str.strip()
    if wahan is not None and order_column and wahan_order_column and wahan_sku_column:
        lookup = (wahan[[wahan_order_column, wahan_sku_column]].copy().drop_duplicates(wahan_order_column))
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
    return (
        work.loc[~invalid, valid_columns].reset_index(drop=True),
        work.loc[invalid].reset_index(drop=True),
    )


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
    return (
        grouped[grouped["Gruppe"].str.startswith("A")].reset_index(drop=True),
        grouped[grouped["Gruppe"].str.startswith("B")].reset_index(drop=True),
    )


def csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False, sep=";", decimal=",", encoding="utf-8-sig").encode("utf-8-sig")
