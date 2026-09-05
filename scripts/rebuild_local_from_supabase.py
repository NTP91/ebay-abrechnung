"""One-time, idempotent reconstruction of the legacy local runtime from Supabase.

Supabase remains authoritative.  Reads use the Management API read-only SQL
endpoint.  The only remote write this utility may perform is restoring the
historical RE0089 discarded-test marker when it is absent.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


EXPECTED_HOLDS = {"08-15103-42438", "06-15117-56051"}
ALLOWED_FINANCE_TYPES = {"SALE", "REFUND", "NON_SALE_CHARGE"}


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Umgebungsvariable fehlt: {name}")
    return value


class Management:
    def __init__(self) -> None:
        self.token = required_env("SUPABASE_ACCESS_TOKEN")
        self.ref = required_env("SUPABASE_PROJECT_REF")
        self.base = f"https://api.supabase.com/v1/projects/{self.ref}/database"
        self.headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def read(self, query: str) -> list[dict]:
        response = requests.post(
            self.base + "/query/read-only",
            headers=self.headers,
            json={"query": query, "parameters": []},
            timeout=90,
        )
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Supabase-Lesezugriff fehlgeschlagen (HTTP {response.status_code}).")
        data = response.json()
        return data if isinstance(data, list) else data.get("result", [])

    def insert_discarded_once(self, record: dict) -> bool:
        existing = self.read(
            "select invoice_id from public.discarded_invoices "
            f"where invoice_id='{sql_text(record['invoice_id'])}'"
        )
        if existing:
            return False
        encoded = base64.b64encode(json.dumps(record["snapshot"], ensure_ascii=False).encode()).decode()
        query = (
            "insert into public.discarded_invoices(invoice_id,label,discarded_at,snapshot) values ("
            f"'{sql_text(record['invoice_id'])}','{sql_text(record['label'])}',"
            f"'{sql_text(record['discarded_at'])}'::timestamptz,"
            f"convert_from(decode('{encoded}','base64'),'UTF8')::jsonb) "
            "on conflict(invoice_id) do nothing"
        )
        name = "restore_re0089_discarded_test_marker"
        uri = self.base + "/migrations"
        response = requests.post(uri, headers=self.headers, json={"query": query, "name": name}, timeout=180)
        history = requests.get(uri, headers={"Authorization": f"Bearer {self.token}"}, timeout=45)
        confirmed = history.status_code == 200 and any(row.get("name") == name for row in history.json())
        if not confirmed:
            raise RuntimeError(f"RE0089-Status nicht bestätigt (HTTP {response.status_code}).")
        return True


def sql_text(value: object) -> str:
    return str(value).replace("'", "''")


def atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", delete=False, dir=path.parent) as out:
        temporary = Path(out.name)
        frame.to_csv(out, sep=";", index=False)
    os.replace(temporary, path)


def atomic_json(document: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as out:
        temporary = Path(out.name)
        json.dump(document, out, ensure_ascii=False, default=str)
    os.replace(temporary, path)


def amount_value(value: object) -> str:
    return "" if value is None else format(value, "f") if hasattr(value, "as_tuple") else str(value)


def api_amount(container: object) -> str:
    return amount_value(container.get("value")) if isinstance(container, dict) else ""


def report_date(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%d.%m.%Y")
    except ValueError:
        return text


def order_frame(rows: list[dict]) -> pd.DataFrame:
    result = []
    for row in rows:
        raw = row.get("raw_row") or {}
        line = raw.get("lineItem") or {}
        result.append({
            "Bestellnummer": row["bestellnummer"],
            "Transaktionsnummer": row["transaktionsnummer"],
            "Artikelnummer": row["artikelnummer"],
            "SKU": row["sku"],
            "Angebotstitel": row["angebotstitel"],
            "Auszahlung Nr.": "",
            "Betrag abzügl. Kosten": "",
            "Typ": "Bestellung",
            "Datum": "",
            "Verkauft am": report_date(raw.get("creationDate", "")),
            "Anzahl": line.get("quantity", ""),
            "Verkauft für": (line.get("lineItemCost") or {}).get("value", ""),
            "Verpackung und Versand": (line.get("deliveryCost") or {}).get("value", ""),
            "Gesamtbetrag": (line.get("total") or {}).get("value", ""),
        })
    return pd.DataFrame(result)


def payout_frame(rows: list[dict], orders: pd.DataFrame, sync_state: dict) -> pd.DataFrame:
    by_id = {int(row["id"]): row for row in rows}
    by_line = {str(row.Transaktionsnummer): row for row in orders.itertuples()}
    payout_details = sync_state.get("payouts") or {}
    result = []
    for row in rows:
        parent = by_id.get(row.get("parent_transaction_id")) if row.get("is_child_reference") else row
        raw = (parent or {}).get("raw_row") or {}
        native = raw.get("transactionType")
        if native not in ALLOWED_FINANCE_TYPES:
            continue
        child = bool(row.get("is_child_reference"))
        match = by_line.get(str(row.get("transaktionsnummer") or ""))
        payout_id = str(row.get("auszahlung_nr") or "")
        details = payout_details.get(payout_id) or {}
        gross = amount_value(row.get("transaktionsbetrag_inkl_kosten"))
        if native == "SALE":
            gross = api_amount(raw.get("totalFeeBasisAmount")) or gross
        elif native == "REFUND":
            refund_basis = api_amount(raw.get("totalFeeBasisAmount"))
            gross = ("-" + refund_basis.lstrip("-")) if refund_basis else gross
            gross = gross or amount_value(row.get("betrag_abzueglich_kosten"))
        elif native == "NON_SALE_CHARGE":
            gross = gross or amount_value(row.get("betrag_abzueglich_kosten"))
        result.append({
            "Bestellnummer": row.get("bestellnummer") or "",
            "Transaktionsnummer": row.get("transaktionsnummer") or "",
            "Artikelnummer": row.get("artikelnummer") or "",
            "SKU": getattr(match, "SKU", "") if match else "",
            "Angebotstitel": getattr(match, "Angebotstitel", "") if match else "",
            "Auszahlung Nr.": payout_id,
            "Betrag abzügl. Kosten": "" if child else amount_value(row.get("betrag_abzueglich_kosten")),
            "Typ": row.get("typ") or "",
            "Datum": report_date(row.get("datum")),
            "Auszahlungsdatum": report_date(details.get("payoutDate", "")),
            "Auszahlungsstatus": row.get("auszahlungsstatus") or "",
            "Stückzahl": 1,
            "Zwischensumme Artikel": amount_value(row.get("zwischensumme_artikel")),
            "Verpackung und Versand": amount_value(row.get("verpackung_und_versand")),
            "Transaktionsbetrag (inkl. Kosten)": "" if child else gross,
            "Referenznummer": row.get("referenznummer") or "",
            "API_Transaktion": raw.get("api_identity", ""),
            "API_Artikelreferenzen": json.dumps(raw.get("orderLineItems") or [], ensure_ascii=False),
            "Importquelle": "eBay API · Supabase",
        })
    return pd.DataFrame(result)


def hold_document(rows: list[dict]) -> dict:
    fields = {
        "order_id": "orderId", "transaction_id": "transactionId", "transaction_type": "transactionType",
        "transaction_status": "transactionStatus", "transaction_date": "transactionDate",
        "booking_entry": "bookingEntry", "amount": "amount", "payout_id": "payoutId",
        "references": "references", "transaction_memo": "transactionMemo",
    }
    observations = []
    seen = set()
    for row in rows:
        raw = row.get("raw_observation") or {}
        transaction = {target: raw.get(target, row[source]) for source, target in fields.items()
                       if raw.get(target, row.get(source)) not in (None, "")}
        key = json.dumps([row.get("observed_at"), transaction], sort_keys=True, ensure_ascii=False, default=str)
        if key in seen:
            continue
        seen.add(key)
        observations.append({"at": str(row["observed_at"]), "transaction": transaction})
    return {"version": 1, "observations": observations}


def load_re0089(directory: Path) -> dict:
    database = directory / "Settlement_State.sqlite3"
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("select * from discarded_invoices where label like 'RE0089%'").fetchone()
    if not row:
        raise RuntimeError("Historischer RE0089-Teststatus fehlt im Recovery-Bestand.")
    record = dict(row)
    record["snapshot"] = json.loads(record["snapshot"])
    return record


def save_local_status(
    project: Path,
    discarded: dict,
    holds: dict,
    sync: dict,
    payouts: list[dict],
) -> None:
    atomic_json(holds, project / "Settlement_API_Holds.json")
    atomic_json(sync, project / "Settlement_Ebay_Sync.json")
    with sqlite3.connect(project / "Settlement_State.sqlite3") as db:
        db.execute("create table if not exists discarded_invoices(invoice_id text primary key,label text not null,discarded_at text not null,snapshot text not null)")
        db.execute("insert or ignore into discarded_invoices values(?,?,?,?)", (
            discarded["invoice_id"], discarded["label"], discarded["discarded_at"],
            json.dumps(discarded["snapshot"], ensure_ascii=False),
        ))
        db.execute("create table if not exists api_hold_evidence(id integer primary key,document text not null)")
        db.execute("insert or replace into api_hold_evidence values(1,?)", (json.dumps(holds, ensure_ascii=False),))
        db.execute("create table if not exists ebay_sync_state(id integer primary key,document text not null)")
        db.execute("insert or replace into ebay_sync_state values(1,?)", (
            json.dumps(sync, ensure_ascii=False, default=str),
        ))
        db.execute("""create table if not exists payouts(
            id text primary key, status text not null, fingerprint text,
            invoice_id text, attempt text, snapshot text
        )""")
        for payout in payouts:
            db.execute(
                "insert or ignore into payouts(id,status) values(?,?)",
                (str(payout["id"]), str(payout["status"])),
            )
        db.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--recovery-data-dir", type=Path, required=True)
    args = parser.parse_args()
    project = args.project_dir.resolve()
    management = Management()
    orders = management.read("select bestellnummer,transaktionsnummer,artikelnummer,sku,angebotstitel,raw_row from public.orders order by id")
    transactions = management.read("select id,auszahlung_nr,bestellnummer,transaktionsnummer,artikelnummer,typ,datum,betrag_abzueglich_kosten,zwischensumme_artikel,verpackung_und_versand,transaktionsbetrag_inkl_kosten,referenznummer,auszahlungsstatus,is_child_reference,parent_transaction_id,raw_row from public.payout_transactions order by id")
    payouts = management.read("select id,status from public.payouts order by id")
    state_rows = management.read("select watermark,payouts,transactions from public.ebay_sync_state where id=1")
    runs = management.read("select id,source,trigger,at,start_at,end_at,status,new_payouts,new_transactions,known,ledger_only,error,finished_at from public.ebay_sync_runs order by at")
    evidence = management.read("select observed_at,order_id,transaction_id,transaction_type,transaction_status,transaction_date,booking_entry,amount,payout_id,\"references\",transaction_memo,raw_observation from public.api_hold_evidence order by id")
    discarded = load_re0089(args.recovery_data_dir.resolve())
    present_holds = {row.get("order_id") for row in evidence if row.get("transaction_type") == "DISPUTE" and row.get("booking_entry") == "DEBIT"}
    missing_holds = EXPECTED_HOLDS - present_holds
    if missing_holds:
        raise RuntimeError("Supabase-Hold-Nachweise fehlen für: " + ", ".join(sorted(missing_holds)))
    order_data = order_frame(orders)
    payout_data = payout_frame(transactions, order_data, state_rows[0] if state_rows else {})
    state = state_rows[0] if state_rows else {}
    sync = {
        "version": 1,
        "watermark": state.get("watermark"),
        "payouts": state.get("payouts") or {},
        "transactions": state.get("transactions") or {},
        "runs": [{
            "id": str(row["id"]).replace("-", ""),
            "source": row["source"],
            "trigger": row["trigger"],
            "at": str(row["at"]),
            "start": str(row.get("start_at") or ""),
            "end": str(row.get("end_at") or ""),
            "status": row["status"],
            "new_payouts": row.get("new_payouts") or 0,
            "new_transactions": row.get("new_transactions") or 0,
            "known": row.get("known") or 0,
            "ledger_only": row.get("ledger_only") or 0,
            "error": row.get("error") or "",
            "finished_at": str(row.get("finished_at") or ""),
        } for row in runs],
    }
    import payout_structure
    payout_structure.validate(payout_data)
    management.insert_discarded_once(discarded)
    atomic_csv(order_data, project / "Master_Orders.csv")
    atomic_csv(payout_data, project / "Master_Payouts.csv")
    save_local_status(project, discarded, hold_document(evidence), sync, payouts)
    print(json.dumps({
        "orders": len(order_data), "payout_rows": len(payout_data),
        "payouts": int(payout_data.loc[payout_data["Auszahlung Nr."] != "", "Auszahlung Nr."].nunique()),
        "hold_orders": len(EXPECTED_HOLDS), "re0089": True,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
