-- Part 6: eBay API hold evidence (append-only), daily sync state/run history,
-- and manual bank reconciliation. Mirrors Settlement_State.sqlite3 tables
-- "api_hold_evidence", "ebay_sync_state", "manual_payout_reconciliation" and
-- the JSON guards Settlement_API_Holds.json / Settlement_Ebay_Sync.json /
-- Settlement_Payout_Reconciliation.json.
--
-- Note: the SQLite originals for api_hold_evidence/ebay_sync_state/
-- manual_payout_reconciliation each stored ONE row holding a single JSON
-- document (the whole observation/run history as one blob). This migration
-- normalizes api_hold_evidence to one row per observation (true append-only,
-- matching api_holds.ingest()'s "union of new observations" semantics) and
-- keeps ebay_sync_state as a singleton row (matching its watermark/checkpoint
-- role) while splitting its "runs" array into its own append-only table.

create table if not exists public.api_hold_evidence (
    id                  bigint generated always as identity primary key,
    observed_at         timestamptz not null default now(),
    order_id            text,
    transaction_id      text,
    transaction_type    text,
    transaction_status  text,
    transaction_date    text,
    booking_entry       text,
    amount              numeric(14, 2),
    payout_id           text,
    "references"        jsonb,
    transaction_memo    text,
    raw_observation     jsonb not null,
    inserted_at         timestamptz not null default now(),
    constraint api_hold_evidence_dedup unique (transaction_id, transaction_type, observed_at)
);
comment on table public.api_hold_evidence is
    'Append-only mirror of Settlement_API_Holds.json "observations" (one row per observation, '
    'instead of the single-document SQLite mirror). Never update/delete rows here — '
    'api_holds.ingest() only ever adds new observations, it never removes evidence.';

create index if not exists api_hold_evidence_order_idx on public.api_hold_evidence (order_id);
create index if not exists api_hold_evidence_payout_idx on public.api_hold_evidence (payout_id);

create table if not exists public.ebay_sync_state (
    id              smallint primary key default 1,
    watermark       timestamptz,
    payouts         jsonb not null default '{}'::jsonb,
    transactions    jsonb not null default '{}'::jsonb,
    updated_at      timestamptz not null default now(),
    constraint ebay_sync_state_singleton check (id = 1)
);
comment on table public.ebay_sync_state is
    'Singleton mirror of Settlement_Ebay_Sync.json checkpoint state (watermark + raw API mirrors). '
    'Exactly one row, matching the current single-document SQLite table.';

create table if not exists public.ebay_sync_runs (
    id                  uuid primary key default extensions.gen_random_uuid(),
    source              text not null default 'eBay API',
    trigger             text not null check (trigger in ('manual', 'automatic')),
    at                  timestamptz not null default now(),
    start_at            timestamptz,
    end_at              timestamptz,
    status              text not null check (
                            status in ('running', 'success', 'partial', 'failed', 'interrupted', 'busy')
                        ),
    new_payouts         integer,
    new_transactions    integer,
    known               integer,
    ledger_only         integer,
    error               text,
    finished_at         timestamptz
);
comment on table public.ebay_sync_runs is
    'Append-only mirror of Settlement_Ebay_Sync.json "runs" (one row per daily/manual import run).';

create index if not exists ebay_sync_runs_at_idx on public.ebay_sync_runs (at);

create table if not exists public.manual_payout_reconciliation (
    payout_id       text primary key references public.payouts (id),
    bank_amount     numeric(14, 2),
    items           jsonb not null default '{}'::jsonb,
    actor           text not null,
    note            text,
    at              timestamptz not null default now()
);
comment on table public.manual_payout_reconciliation is
    'Mirrors the current-state part of Settlement_State.sqlite3 table "manual_payout_reconciliation" '
    '(one row per payout, the SQLite original stored the whole "payouts" dict as one document).';

create table if not exists public.manual_payout_reconciliation_audit (
    id          bigint generated always as identity primary key,
    payout_id   text references public.payouts (id),
    "before"    jsonb,
    "after"     jsonb,
    changed_at  timestamptz not null default now()
);
comment on table public.manual_payout_reconciliation_audit is
    'Append-only mirror of the "audit" array inside Settlement_Payout_Reconciliation.json.';

create index if not exists manual_payout_reconciliation_audit_payout_idx
    on public.manual_payout_reconciliation_audit (payout_id);

alter table public.api_hold_evidence enable row level security;
alter table public.ebay_sync_state enable row level security;
alter table public.ebay_sync_runs enable row level security;
alter table public.manual_payout_reconciliation enable row level security;
alter table public.manual_payout_reconciliation_audit enable row level security;
