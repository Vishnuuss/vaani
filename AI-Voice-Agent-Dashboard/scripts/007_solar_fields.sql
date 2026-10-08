-- ============================================================================
-- 007 — The solar agent's two answers get their own columns
--
-- Run this in the Supabase SQL editor of the LEADS project (ciydgkugezflbhgqokwn),
-- NOT the billing project. Safe on live data: both statements are additive, both
-- columns are nullable, and no existing row is touched or rewritten.
--
-- The solar agent (Dograh workflow 5) asks exactly two questions:
--   1. own house or rent house   -> house_ownership
--   2. planning to put solar     -> solar_planning
-- and the score follows from them: rent 0, own 50, own + planning 100.
--
-- These already travel inside leads.qual_data, so the dashboard works without
-- this migration. Real columns exist so the two answers can be filtered, sorted,
-- indexed and read straight out of a SQL query or an export - which a jsonb blob
-- makes awkward for anyone querying the table by hand.
--
-- ORDER: this is safe to run BEFORE or AFTER the dashboard deploys. The webhook
-- writes these columns when they exist and silently falls back to qual_data-only
-- when they do not (lib/lead-update.ts), so neither order can break a call
-- result. Run it whenever you like - the sooner the sooner the columns fill.
-- ============================================================================

-- ─── 1. Own house or rented ──────────────────────────────────────────────────
-- text, not boolean: the honest states are 'own', 'rent' and "never said", and a
-- boolean would force the third one to look like a 'rent'. Null means the
-- customer never answered, which is a 25, not a 0.
alter table public.leads
  add column if not exists house_ownership text;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'leads_house_ownership_check'
  ) then
    alter table public.leads
      add constraint leads_house_ownership_check
      check (house_ownership is null or house_ownership in ('own','rent'));
  end if;
end $$;

-- ─── 2. Are they planning solar ──────────────────────────────────────────────
-- Three states again: true, false, and null for "never got to that question".
alter table public.leads
  add column if not exists solar_planning boolean;

-- ─── 3. The one query the solar team actually runs ───────────────────────────
-- "give me the solar leads who own their house and are planning it", newest
-- first. Partial index: it only covers solar rows, so it stays small and costs
-- the loan leads nothing.
create index if not exists leads_solar_ready_idx
  on public.leads (vertical, house_ownership, solar_planning, created_at desc)
  where vertical = 'solar';

comment on column public.leads.house_ownership is
  'Solar agent question 1: own | rent | null (never said). Rent scores 0 - rooftop solar needs their own house.';
comment on column public.leads.solar_planning is
  'Solar agent question 2: are they planning solar. Null = never answered. Own house + true = 100.';

-- ─── Verify ──────────────────────────────────────────────────────────────────
-- Run these after the migration:
--
--   -- 1. Both columns exist and are nullable.
--   select column_name, data_type, is_nullable
--     from information_schema.columns
--    where table_schema = 'public' and table_name = 'leads'
--      and column_name in ('house_ownership','solar_planning');
--
--   -- 2. After the first solar calls land, this is the executive's call list.
--   select name, phone, score, house_ownership, solar_planning, last_attempt_at
--     from public.leads
--    where vertical = 'solar' and house_ownership = 'own' and solar_planning is true
--    order by last_attempt_at desc;
