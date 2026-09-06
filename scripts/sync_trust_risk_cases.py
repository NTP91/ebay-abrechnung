"""Read eBay Trust/Risk evidence and idempotently persist strict line-item cases."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ebay_readonly import Client
from ebay_trading import TradingClient
from scripts.rebuild_local_from_supabase import Management
from trust_risk_cases import hold_signals, normalize_events, return_signals


def encoded(value):
    return base64.b64encode(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")).decode("ascii")


def json_recordset(blob, columns):
    return f"jsonb_to_recordset(convert_from(decode('{encoded(blob)}','base64'),'UTF8')::jsonb) as x({columns})"


def collect(manager, now=None):
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=90)
    orders = manager.read("select bestellnummer,transaktionsnummer,artikelnummer,sku,angebotstitel,raw_row from public.orders order by id")
    partners = {row["code"] for row in manager.read("select code from public.partners")} | {"FS"}
    orders = [row for row in orders if str(row.get("sku") or "").split("/")[0].strip().upper().replace("MH43", "MH") in partners
              or str(row.get("sku") or "").split("/")[0].strip().upper().startswith("MH")]
    holds = manager.read("select id,observed_at,order_id,transaction_id,transaction_status,raw_observation from public.api_hold_evidence order by id")

    rest = Client()
    returns = rest.pages("returns", "members", {
        "role": "SELLER",
        "creation_date_range_from": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "creation_date_range_to": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    })["items"]
    finance_transactions = rest.pages("transactions", "transactions", {
        "filter": f"transactionDate:[{start.strftime('%Y-%m-%dT%H:%M:%S.000Z')}..{now.strftime('%Y-%m-%dT%H:%M:%S.000Z')}]"
    })["items"]
    dispute_summaries = rest.pages("disputes", "paymentDisputeSummaries")["items"]
    disputes = []
    for summary in dispute_summaries:
        detail = rest.get("dispute", summary["paymentDisputeId"])
        disputes.append({**summary, **detail})

    trading = TradingClient(rest)
    messages = trading.message_history(start, now)
    feedback = trading.negative_feedback()

    # Live evidence wins when the same stable hold identity is also present
    # in the append-only historical evidence table.
    signals = list(return_signals(returns)) + list(hold_signals(finance_transactions)) + list(hold_signals(holds))
    for row in disputes:
        signals.append(("dispute", {"external_id": row.get("paymentDisputeId"), "order_id": row.get("orderId"),
            "line_item_id": row.get("orderLineItemId"), "transaction_id": row.get("transactionId"),
            "item_id": row.get("legacyItemId"), "reason": row.get("reason"),
            "event_at": row.get("openDate") or row.get("creationDate"), "status": row.get("paymentDisputeStatus")}))
    for row in messages["rows"]:
        signals.append(("message", row))
    for row in feedback:
        signals.append(("negative_feedback", row))

    normalized = normalize_events(orders, signals)
    normalized["cases"].sort(key=lambda row: (row["order_id"], row["line_item_id"], row["sku"], row["partner_id"]))
    normalized["signals"].sort(key=lambda row: (row["source"], row["external_id"], row["order_id"], row["line_item_id"]))
    normalized["unmatched"].sort(key=lambda row: (row["source"], row["external_id"]))
    normalized["coverage"] = {
        "orders": len(orders), "returns": len(returns), "disputes": len(disputes),
        "finance_transactions": len(finance_transactions),
        "holds": len(holds), "negative_feedback": len(feedback), "messages": messages["coverage"],
        "selected_message_api": messages["selected"], "window_start": start.isoformat(), "window_end": now.isoformat(),
    }
    return normalized


def sql_for(result):
    case_columns = """order_id text,line_item_id text,sku text,partner_id text,title text,
has_return boolean,return_reason_de text,buyer_comment text,has_message boolean,has_dispute boolean,
has_hold boolean,has_negative_feedback boolean,first_event_at timestamptz,last_contact_at timestamptz,case_status text,
is_problem boolean,
not_as_described boolean,wrong_item boolean,defective boolean,used_instead_of_new boolean,opened_used boolean,
empty_consumed boolean,incomplete_parts boolean,wrong_variant boolean,item_not_received boolean,other_complaint boolean"""
    case_names = [part.strip().split()[0] for part in case_columns.replace("\n", " ").split(",")]
    signal_columns = """source text,external_id text,order_id text,line_item_id text,sku text,partner_id text,
event_at timestamptz,status text,summary text,original_code text,original_text text,sender text,recipient text,
sender_role text,reply_present boolean,attachment_present boolean,payload jsonb"""
    signal_names = [part.strip().split()[0] for part in signal_columns.split(",")]
    unmatched_columns = "source text,external_id text,reason text,payload jsonb"
    unmatched_names = [part.strip().split()[0] for part in unmatched_columns.split(",")]
    cases = json_recordset(result["cases"], case_columns)
    signals = json_recordset(result["signals"], signal_columns)
    unmatched = json_recordset(result["unmatched"], unmatched_columns)
    bools = ["is_problem", "has_return", "has_message", "has_dispute", "has_hold", "has_negative_feedback",
             "not_as_described", "wrong_item", "defective", "used_instead_of_new", "opened_used",
             "empty_consumed", "incomplete_parts", "wrong_variant", "item_not_received", "other_complaint"]
    updates = [f"{name}=audit_cases.{name} or excluded.{name}" for name in bools]
    updates += ["title=case when excluded.title<>'' then excluded.title else audit_cases.title end",
                "return_reason_de=case when excluded.return_reason_de<>'' then excluded.return_reason_de else audit_cases.return_reason_de end",
                "buyer_comment=case when excluded.buyer_comment<>'' then excluded.buyer_comment else audit_cases.buyer_comment end",
                "first_event_at=least(audit_cases.first_event_at,excluded.first_event_at)",
                "last_contact_at=greatest(audit_cases.last_contact_at,excluded.last_contact_at)",
                "case_status=case when audit_cases.case_status='offen' or excluded.case_status='offen' then 'offen' else 'geschlossen' end"]
    return f"""
insert into public.audit_cases({','.join(case_names)}) select {','.join(case_names)} from {cases}
on conflict(order_id,line_item_id,sku,partner_id) do update set {','.join(updates)};
insert into public.audit_case_signals({','.join(signal_names)}) select {','.join(signal_names)} from {signals}
on conflict(source,external_id,order_id,line_item_id,sku,partner_id) do update
set event_at=excluded.event_at,status=excluded.status,summary=excluded.summary,
original_code=excluded.original_code,original_text=excluded.original_text,sender=excluded.sender,
recipient=excluded.recipient,sender_role=excluded.sender_role,reply_present=excluded.reply_present,
attachment_present=excluded.attachment_present,payload=excluded.payload;
insert into public.audit_unmatched_signals({','.join(unmatched_names)}) select {','.join(unmatched_names)} from {unmatched}
on conflict(source,external_id) do update set reason=excluded.reason,payload=excluded.payload,last_seen_at=now();
"""


def apply(manager, result):
    # The Management API has a strict request-body limit. Insert the parent
    # cases first, then evidence, so every small batch remains FK-safe and a
    # failed run can be resumed idempotently without deleting anything.
    for kind, size in (("cases", 40), ("signals", 10), ("unmatched", 10)):
        rows = result[kind]
        for offset in range(0, len(rows), size):
            batch = {"cases": [], "signals": [], "unmatched": []}
            batch[kind] = rows[offset:offset + size]
            response = manager_session_post(manager, sql_for(batch), f"sync_trust_risk_{kind}_{offset // size:03d}")
            if response.status_code not in (200, 201):
                raise RuntimeError(f"Supabase Trust/Risk-Sync fehlgeschlagen: {kind}, Batch {offset // size + 1} (HTTP {response.status_code}).")
            # The Management API versions migrations at second precision.
            time.sleep(1.05)


def manager_session_post(manager, query, name):
    import requests
    return requests.post(manager.base + "/migrations", headers=manager.headers,
                         json={"query": query, "name": name + "_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")}, timeout=180)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Persist normalized cases in Supabase")
    args = parser.parse_args()
    manager = Management()
    result = collect(manager)
    if args.apply:
        apply(manager, result)
    print(json.dumps({"applied": args.apply, "cases": len(result["cases"]), "signals": len(result["signals"]),
                      "unmatched": len(result["unmatched"]), "coverage": result["coverage"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
