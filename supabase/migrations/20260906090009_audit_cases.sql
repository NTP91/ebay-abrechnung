-- Case-centric Trust/Risk evidence. Multiple source signals attach to one concrete order line.
create table if not exists public.audit_cases (
 order_id text not null, line_item_id text not null, sku text not null, partner_id text not null,
 title text not null default '', has_return boolean not null default false,
 return_reason_de text not null default '', buyer_comment text not null default '',
 has_message boolean not null default false, has_dispute boolean not null default false,
 has_hold boolean not null default false, has_negative_feedback boolean not null default false,
 first_event_at timestamptz, last_contact_at timestamptz,
 case_status text not null check(case_status in ('offen','geschlossen')),
 not_as_described boolean not null default false, wrong_item boolean not null default false,
 defective boolean not null default false, used_instead_of_new boolean not null default false,
 opened_used boolean not null default false, empty_consumed boolean not null default false,
 incomplete_parts boolean not null default false, wrong_variant boolean not null default false,
 item_not_received boolean not null default false, other_complaint boolean not null default false,
 is_problem boolean not null default false, created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(), primary key(order_id,line_item_id,sku,partner_id)
);
create table if not exists public.audit_case_signals (
 source text not null, external_id text not null, order_id text not null, line_item_id text not null,
 sku text not null, partner_id text not null, event_at timestamptz, status text not null default '',
 summary text not null default '', payload jsonb not null default '{}'::jsonb,
 imported_at timestamptz not null default now(), original_code text not null default '',
 original_text text not null default '', sender text not null default '', recipient text not null default '',
 sender_role text not null default 'unknown', reply_present boolean, attachment_present boolean,
 primary key(source,external_id,order_id,line_item_id,sku,partner_id), foreign key(order_id,line_item_id,sku,partner_id)
 references public.audit_cases(order_id,line_item_id,sku,partner_id) on delete cascade
);
create table if not exists public.audit_unmatched_signals (
 source text not null, external_id text not null, reason text not null,
 payload jsonb not null default '{}'::jsonb, first_seen_at timestamptz not null default now(),
 last_seen_at timestamptz not null default now(), primary key(source,external_id)
);
create index if not exists audit_cases_partner_idx on public.audit_cases(partner_id);
create index if not exists audit_cases_sku_idx on public.audit_cases(sku,partner_id);
create index if not exists audit_signals_case_idx on public.audit_case_signals(order_id,line_item_id,sku,partner_id);
create or replace view public.audit_summary_by_partner with(security_invoker=true) as
 select partner_id,count(*) filter(where is_problem) problem_cases,
 count(*) filter(where has_return) returns,count(*) filter(where has_message) messages,
 count(*) filter(where has_dispute) disputes,count(*) filter(where has_hold) holds,
 count(*) filter(where has_negative_feedback) negative_feedback,
 count(*) filter(where not_as_described) not_as_described,count(*) filter(where wrong_item) wrong_item,
 count(*) filter(where defective) defective,count(*) filter(where used_instead_of_new or opened_used) used_opened,
 count(distinct order_id) filter(where is_problem) affected_orders,
 count(distinct sku) filter(where is_problem and sku<>'') affected_skus
 from public.audit_cases group by partner_id;
create or replace view public.audit_summary_by_sku with(security_invoker=true) as
 select sku,partner_id,count(*) filter(where is_problem) problem_cases,
 count(*) filter(where not_as_described) not_as_described,count(*) filter(where wrong_item) wrong_item,
 count(*) filter(where defective) defective,count(*) filter(where used_instead_of_new or opened_used) used_opened,
 count(*) filter(where is_problem) repeat_count from public.audit_cases where sku<>'' group by sku,partner_id;
alter table public.audit_cases enable row level security;
alter table public.audit_case_signals enable row level security;
alter table public.audit_unmatched_signals enable row level security;
revoke all on public.audit_cases,public.audit_case_signals,public.audit_unmatched_signals from anon,authenticated;
revoke all on public.audit_summary_by_partner,public.audit_summary_by_sku from anon,authenticated;
