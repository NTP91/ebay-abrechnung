-- Remove only obsolete unmatched aliases when their canonical Finances
-- identity already exists. No evidence is lost: the canonical row remains.
delete from public.audit_unmatched_signals old
where old.source = 'hold'
  and exists (
    select 1 from public.audit_unmatched_signals canonical
    where canonical.source = 'hold'
      and canonical.external_id =
        coalesce(nullif(old.payload->>'transaction_id',''), 'unknown') || ':' ||
        coalesce(nullif(old.payload->>'line_item_id',''), nullif(old.payload->>'transaction_id',''), 'unknown')
      and canonical.external_id <> old.external_id
  );
