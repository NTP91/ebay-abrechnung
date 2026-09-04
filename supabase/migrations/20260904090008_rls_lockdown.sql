-- Part 8: RLS lockdown for every table created in this schema.
--
-- Design: RLS is enabled on every table (done per-table in the earlier
-- migrations) and, deliberately, NO policies are created for anon or
-- authenticated here. In Postgres/Supabase, "RLS enabled + zero policies"
-- means those two roles get ZERO rows on SELECT/INSERT/UPDATE/DELETE,
-- regardless of any table-level GRANTs — this is the strict default-deny
-- posture the task asked for ("nichts unkontrolliert öffentlich zugänglich").
--
-- service_role (the role a future GitHub Actions service would use via the
-- Supabase service-role key) BYPASSES row level security by Supabase's own
-- built-in role configuration — it does not need, and does not get, any
-- policy defined here. It still needs table-level privileges, which Supabase
-- grants to service_role by default via project-level default privileges on
-- the public schema; this migration does not touch that mechanism.
--
-- The explicit REVOKE statements below are defense in depth only: even if a
-- future migration accidentally adds an anon/authenticated policy to one
-- table, the other tables stay unreadable/unwritable for those roles until a
-- policy is deliberately added AND a grant exists.

revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;

-- No CREATE POLICY statements are added in this migration on purpose — see
-- header comment. When the app or a GitHub Actions service needs scoped
-- access through anon/authenticated (as opposed to the service_role key),
-- add narrow, explicit policies in a dedicated follow-up migration then,
-- not here.
