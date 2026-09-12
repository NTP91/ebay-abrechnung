-- Hold status text is evidence of a hold, not a buyer complaint category.
-- Correct only cases with no complaint-bearing source.
update public.audit_cases
set other_complaint = false
where has_hold
  and not has_return
  and not has_message
  and not has_dispute
  and not has_negative_feedback;
