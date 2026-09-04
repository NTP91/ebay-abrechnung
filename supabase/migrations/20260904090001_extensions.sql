-- Durchstarter / ebay-abrechnung — Supabase schema migration
-- Part 1: extensions required by later migrations (uuid generation).
-- Idempotent: safe to re-run.

create extension if not exists pgcrypto with schema extensions;
