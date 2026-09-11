-- Canonicalize earlier hold evidence IDs so historical and current Finances
-- observations upsert the same signal for a concrete line item.
update public.audit_case_signals
set external_id = coalesce(nullif(payload->>'transaction_id',''), 'unknown') || ':' || line_item_id
where source = 'hold';
