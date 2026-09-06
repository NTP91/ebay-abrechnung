-- Case-centric Trust/Risk storage. One problem is counted once per exact
-- order line, SKU and partner even when several evidence sources apply.

insert into public.partners(code, group_label) values ('FS', 'B')
on conflict (code) do nothing;

create table if not exists public.audit_cases (
    order_id text not null,
    line_item_id text not null,
    sku text not null,
    partner_id text not null references public.partners(code) on update cascade,
    title text not null default '',
    has_return boolean not null default false,
    return_reason_de text not null default '',
    buyer_comment text not null default '',
    has_message boolean not null default false,
    has_dispute boolean not null default false,
    has_hold boolean not null default false,
    has_negative_feedback boolean not null default false,
    first_event_at timestamptz,
    last_contact_at timestamptz,
    case_status text not null check (case_status in ('offen','geschlossen')),
    not_as_described boolean not null default false,
    wrong_item boolean not null default false,
    defective boolean not null default false,
    used_instead_of_new boolean not null default false,
    opened_used boolean not null default false,
    empty_consumed boolean not null default false,
    incomplete_parts boolean not null default false,
    wrong_variant boolean not null default false,
    item_not_received boolean not null default false,
    other_complaint boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (order_id, line_item_id, sku, partner_id),
    check (order_id <> '' and line_item_id <> '' and sku <> '' and partner_id <> '')
);

create table if not exists public.audit_case_signals (
    source text not null check (source in ('return','message','dispute','hold','negative_feedback')),
    external_id text not null,
    order_id text not null,
    line_item_id text not null,
    sku text not null,
    partner_id text not null,
    event_at timestamptz,
    status text not null default '',
    summary text not null default '',
    payload jsonb not null default '{}'::jsonb,
    imported_at timestamptz not null default now(),
    primary key (source, external_id, order_id, line_item_id, sku, partner_id),
    foreign key (order_id, line_item_id, sku, partner_id)
        references public.audit_cases(order_id, line_item_id, sku, partner_id) on update cascade
);

create table if not exists public.audit_unmatched_signals (
    source text not null,
    external_id text not null,
    reason text not null,
    payload jsonb not null default '{}'::jsonb,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    primary key (source, external_id)
);

create index if not exists audit_cases_partner_idx on public.audit_cases(partner_id);
create index if not exists audit_cases_sku_partner_idx on public.audit_cases(sku, partner_id);
create index if not exists audit_cases_status_idx on public.audit_cases(case_status);
create index if not exists audit_case_signals_case_idx on public.audit_case_signals(order_id, line_item_id, sku, partner_id);

drop trigger if exists audit_cases_set_updated_at on public.audit_cases;
create trigger audit_cases_set_updated_at before update on public.audit_cases
for each row execute function public.set_updated_at();

create or replace view public.audit_summary_by_partner with (security_invoker=true) as
select partner_id,
 count(*) as problem_cases,
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
from public.audit_cases
where has_return or has_message or has_dispute or has_hold or has_negative_feedback
group by partner_id;

create or replace view public.audit_summary_by_sku with (security_invoker=true) as
select sku, partner_id,
 count(*) as problem_cases,
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
where has_return or has_message or has_dispute or has_hold or has_negative_feedback
group by sku, partner_id;

alter table public.audit_cases enable row level security;
alter table public.audit_case_signals enable row level security;
alter table public.audit_unmatched_signals enable row level security;
revoke all on public.audit_cases, public.audit_case_signals, public.audit_unmatched_signals from anon, authenticated;
revoke all on public.audit_summary_by_partner, public.audit_summary_by_sku from anon, authenticated;
