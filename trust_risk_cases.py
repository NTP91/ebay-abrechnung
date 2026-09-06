"""Pure case-centric normalization for Trust/Risk evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re


REASON_DE = {
    "NOT_AS_DESCRIBED": "Artikel entspricht nicht der Beschreibung",
    "WRONG_ITEM": "Falscher Artikel geliefert",
    "DEFECTIVE": "Artikel defekt",
    "DEFECTIVE_ITEM": "Artikel defekt",
    "ARRIVED_DAMAGED": "Artikel beschädigt angekommen",
    "MISSING_PARTS": "Teile fehlen",
    "DOES_NOT_FIT": "Falsche Variante oder passt nicht",
    "ITEM_NOT_RECEIVED": "Artikel nicht erhalten",
    "WRONG_SIZE": "Falsche Größe bestellt",
    "ORDERED_DIFFERENT_ITEM": "Anderen Artikel bestellt",
    "WITHDRAW_FROM_PURCHASE_CONTRACT": "Widerruf des Kaufvertrags",
    "NO_LONGER_NEED_ITEM": "Artikel wird nicht mehr benötigt",
    "ORDERED_ACCIDENTALLY": "Versehentlich bestellt",
}

PATTERNS = {
    "not_as_described": ("not as described", "nicht wie beschrieben", "entspricht nicht"),
    "wrong_item": ("wrong item", "falscher artikel", "falschliefer"),
    "defective": ("defect", "defekt", "beschädigt", "damaged", "funktioniert nicht"),
    "used_instead_of_new": ("used instead of new", "gebraucht statt neu", "als gebraucht"),
    "opened_used": ("opened", "geöffnet", "benutzt", "used"),
    "empty_consumed": ("empty", "leer", "verbraucht", "aufgebraucht"),
    "incomplete_parts": ("missing parts", "teile fehlen", "unvollständig", "incomplete"),
    "wrong_variant": ("wrong variant", "falsche variante", "passt nicht", "does not fit"),
    "item_not_received": ("not received", "nicht erhalten", "nicht angekommen", "item not received"),
}
CATEGORY_NAMES = tuple(PATTERNS) + ("other_complaint",)


def partner_from_sku(sku):
    value = str(sku or "").split("/")[0].strip().upper()
    return "MH" if value.startswith("MH") else value


def line_reference(value):
    value = str(value or "").strip()
    if not value:
        return ""
    # Trading OrderLineItemID commonly uses legacyItemId-transactionId.
    return value.rsplit("-", 1)[-1] if "-" in value else value


class OrderIndex:
    def __init__(self, orders):
        self.rows = []
        self.by_line, self.by_order, self.by_item = {}, {}, {}
        for row in orders:
            raw = row.get("raw_row") or {}
            line = raw.get("lineItem") or {}
            normalized = {
                "order_id": str(row.get("bestellnummer") or raw.get("orderId") or "").strip(),
                "line_item_id": str(row.get("transaktionsnummer") or line.get("lineItemId") or "").strip(),
                "item_id": str(row.get("artikelnummer") or line.get("legacyItemId") or "").strip(),
                "sku": str(row.get("sku") or line.get("sku") or "").strip(),
                "title": str(row.get("angebotstitel") or line.get("title") or "").strip(),
            }
            normalized["partner_id"] = partner_from_sku(normalized["sku"])
            if not all(normalized[key] for key in ("order_id", "line_item_id", "sku", "partner_id")):
                continue
            self.rows.append(normalized)
            self.by_line.setdefault(normalized["line_item_id"], []).append(normalized)
            self.by_order.setdefault(normalized["order_id"], []).append(normalized)
            if normalized["item_id"]:
                self.by_item.setdefault(normalized["item_id"], []).append(normalized)

    def resolve(self, signal):
        raw_line = str(signal.get("line_item_id") or signal.get("transaction_id") or "").strip()
        candidates = self.by_line.get(raw_line, []) if raw_line else []
        if not candidates and raw_line:
            candidates = self.by_line.get(line_reference(raw_line), [])
        if not candidates and raw_line:
            candidates = [row for known, rows in self.by_line.items()
                          if raw_line.endswith("-" + known) for row in rows]
        order = str(signal.get("order_id") or "").strip()
        item = str(signal.get("item_id") or "").strip()
        if not candidates and order:
            candidates = self.by_order.get(order, [])
        if not candidates and item:
            candidates = self.by_item.get(item, [])
        if order:
            candidates = [row for row in candidates if row["order_id"] == order]
        if item and len(candidates) > 1:
            candidates = [row for row in candidates if row["item_id"] == item]
        unique = {case_key(row): row for row in candidates}
        return next(iter(unique.values())) if len(unique) == 1 else None


def case_key(row):
    return tuple(row[key] for key in ("order_id", "line_item_id", "sku", "partner_id"))


def category_flags(*texts):
    text = " ".join(str(value or "") for value in texts).casefold().replace("_", " ")
    flags = {name: any(term in text for term in terms) for name, terms in PATTERNS.items()}
    flags["other_complaint"] = bool(text.strip()) and not any(flags.values())
    return flags


def safe_payload(value):
    """Keep useful evidence while excluding invalid/unbounded transport text."""
    if isinstance(value, dict):
        return {str(key): safe_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [safe_payload(item) for item in value]
    if isinstance(value, str):
        return value.replace("\x00", "")[:12000]
    return value


def normalize_events(orders, signals):
    index = OrderIndex(orders)
    cases, evidence, unmatched, seen_evidence, seen_unmatched = {}, [], [], set(), set()
    for source, signal in signals:
        external_id = str(signal.get("external_id") or "").strip()
        matched = index.resolve(signal)
        if not external_id or not matched:
            if not external_id:
                external_id = "hash:" + hashlib.sha256(json.dumps(safe_payload(signal), sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
            unmatched_key = (source, external_id)
            if unmatched_key not in seen_unmatched:
                seen_unmatched.add(unmatched_key)
                unmatched.append({"source": source, "external_id": external_id,
                                  "reason": "Keine eindeutige Line-Item-/SKU-/Partnerzuordnung", "payload": safe_payload(signal)})
            continue
        key = case_key(matched)
        evidence_key = (source, external_id, *key)
        if evidence_key in seen_evidence:
            continue
        seen_evidence.add(evidence_key)
        case = cases.setdefault(key, {**matched, "has_return": False, "has_message": False,
            "has_dispute": False, "has_hold": False, "has_negative_feedback": False,
            "return_reason_de": "", "buyer_comment": "", "first_event_at": None,
            "last_contact_at": None, "case_status": "geschlossen", **{name: False for name in CATEGORY_NAMES}})
        signal_flag = {"return": "has_return", "message": "has_message", "dispute": "has_dispute",
                       "hold": "has_hold", "negative_feedback": "has_negative_feedback"}[source]
        case[signal_flag] = True
        reason = str(signal.get("reason") or "").replace("\x00", "")[:12000]
        comment = str(signal.get("buyer_comment") or signal.get("text") or "").replace("\x00", "")[:12000]
        if source == "return":
            case["return_reason_de"] = REASON_DE.get(reason.upper(), reason)
            case["buyer_comment"] = comment or case["buyer_comment"]
        if source != "hold":
            for name, value in category_flags(reason, comment).items():
                case[name] = case[name] or value
        event_at = signal.get("event_at")
        if isinstance(event_at, dict):
            event_at = event_at.get("value")
        event_at = str(event_at or "").strip() or None
        if event_at:
            case["first_event_at"] = min(filter(None, (case["first_event_at"], event_at)))
            if source == "message":
                case["last_contact_at"] = max(filter(None, (case["last_contact_at"], event_at)))
        status = str(signal.get("status") or "").upper()
        if source == "hold" or status not in ("CLOSED", "RESOLVED", "COMPLETED", "RETURN_CLOSED"):
            case["case_status"] = "offen"
        evidence.append({"source": source, "external_id": external_id, **matched,
                         "event_at": event_at, "status": status, "summary": reason or comment,
                         "payload": safe_payload(signal)})
    return {"cases": list(cases.values()), "signals": evidence, "unmatched": unmatched}


def return_signals(rows):
    for row in rows:
        creation = row.get("creationInfo") or {}
        item = creation.get("item") or {}
        comments = creation.get("comments") or row.get("comments") or []
        comment = "\n".join(str(value.get("content") or "") for value in comments if isinstance(value, dict)).strip()
        yield "return", {"external_id": row.get("returnId"), "order_id": row.get("orderId"),
            "line_item_id": item.get("transactionId") or item.get("lineItemId"), "item_id": item.get("itemId"),
            "reason": creation.get("reason") or creation.get("reasonType"), "buyer_comment": comment,
            "event_at": creation.get("creationDate") or row.get("creationDate"), "status": row.get("state") or row.get("status")}


def hold_signals(rows):
    for row in rows:
        raw = row.get("raw_observation") or row
        if raw.get("transactionStatus") not in ("FUNDS_ON_HOLD", "FUNDS_PROCESSING"):
            continue
        lines = raw.get("orderLineItems") or [{}]
        for index, line in enumerate(lines):
            line_id = line.get("lineItemId") or raw.get("transactionId") or "unknown"
            transaction_id = raw.get("transactionId") or row.get("transaction_id") or "unknown"
            yield "hold", {"external_id": f"{transaction_id}:{line_id}",
                "order_id": raw.get("orderId") or row.get("order_id"), "line_item_id": line.get("lineItemId"),
                "transaction_id": transaction_id, "transaction_type": raw.get("transactionType"), "item_id": line.get("legacyItemId"),
                "reason": raw.get("transactionMemo") or raw.get("transactionStatus"),
                "event_at": row.get("observed_at"), "status": raw.get("transactionStatus")}
