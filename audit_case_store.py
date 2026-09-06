"""Read-only access to the case-centric Trust/Risk data in Supabase."""
from __future__ import annotations

import os

import requests


class AuditStoreError(Exception):
    pass


def _required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise AuditStoreError(f"Supabase-Konfiguration fehlt: {name}")
    return value


def load():
    """Load only the three fixed review datasets; caller filters locally."""
    token = _required("SUPABASE_ACCESS_TOKEN")
    ref = _required("SUPABASE_PROJECT_REF")
    url = f"https://api.supabase.com/v1/projects/{ref}/database/query/read-only"
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    queries = {
        "cases": """select order_id,line_item_id,partner_id,sku,title,has_return,return_reason_de,
buyer_comment,has_message,has_dispute,has_hold,has_negative_feedback,first_event_at,last_contact_at,
case_status,not_as_described,wrong_item,defective,used_instead_of_new,opened_used,empty_consumed,
incomplete_parts,wrong_variant,item_not_received,other_complaint
from public.audit_cases order by last_contact_at desc nulls last,first_event_at desc nulls last,partner_id,sku""",
        "partners": "select * from public.audit_summary_by_partner order by problem_cases desc,partner_id",
        "skus": "select * from public.audit_summary_by_sku where problem_cases>1 order by problem_cases desc,partner_id,sku",
    }
    result = {}
    try:
        for name, query in queries.items():
            response = requests.post(url, headers=headers, json={"query": query, "parameters": []}, timeout=45)
            if response.status_code not in (200, 201):
                raise AuditStoreError(f"Supabase-Lesezugriff fehlgeschlagen (HTTP {response.status_code}).")
            data = response.json()
            rows = data if isinstance(data, list) else data.get("result")
            if not isinstance(rows, list):
                raise AuditStoreError("Supabase-Leseantwort ist unvollständig.")
            result[name] = rows
    except requests.RequestException:
        raise AuditStoreError("Supabase-Verbindung für die Trust/Risk-Prüfung fehlgeschlagen.") from None
    return result
