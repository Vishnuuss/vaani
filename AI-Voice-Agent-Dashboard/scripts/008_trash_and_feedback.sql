-- ============================================================================
-- 008 — Recycle Bin (smart delete) and client feedback
--
-- Run this in the Supabase SQL editor of the LEADS project (ciydgkugezflbhgqokwn),
-- NOT the billing project. Safe on live data: every statement is additive. No
-- existing table is altered and no existing row is touched.
--
-- ─── Why archive TABLES and not a `deleted_at` flag ─────────────────────────
-- Roughly fifty places in app/api/** and lib/** read `leads`, `call_logs` and
-- `campaign_runs` — the reports, the stat tiles, the segment counts, the billing
-- meter, the reconcile sweep, and the campaign launch route that decides who
-- gets DIALLED. A soft-delete flag would make every one of those fifty queries
-- wrong until it was individually edited, and the failure mode of the one that
-- got missed is not a cosmetic count being off: it is the launch route claiming
-- a deleted lead and placing a real, billed call to someone the client deleted.
--
-- Moving the row into a separate table cannot fail that way. Once a lead is out
-- of `leads` it is out of all fifty queries at once, with no code change and no
-- possibility of a leak. The cost is that restore has to put the row back, which
-- is a single explicit operation in lib/trash.ts rather than fifty implicit ones.
-- ============================================================================

-- ─── trash_batches ──────────────────────────────────────────────────────────
-- One row per delete ACTION, not per deleted record. The Recycle Bin lists
-- these, so the client sees "4,318 unqualified leads deleted 2 hours ago" as a
-- single restorable unit rather than 4,318 separate lines.
create table if not exists public.trash_batches (
  id            uuid primary key default gen_random_uuid(),
  -- Which table the rows came from. The Recycle Bin groups by this.
  entity        text not null check (entity in ('lead', 'call_log', 'campaign')),
  -- The filter in words, built server-side from the same parameters the delete
  -- ran with: "Unqualified leads, Solar, before 01 Jul 2026". Stored rather than
  -- recomputed so the bin still describes the deletion correctly even after the
  -- filter vocabulary in the UI changes.
  filter_label  text not null,
  -- The raw filter, kept for support questions ("what exactly did we delete?").
  filter_json   jsonb,
  row_count     integer not null default 0,
  -- How many rows were swept along because they belonged to a deleted parent —
  -- call logs archived because their lead went. Shown separately in the bin so
  -- "12 leads" never silently means "12 leads and 400 calls".
  cascaded_count integer not null default 0,
  deleted_by    text,
  created_at    timestamptz not null default now(),
  -- When the auto-purge may wipe this batch. Set by the application to
  -- created_at + 7 days; stored as a column rather than computed so the
  -- retention window can be changed later without rewriting history.
  purge_after   timestamptz not null default (now() + interval '7 days'),
  -- Set when the batch has been restored, so it stops appearing as restorable
  -- without losing the audit trail of what was deleted and when.
  restored_at   timestamptz
);

-- The bin lists newest-first, and the purge sweep scans by purge_after.
create index if not exists trash_batches_created_idx
  on public.trash_batches (created_at desc);

create index if not exists trash_batches_purge_idx
  on public.trash_batches (purge_after)
  where restored_at is null;

-- ─── leads_trash ────────────────────────────────────────────────────────────
-- `id` is the ORIGINAL lead id, preserved deliberately: restoring must put the
-- lead back under the same id or every call_log pointing at it would be orphaned.
--
-- `row_data` holds the complete original row as jsonb, so restore is a faithful
-- reinsert and this table never needs migrating when `leads` gains a column.
-- The flat columns beside it exist only so the Recycle Bin can show a readable
-- preview without parsing jsonb for every row.
create table if not exists public.leads_trash (
  id             uuid primary key,
  batch_id       uuid not null references public.trash_batches (id) on delete cascade,
  row_data       jsonb not null,
  phone          text,
  name           text,
  vertical       text,
  status         text,
  qualification  text,
  deleted_at     timestamptz not null default now()
);

create index if not exists leads_trash_batch_idx
  on public.leads_trash (batch_id);

-- ─── call_logs_trash ────────────────────────────────────────────────────────
-- Rows arrive here two ways: deleted directly from the Call Logs page, or swept
-- along because the lead they belong to was deleted. `cascaded` records which,
-- so the bin can say "400 call logs (deleted with their leads)".
--
-- `dograh_run_id` is copied out flat because it is UNIQUE in `call_logs` and is
-- the idempotency barrier the webhook and the reconcile sweep both depend on.
-- A restore that violated it would resurrect a duplicate of a call the system
-- has already scored, so restore checks this column before reinserting.
create table if not exists public.call_logs_trash (
  id             uuid primary key,
  batch_id       uuid not null references public.trash_batches (id) on delete cascade,
  row_data       jsonb not null,
  lead_id        uuid,
  dograh_run_id  bigint,
  outcome        text,
  called_at      timestamptz,
  cascaded       boolean not null default false,
  deleted_at     timestamptz not null default now()
);

create index if not exists call_logs_trash_batch_idx
  on public.call_logs_trash (batch_id);

-- ─── campaign_runs_trash ────────────────────────────────────────────────────
-- `leads.campaign_run_id` and `call_logs.campaign_run_id` are both ON DELETE SET
-- NULL, so deleting a campaign quietly cuts every lead and call loose from it.
-- Restoring the campaign row alone would bring back a campaign that appears to
-- have dialled nobody. `relink_lead_ids` / `relink_call_ids` record exactly which
-- rows pointed here at delete time so restore can reattach them.
create table if not exists public.campaign_runs_trash (
  id              uuid primary key,
  batch_id        uuid not null references public.trash_batches (id) on delete cascade,
  row_data        jsonb not null,
  campaign_name   text,
  status          text,
  vertical        text,
  relink_lead_ids jsonb not null default '[]'::jsonb,
  relink_call_ids jsonb not null default '[]'::jsonb,
  deleted_at      timestamptz not null default now()
);

create index if not exists campaign_runs_trash_batch_idx
  on public.campaign_runs_trash (batch_id);

-- ─── feedback ───────────────────────────────────────────────────────────────
-- Client's answers about the dashboard and the voice agents. Read from the
-- Operator console only — the client submits and sees a thank-you, never the
-- list of past submissions.
--
-- Every rating column is nullable: a half-finished form is worth more than no
-- form, so only `vertical` is required by the API.
create table if not exists public.feedback (
  id                     uuid primary key default gen_random_uuid(),
  -- 'solar', 'loan' or 'both'. Not constrained to lib/verticals because this is
  -- the client's choice of what they are rating, and 'both' is a valid answer
  -- that is not a business line.
  vertical               text not null check (vertical in ('solar', 'loan', 'both')),
  dashboard_rating       integer check (dashboard_rating between 1 and 5),
  voice_rating           integer check (voice_rating between 1 and 5),
  understanding_rating   integer check (understanding_rating between 1 and 5),
  -- 'much_better' | 'better' | 'same' | 'worse' | 'too_early'
  qualification_change   text,
  qualified_before_week  integer,
  qualified_after_week   integer,
  -- 'none' | 'under_5' | '5_to_15' | 'over_15'
  hours_saved            text,
  improvements           text,
  recommend_score        integer check (recommend_score between 0 and 10),
  submitted_by           text,
  created_at             timestamptz not null default now()
);

create index if not exists feedback_created_idx
  on public.feedback (created_at desc);

-- ─── Row Level Security ─────────────────────────────────────────────────────
-- Same model as 001: enabled with NO policies, so the anon/publishable key can
-- read nothing. All access is through route handlers on the service role key.
-- This matters more here than anywhere else in the schema — these tables hold
-- deleted customer records and the client's private opinion of the product.
alter table public.trash_batches      enable row level security;
alter table public.leads_trash        enable row level security;
alter table public.call_logs_trash    enable row level security;
alter table public.campaign_runs_trash enable row level security;
alter table public.feedback           enable row level security;

-- ─── Verify ─────────────────────────────────────────────────────────────────
-- Run these after the migration. All five must return a row.
--
--   select table_name from information_schema.tables
--    where table_schema = 'public'
--      and table_name in ('trash_batches','leads_trash','call_logs_trash',
--                         'campaign_runs_trash','feedback');
--
--   -- Must be 5 rows, all with rowsecurity = true.
--   select relname, relrowsecurity from pg_class
--    where relname in ('trash_batches','leads_trash','call_logs_trash',
--                      'campaign_runs_trash','feedback');
