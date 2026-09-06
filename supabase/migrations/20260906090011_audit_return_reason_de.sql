-- Translate the currently observed eBay reason codes and correct their
-- deterministic category flags. Buyer-choice reasons remain "other".
update public.audit_cases set
  return_reason_de = case return_reason_de
    when 'WRONG_SIZE' then 'Falsche Größe bestellt'
    when 'DEFECTIVE_ITEM' then 'Artikel defekt'
    when 'ORDERED_DIFFERENT_ITEM' then 'Anderen Artikel bestellt'
    when 'WITHDRAW_FROM_PURCHASE_CONTRACT' then 'Widerruf des Kaufvertrags'
    when 'NO_LONGER_NEED_ITEM' then 'Artikel wird nicht mehr benötigt'
    when 'ORDERED_ACCIDENTALLY' then 'Versehentlich bestellt'
    else return_reason_de end,
  defective = defective or return_reason_de = 'DEFECTIVE_ITEM',
  not_as_described = not_as_described or return_reason_de = 'Artikel entspricht nicht der Beschreibung',
  other_complaint = case
    when return_reason_de in ('DEFECTIVE_ITEM','Artikel entspricht nicht der Beschreibung') then false
    else other_complaint end
where has_return;
