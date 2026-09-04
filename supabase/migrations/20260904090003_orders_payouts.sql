-- Part 3: core ledger — orders (Master_Orders.csv) and payout transactions
-- (Master_Payouts.csv) plus the payout status/lock table that today lives in
-- Settlement_State.sqlite3 (table "payouts") / Settlement_Locks.json.
--
-- Consolidation semantics for orders (matching by Transaktionsnummer, or by
-- Bestellnummer+Artikelnummer when no Transaktionsnummer exists on either
-- side, with conflicting values rejected) are procedural application logic
-- today (core.import_reports) and are intentionally NOT re-implemented as
-- SQL constraints here — a future migration/import script must keep
-- enforcing them the same way core.py does. The indexes below only support
-- that logic efficiently; they do not replace it.

create table if not exists public.payouts (
    id              text primary key,               -- "Auszahlung Nr."
    status          text not null,
    fingerprint     text,
    invoice_id      text,
    attempt         text,
    snapshot        jsonb,                           -- last built Lexware payload
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);
comment on table public.payouts is
    'Mirrors Settlement_State.sqlite3 table "payouts" / Settlement_Locks.json. '
    'One row per eBay payout; carries the Lexware draft-invoice reservation/lock state.';

create table if not exists public.orders (
    id                  bigint generated always as identity primary key,
    bestellnummer       text not null default '',
    transaktionsnummer  text not null default '',
    artikelnummer       text not null default '',
    sku                 text not null default '',
    angebotstitel       text not null default '',
    raw_row             jsonb not null default '{}'::jsonb,  -- full canonicalized source row for fidelity
    imported_at         timestamptz not null default now(),
    constraint orders_identity_present check (
        bestellnummer <> '' and (transaktionsnummer <> '' or artikelnummer <> '')
    )
);
comment on table public.orders is
    'Mirrors Master_Orders.csv. Row identity/consolidation matches core.match_order / '
    'core.import_reports consolidation logic, enforced by the application, not by DB constraints.';

-- Supports match_order()'s lookup by Transaktionsnummer alone.
create unique index if not exists orders_transaktionsnummer_key
    on public.orders (transaktionsnummer)
    where transaktionsnummer <> '';

-- Supports match_order()'s lookup by Bestellnummer + Artikelnummer.
create index if not exists orders_bestellnummer_artikelnummer_idx
    on public.orders (bestellnummer, artikelnummer);

create table if not exists public.payout_transactions (
    id                              bigint generated always as identity primary key,
    auszahlung_nr                   text references public.payouts (id) on update cascade,
    bestellnummer                   text not null default '',
    transaktionsnummer              text not null default '',
    artikelnummer                   text not null default '',
    typ                             text not null default '',
    datum                           text not null default '',   -- kept as text: source format not normalized upstream
    betrag_abzueglich_kosten        numeric(14, 2),
    zwischensumme_artikel           numeric(14, 2),
    verpackung_und_versand          numeric(14, 2),
    transaktionsbetrag_inkl_kosten  numeric(14, 2),
    referenznummer                  text not null default '',
    auszahlungsstatus               text not null default '',
    -- Parent/child structure (payout_structure.py): a child reference row
    -- belongs to a multi-item order and carries no amount of its own; the
    -- parent row carries the payout's financial total for the group.
    is_child_reference              boolean not null default false,
    parent_transaction_id           bigint references public.payout_transactions (id),
    raw_row                         jsonb not null default '{}'::jsonb,
    imported_at                     timestamptz not null default now(),
    constraint payout_transactions_child_has_parent check (
        not is_child_reference or parent_transaction_id is not null
    )
);
comment on table public.payout_transactions is
    'Mirrors Master_Payouts.csv. parent_transaction_id/is_child_reference make the '
    'parent/child grouping validated by payout_structure.validate() explicitly queryable; '
    'they are populated by the import process the same way validate() derives it today, '
    'not enforced as a blind DB constraint (the amount-sum check stays application logic).';

-- payout_structure.validate() groups by (Auszahlung Nr., Bestellnummer, Typ).
create index if not exists payout_transactions_group_idx
    on public.payout_transactions (auszahlung_nr, bestellnummer, typ);

create index if not exists payout_transactions_bestellnummer_idx
    on public.payout_transactions (bestellnummer);

create index if not exists payout_transactions_transaktionsnummer_idx
    on public.payout_transactions (transaktionsnummer)
    where transaktionsnummer <> '';

create index if not exists payout_transactions_parent_idx
    on public.payout_transactions (parent_transaction_id);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists payouts_set_updated_at on public.payouts;
create trigger payouts_set_updated_at
    before update on public.payouts
    for each row
    execute function public.set_updated_at();

alter table public.payouts enable row level security;
alter table public.orders enable row level security;
alter table public.payout_transactions enable row level security;
