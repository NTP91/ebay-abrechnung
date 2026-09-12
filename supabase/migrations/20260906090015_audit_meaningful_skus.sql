-- A partner prefix such as "NB /" is not a product SKU. Keep its cases and
-- evidence, but exclude it from SKU repetition analysis.
create or replace view public.audit_summary_by_partner with (security_invoker=true) as
select partner_id,
 count(*) as problem_cases,
 count(distinct order_id) as affected_orders,
 count(distinct sku) filter (where btrim(split_part(sku,'/',2)) <> '') as affected_skus,
 array_agg(distinct order_id order by order_id) as order_ids,
 array_agg(distinct sku order by sku) filter (where btrim(split_part(sku,'/',2)) <> '') as sku_list,
 count(*) filter (where has_return) as returns,
 count(*) filter (where has_message) as messages,
 count(*) filter (where has_dispute) as disputes,
 count(*) filter (where has_hold) as holds,
 count(*) filter (where has_negative_feedback) as negative_feedback,
 count(*) filter (where not_as_described) as not_as_described,
 count(*) filter (where wrong_item) as wrong_item,
 count(*) filter (where defective) as defective,
 count(*) filter (where used_instead_of_new) as used_instead_of_new,
 count(*) filter (where opened_used) as opened_used,
 count(*) filter (where empty_consumed) as empty_consumed,
 count(*) filter (where incomplete_parts) as incomplete_parts,
 count(*) filter (where wrong_variant) as wrong_variant,
 count(*) filter (where item_not_received) as item_not_received,
 count(*) filter (where other_complaint) as other_complaint
from public.audit_cases where is_problem group by partner_id;

create or replace view public.audit_summary_by_sku with (security_invoker=true) as
select sku,partner_id,
 count(*) as problem_cases,
 count(*) as repeat_count,
 count(distinct order_id) as affected_orders,
 array_agg(distinct order_id order by order_id) as order_ids,
 count(*) filter (where not_as_described) as not_as_described,
 count(*) filter (where wrong_item) as wrong_item,
 count(*) filter (where defective) as defective,
 count(*) filter (where used_instead_of_new) as used_instead_of_new,
 count(*) filter (where opened_used) as opened_used,
 count(*) filter (where empty_consumed) as empty_consumed,
 count(*) filter (where incomplete_parts) as incomplete_parts,
 count(*) filter (where wrong_variant) as wrong_variant,
 count(*) filter (where item_not_received) as item_not_received,
 count(*) filter (where other_complaint) as other_complaint
from public.audit_cases
where is_problem and btrim(split_part(sku,'/',2)) <> ''
group by sku,partner_id;

revoke all on public.audit_summary_by_partner,public.audit_summary_by_sku from anon,authenticated;
