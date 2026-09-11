-- Preserve original evidence and message workflow details without changing
-- settlement or billing data.
alter table public.audit_cases add column if not exists is_problem boolean not null default false;
alter table public.audit_case_signals add column if not exists original_code text not null default '';
alter table public.audit_case_signals add column if not exists original_text text not null default '';
alter table public.audit_case_signals add column if not exists sender text not null default '';
alter table public.audit_case_signals add column if not exists recipient text not null default '';
alter table public.audit_case_signals add column if not exists sender_role text not null default 'unknown'
  check (sender_role in ('buyer','seller','unknown'));
alter table public.audit_case_signals add column if not exists reply_present boolean;
alter table public.audit_case_signals add column if not exists attachment_present boolean;

-- Rebuild current classification from the complete fresh source window on
-- the next idempotent sync. This removes the former blanket classification
-- of every message as "other complaint".
update public.audit_cases set
  not_as_described=false, wrong_item=false, defective=false,
  used_instead_of_new=false, opened_used=false, empty_consumed=false,
  incomplete_parts=false, wrong_variant=false, item_not_received=false,
  other_complaint=false, is_problem=false;

drop view if exists public.audit_summary_by_partner;
drop view if exists public.audit_summary_by_sku;

create or replace view public.audit_summary_by_partner with (security_invoker=true) as
select partner_id,
 count(*) as problem_cases,
 count(distinct order_id) as affected_orders,
 count(distinct sku) as affected_skus,
 array_agg(distinct order_id order by order_id) as order_ids,
 array_agg(distinct sku order by sku) as sku_list,
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
select sku, partner_id,
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
from public.audit_cases where is_problem group by sku,partner_id;

revoke all on public.audit_summary_by_partner,public.audit_summary_by_sku from anon,authenticated;
