-- Part 2: static reference/master data — mirrors partners.json and
-- billing_recipients.json. No production rows are inserted here.

create table if not exists public.partners (
    code            text primary key,
    group_label     text not null check (group_label in ('A', 'B')),
    created_at      timestamptz not null default now()
);
comment on table public.partners is
    'Mirrors partners.json (group_b list). Group A membership is otherwise '
    'derived at runtime from SKU prefixes (PP, BA, MK, 001) in the app; this '
    'table only needs to hold the exceptions/overrides that partners.json holds today.';

create table if not exists public.billing_recipients (
    key             text primary key,
    name            text not null,
    name_addition   text not null default '',
    street          text not null default '',
    postal_code     text not null default '',
    city            text not null default '',
    country         text not null default '',
    updated_at      timestamptz not null default now()
);
comment on table public.billing_recipients is 'Mirrors billing_recipients.json.';

alter table public.partners enable row level security;
alter table public.billing_recipients enable row level security;
