-- Complete fail-closed operational object store used by the payment runtime.
create table if not exists public.payment_runtime_objects (
    key text primary key,
    version bigint not null default 1 check (version > 0),
    sha256 text not null,
    content bytea not null,
    updated_at timestamptz not null default now()
);
alter table public.payment_runtime_objects enable row level security;
revoke all on public.payment_runtime_objects from anon, authenticated;
