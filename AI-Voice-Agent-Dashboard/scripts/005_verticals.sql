-- ============================================================================
-- 005 — Four business lines: loan, real estate, solar, investing
--
-- Run this in the Supabase SQL editor of the LEADS project (ciydgkugezflbhgqokwn),
-- NOT the billing project. Safe on live data: every statement is additive or a
-- constraint swap, and every existing row becomes `loan`, which is what they are.
--
-- MUST run BEFORE the n8n workflow is edited to send `vertical`. n8n sending a
-- column the table does not have makes every upload fail with a PostgREST 400.
-- ============================================================================

-- ─── 1. The business line on every lead ──────────────────────────────────────
alter table public.leads
  add column if not exists vertical text not null default 'loan';

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'leads_vertical_check'
  ) then
    alter table public.leads
      add constraint leads_vertical_check
      check (vertical in ('loan','realestate','solar','investing'));
  end if;
end $$;

-- ─── 2. The important one ────────────────────────────────────────────────────
-- `phone` was globally unique, and n8n upserts with resolution=ignore-duplicates.
-- So a number already present as a loan lead was SILENTLY DROPPED when it
-- appeared in a real-estate or solar file - no error, no rejected_leads row, the
-- lead simply vanished. One person can legitimately be a lead in two business
-- lines; the natural key is therefore (phone, vertical), not phone.
--
-- The campaign lead-claim in app/api/campaigns/route.ts claims by lead `id`, so
-- dropping the global phone key does not affect it.
--
-- NOTE: this deliberately does NOT say `drop constraint if exists leads_phone_key`.
-- That is the name Postgres gives a constraint declared as `phone text not null
-- unique`, but if the live constraint is named anything else the DROP would do
-- NOTHING, succeed silently, and leave the global phone key in place - so
-- cross-vertical leads would still be discarded with no error and no sign that
-- the migration had failed. Dropping by lookup instead of by name cannot fail
-- that way. It also removes a bare unique INDEX on phone, which a constraint
-- lookup alone would miss.
do $$
declare
  r record;
  dropped int := 0;
begin
  -- Unique CONSTRAINTS whose column list is exactly (phone)
  for r in
    select con.conname
      from pg_constraint con
      join pg_class rel on rel.oid = con.conrelid
      join pg_namespace ns on ns.oid = rel.relnamespace
     where ns.nspname = 'public'
       and rel.relname = 'leads'
       and con.contype = 'u'
       and (
         select array_agg(att.attname::text)
           from unnest(con.conkey) k
           join pg_attribute att
             on att.attrelid = con.conrelid and att.attnum = k
       ) = array['phone']
  loop
    execute format('alter table public.leads drop constraint %I', r.conname);
    raise notice 'dropped unique constraint %', r.conname;
    dropped := dropped + 1;
  end loop;

  -- Unique INDEXES on (phone) not backed by a constraint
  for r in
    select i.indexrelid::regclass::text as idxname
      from pg_index i
      join pg_class rel on rel.oid = i.indrelid
      join pg_namespace ns on ns.oid = rel.relnamespace
     where ns.nspname = 'public'
       and rel.relname = 'leads'
       and i.indisunique
       and not exists (select 1 from pg_constraint c where c.conindid = i.indexrelid)
       and (
         select array_agg(att.attname::text)
           from unnest(i.indkey) k
           join pg_attribute att
             on att.attrelid = i.indrelid and att.attnum = k
       ) = array['phone']
  loop
    execute format('drop index public.%I', split_part(r.idxname, '.', 2));
    raise notice 'dropped unique index %', r.idxname;
    dropped := dropped + 1;
  end loop;

  raise notice 'phone-only unique keys removed: %', dropped;
end $$;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'leads_phone_vertical_key'
  ) then
    alter table public.leads
      add constraint leads_phone_vertical_key unique (phone, vertical);
  end if;
end $$;

-- ─── 3. Index the pair every campaign query filters on ───────────────────────
create index if not exists leads_vertical_status_created_idx
  on public.leads (vertical, status, created_at);

-- ─── 4. Campaigns and uploads carry the business line too ────────────────────
-- Without these, campaign history and upload history cannot say which business
-- line they belonged to, and the dashboard's Campaigns page cannot be filtered.
alter table public.campaign_runs
  add column if not exists vertical text not null default 'loan';

alter table public.upload_batches
  add column if not exists vertical text not null default 'loan';

create index if not exists campaign_runs_vertical_created_idx
  on public.campaign_runs (vertical, created_at desc);

-- ─── Verify ──────────────────────────────────────────────────────────────────
-- Run these after the migration. Do not skip: the whole point of the block above
-- is that a silent no-op is possible, and only this check would reveal it.
--
--   -- 1. Every existing lead should be 'loan' (18 rows as of 2026-08-06).
--   select vertical, count(*) from public.leads group by vertical;
--
--   -- 2. MUST list leads_phone_vertical_key and MUST NOT list any unique key
--   --    whose columns are exactly (phone). If a phone-only unique key is still
--   --    here, STOP - cross-vertical leads will still be silently discarded.
--   select con.conname,
--          (select array_agg(att.attname::text)
--             from unnest(con.conkey) k
--             join pg_attribute att
--               on att.attrelid = con.conrelid and att.attnum = k) as columns
--     from pg_constraint con
--    where con.conrelid = 'public.leads'::regclass
--      and con.contype = 'u';
