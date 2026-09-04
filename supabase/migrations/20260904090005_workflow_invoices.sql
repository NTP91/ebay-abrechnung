-- Part 5: position workflow, discarded invoices (incl. RE0089), and partner
-- invoices. Mirrors Settlement_State.sqlite3 tables "position_workflow",
-- "discarded_invoices", "partner_invoices", "partner_invoice_positions" and
-- the JSON guards Settlement_Workflow.json / Settlement_Corrections.json /
-- Settlement_Partner_Invoices.json.

create table if not exists public.position_workflow (
    position_key    text primary key,   -- sha256(Auszahlung Nr., Transaktionsnummer, Bestellnummer, Artikelnummer, Art)
    payout_id       text references public.payouts (id),  -- denormalized convenience column, not in the SQLite original
    reviewed_at     timestamptz,
    paid_at         timestamptz,
    received_at     timestamptz,
    closed_at       timestamptz,
    source          text   -- snapshot hash/string used to detect "Quelldaten seit Bestätigung verändert"
);
comment on table public.position_workflow is
    'Mirrors Settlement_State.sqlite3 table "position_workflow" (per-position business completion state). '
    'payout_id is an added denormalized index column for query/RLS convenience; position_key remains the identity.';

create index if not exists position_workflow_payout_idx on public.position_workflow (payout_id);

create table if not exists public.discarded_invoices (
    invoice_id      text primary key,
    label           text not null,
    discarded_at    timestamptz not null default now(),
    snapshot        jsonb not null
);
comment on table public.discarded_invoices is
    'Mirrors Settlement_State.sqlite3 table "discarded_invoices". Append-only record of released '
    'Lexware draft reservations, including the archived RE0089 test-document row '
    '(invoice_id cea421da-8ae0-46f7-8576-ba68805229a2), which must migrate 1:1 with its original '
    'label, discarded_at and snapshot — no reinterpretation of that row is permitted by this schema.';

create table if not exists public.partner_invoices (
    id              text primary key,   -- app-generated id (uuid-style string, matches SQLite original)
    file_hash       text not null unique,
    partner         text not null,
    number_key      text unique,
    invoice_number  text,
    invoice_date    text,
    file_ref        text,               -- storage object path once migrated to Supabase Storage (see plan notes)
    uploaded_at     timestamptz not null default now(),
    approved_at     timestamptz,
    approved_by     text,
    approval_mode   text check (approval_mode in ('automatic_match', 'manual_override')),
    override_reason text,
    record          jsonb not null      -- full record (extracted, expected, report) for fidelity, as in SQLite "record" TEXT
);
comment on table public.partner_invoices is
    'Mirrors Settlement_State.sqlite3 table "partner_invoices". file_ref will point at the '
    'Supabase Storage object once Partner_Invoices/ is migrated (see migration plan notes below).';

create index if not exists partner_invoices_partner_idx on public.partner_invoices (partner);

create table if not exists public.partner_invoice_positions (
    position_key    text primary key
                    references public.position_workflow (position_key)
                    deferrable initially deferred,
    invoice_id      text not null references public.partner_invoices (id)
);
comment on table public.partner_invoice_positions is
    'Mirrors Settlement_State.sqlite3 table "partner_invoice_positions" (locks a settlement '
    'position to the partner invoice that claims it, preventing double-billing). The FK to '
    'position_workflow is DEFERRABLE INITIALLY DEFERRED because approve() may write both rows '
    'within one transaction in an order not fully confirmed from this read-only inventory — '
    'revisit and tighten once the write path (partner_invoices.approve/position_workflow.confirm) '
    'is ported.';

create index if not exists partner_invoice_positions_invoice_idx
    on public.partner_invoice_positions (invoice_id);

alter table public.position_workflow enable row level security;
alter table public.discarded_invoices enable row level security;
alter table public.partner_invoices enable row level security;
alter table public.partner_invoice_positions enable row level security;
