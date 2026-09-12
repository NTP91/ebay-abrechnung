-- Part 7: eBay Trust/Risk read-only snapshots. Mirrors Ebay_Readonly/
-- (latest.json + immutable timestamped audit copies).

create table if not exists public.trust_risk_snapshots (
    id              bigint generated always as identity primary key,
    snapshot_uuid   uuid not null default extensions.gen_random_uuid(),
    account         text not null,
    fetched_at      timestamptz not null,
    resources       jsonb not null,
    inserted_at     timestamptz not null default now(),
    constraint trust_risk_snapshots_dedup unique (account, fetched_at)
);
comment on table public.trust_risk_snapshots is
    'Append-only mirror of Ebay_Readonly/<UTC-timestamp>-<uuid4hex>.json (one immutable row per '
    'trust_risk.save_snapshot() run). Never update/delete rows here — each snapshot is permanent '
    'audit evidence, same guarantee the local timestamped files provide today.';

create index if not exists trust_risk_snapshots_fetched_at_idx
    on public.trust_risk_snapshots (fetched_at desc);

-- Equivalent of Ebay_Readonly/latest.json: the most recent snapshot per account.
-- security_invoker ensures this view respects trust_risk_snapshots' RLS for
-- the querying role instead of running with the view owner's privileges.
create or replace view public.trust_risk_latest
    with (security_invoker = true) as
select distinct on (account) *
from public.trust_risk_snapshots
order by account, fetched_at desc;

alter table public.trust_risk_snapshots enable row level security;
