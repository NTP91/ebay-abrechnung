-- Part 4: audit trail + import history — mirrors Settlement_State.sqlite3
-- tables "audit", "imports", "import_warnings". Append-only in practice
-- (grants in the RLS migration deny UPDATE/DELETE to non-service roles).

create table if not exists public.audit_log (
    id          bigint generated always as identity primary key,
    payout_id   text references public.payouts (id),
    at          timestamptz not null default now(),
    event       text not null
);
comment on table public.audit_log is 'Mirrors Settlement_State.sqlite3 table "audit". Append-only.';

create index if not exists audit_log_payout_idx on public.audit_log (payout_id);
create index if not exists audit_log_at_idx on public.audit_log (at);

create table if not exists public.imports (
    id          bigint generated always as identity primary key,
    kind        text not null check (kind in ('orders', 'payout')),
    filename    text not null default '',
    at          timestamptz not null default now(),
    start_at    text not null default '',   -- kept as text: source period bounds not normalized upstream
    end_at      text not null default '',
    detected    integer,
    added       integer,
    present     integer,
    issues      integer,
    error       text
);
comment on table public.imports is 'Mirrors Settlement_State.sqlite3 table "imports" (import run history).';

create index if not exists imports_kind_at_idx on public.imports (kind, at);

create table if not exists public.import_warnings (
    id          bigint generated always as identity primary key,
    payout_id   text,   -- intentionally no FK: a rejected import can name a
                         -- payout id that has no "payouts" row yet
    at          timestamptz not null default now(),
    reason      text not null,
    snapshot    jsonb
);
comment on table public.import_warnings is
    'Mirrors Settlement_State.sqlite3 table "import_warnings" (rejected/conflicting import rows).';

create index if not exists import_warnings_payout_idx on public.import_warnings (payout_id);

alter table public.audit_log enable row level security;
alter table public.imports enable row level security;
alter table public.import_warnings enable row level security;
