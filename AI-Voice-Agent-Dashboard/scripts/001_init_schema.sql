-- ============================================================================
-- AI Voice Agent Dashboard — initial schema
--
-- Every column here is referenced by code in app/api/** or lib/**. The
-- TypeScript interfaces in types/index.ts are the source of truth for shapes.
--
-- Access model: all reads/writes go through Next.js route handlers that use the
-- Supabase SERVICE ROLE key (lib/supabase-server.ts), which bypasses RLS. RLS is
-- therefore enabled with NO permissive policies so that the public anon key
-- cannot read or write these tables directly.
-- ============================================================================

-- ─── upload_batches ─────────────────────────────────────────────────────────
-- One row per CSV upload (app/api/leads/upload).
create table if not exists public.upload_batches (
  id              uuid primary key default gen_random_uuid(),
  filename        text,
  total_rows      integer not null default 0,
  valid_rows      integer not null default 0,
  rejected_rows   integer not null default 0,
  duplicate_rows  integer not null default 0,
  created_at      timestamptz not null default now()
);

-- ─── campaign_runs ──────────────────────────────────────────────────────────
-- One row per launch. Reserved before dialling so a crash still leaves a
-- record we can find and stop (see app/api/campaigns/route.ts).
create table if not exists public.campaign_runs (
  id                  uuid primary key default gen_random_uuid(),
  campaign_name       text not null,
  dograh_campaign_id  bigint,
  workflow_id         bigint,
  source_id           text,
  requested_count     integer not null default 0,
  actual_count        integer not null default 0,
  concurrency         integer not null default 1,
  status              text not null default 'pending',
  target_segment      jsonb,
  retry_config        jsonb,
  schedule_config     jsonb,
  error_message       text,
  started_at          timestamptz,
  completed_at        timestamptz,
  paused_at           timestamptz,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

-- The duplicate-launch guard queries recent runs by name.
create index if not exists campaign_runs_name_created_idx
  on public.campaign_runs (campaign_name, created_at desc);

-- The reconcile sweep pages over runs that reached the provider.
create index if not exists campaign_runs_dograh_id_idx
  on public.campaign_runs (dograh_campaign_id)
  where dograh_campaign_id is not null;

create index if not exists campaign_runs_status_idx
  on public.campaign_runs (status);

-- ─── leads ──────────────────────────────────────────────────────────────────
-- `phone` is unique: it is the natural key used for CSV de-duplication and it
-- is what makes the compare-and-set lead claim in the launch route safe.
create table if not exists public.leads (
  id               uuid primary key default gen_random_uuid(),
  name             text,
  phone            text not null unique,
  email            text,
  city             text,
  source           text,
  status           text not null default 'new',
  score            integer,
  qualification    text,
  qual_data        jsonb,
  call_outcome     text,
  property_type    text,
  budget           text,
  notes            text,
  batch_id         uuid references public.upload_batches (id) on delete set null,
  campaign_run_id  uuid references public.campaign_runs (id) on delete set null,
  retry_count      integer not null default 0,
  recording_url    text,
  transcript_url   text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  last_attempt_at  timestamptz,
  follow_up_date   timestamptz
);

-- The launch route claims leads with `.eq('status','new').order('created_at')`.
create index if not exists leads_status_created_idx
  on public.leads (status, created_at);

create index if not exists leads_campaign_run_idx
  on public.leads (campaign_run_id);

create index if not exists leads_qualification_idx
  on public.leads (qualification);

-- The stuck-lead sweep orders queued leads by last_attempt_at.
create index if not exists leads_last_attempt_idx
  on public.leads (last_attempt_at);

create index if not exists leads_follow_up_idx
  on public.leads (follow_up_date)
  where follow_up_date is not null;

-- ─── rejected_leads ─────────────────────────────────────────────────────────
-- CSV rows that failed validation, kept so the user can see why.
create table if not exists public.rejected_leads (
  id          uuid primary key default gen_random_uuid(),
  batch_id    uuid references public.upload_batches (id) on delete cascade,
  raw_data    jsonb,
  reason      text,
  created_at  timestamptz not null default now()
);

create index if not exists rejected_leads_batch_idx
  on public.rejected_leads (batch_id);

-- ─── call_logs ──────────────────────────────────────────────────────────────
-- Append-only audit trail of every call attempt.
--
-- `dograh_run_id` is UNIQUE and that is load-bearing: it is the idempotency
-- barrier shared by the webhook and the reconcile sweep. A concurrent duplicate
-- delivery fails this constraint with SQLSTATE 23505, which both callers treat
-- as "already processed" — so a lead is never scored or retried twice.
create table if not exists public.call_logs (
  id                uuid primary key default gen_random_uuid(),
  lead_id           uuid references public.leads (id) on delete cascade,
  campaign_run_id   uuid references public.campaign_runs (id) on delete set null,
  dograh_run_id     bigint unique,
  attempt_no        integer not null default 1,
  outcome           text,
  duration          integer,
  recording_url     text,
  transcript_url    text,
  gathered_context  jsonb,
  cost_info         jsonb,
  called_at         timestamptz not null default now(),
  created_at        timestamptz not null default now()
);

create index if not exists call_logs_lead_idx
  on public.call_logs (lead_id);

create index if not exists call_logs_campaign_run_idx
  on public.call_logs (campaign_run_id);

create index if not exists call_logs_called_at_idx
  on public.call_logs (called_at desc);

create index if not exists call_logs_outcome_idx
  on public.call_logs (outcome);

-- ─── settings ───────────────────────────────────────────────────────────────
-- Simple key/value store upserted on conflict by `key` (app/api/settings).
create table if not exists public.settings (
  key         text primary key,
  value       jsonb,
  updated_at  timestamptz not null default now()
);

-- ─── updated_at maintenance ─────────────────────────────────────────────────
-- The stuck-lead sweep relies on leads.updated_at moving whenever a row
-- changes, so keep it in the database rather than trusting every call site.
create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists leads_touch_updated_at on public.leads;
create trigger leads_touch_updated_at
  before update on public.leads
  for each row execute function public.touch_updated_at();

drop trigger if exists campaign_runs_touch_updated_at on public.campaign_runs;
create trigger campaign_runs_touch_updated_at
  before update on public.campaign_runs
  for each row execute function public.touch_updated_at();

drop trigger if exists settings_touch_updated_at on public.settings;
create trigger settings_touch_updated_at
  before update on public.settings
  for each row execute function public.touch_updated_at();

-- ─── Row Level Security ─────────────────────────────────────────────────────
-- Enabled with no policies: the anon/publishable key gets nothing. Server
-- routes use the service role key and are unaffected.
alter table public.upload_batches enable row level security;
alter table public.campaign_runs  enable row level security;
alter table public.leads          enable row level security;
alter table public.rejected_leads enable row level security;
alter table public.call_logs      enable row level security;
alter table public.settings       enable row level security;
