-- Review before running in Supabase SQL Editor. Existing tables require manual
-- column mapping/backfill and duplicate review; this does NOT migrate them.
create table if not exists public.commercial_leads (
  id bigint generated always as identity primary key,
  address text not null,
  address_key text not null unique,
  status text not null default 'New',
  source text not null,
  source_record_id text not null,
  lead_type text not null check (lead_type in ('violation', 'backflow')),
  deadline date,
  created_at timestamptz not null default now()
);
alter table public.commercial_leads enable row level security;
-- No anonymous/authenticated policies. Server-only service role bypasses RLS.
