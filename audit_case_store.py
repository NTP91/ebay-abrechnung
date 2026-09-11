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
        "cases": """select c.order_id,c.line_item_id,c.partner_id,c.sku,c.title,c.has_return,c.return_reason_de,
c.buyer_comment,c.has_message,c.has_dispute,c.has_hold,c.has_negative_feedback,c.first_event_at,c.last_contact_at,
c.case_status,c.is_problem,c.not_as_described,c.wrong_item,c.defective,c.used_instead_of_new,c.opened_used,c.empty_consumed,
c.incomplete_parts,c.wrong_variant,c.item_not_received,c.other_complaint,
coalesce(s.problem_signal_count,0) problem_signal_count,coalesce(s.problem_sources,'') problem_sources
from public.audit_cases c left join (
 select order_id,line_item_id,sku,partner_id,
 count(*) filter(where source in ('return','dispute','negative_feedback') or (source='message' and summary<>'')) problem_signal_count,
 string_agg(distinct source,', ' order by source) filter(where source in ('return','dispute','negative_feedback') or (source='message' and summary<>'')) problem_sources
 from public.audit_case_signals group by order_id,line_item_id,sku,partner_id
) s using(order_id,line_item_id,sku,partner_id)
where c.is_problem order by c.last_contact_at desc nulls last,c.first_event_at desc nulls last,c.partner_id,c.sku""",
        "partners": "select * from public.audit_summary_by_partner order by problem_cases desc,partner_id",
        "skus": "select * from public.audit_summary_by_sku where problem_cases>1 order by problem_cases desc,partner_id,sku",
        "orders": "select bestellnummer,transaktionsnummer,artikelnummer,sku from public.orders",
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
