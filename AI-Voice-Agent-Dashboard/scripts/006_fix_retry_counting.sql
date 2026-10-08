-- ═══════════════════════════════════════════════════════════════════════════
-- 006 — Fix over-counted and over-placed call attempts
-- ═══════════════════════════════════════════════════════════════════════════
--
-- WHAT WENT WRONG
--
-- 001_init_schema.sql declares `call_logs.dograh_run_id bigint unique`, but the
-- live table predates that line and `create table if not exists` never adds a
-- constraint to a table that already exists. So the UNIQUE index was never
-- actually created in production, and both idempotency barriers that depend on
-- it were inert:
--
--   * the webhook (app/api/webhook/call-result) expects the insert to fail with
--     SQLSTATE 23505 on a redelivery. It never failed, so every redelivery
--     inserted another row AND incremented leads.retry_count again.
--   * the reconcile sweep (lib/reconcile.ts) looked the run up with
--     `.maybeSingle()`, which ERRORS on more than one match. The error was
--     discarded, so a run with two rows looked unseen and got inserted a third
--     time — and a fourth, on every subsequent cron tick.
--
-- Measured on production 2026-08-08: 301 call_logs rows for 217 real calls.
-- One lead had 47 rows and retry_count = 31 for 3 actual dials.
--
-- This migration is idempotent and safe to re-run.
-- ═══════════════════════════════════════════════════════════════════════════

begin;

-- ─── Step 1: collapse duplicate rows ────────────────────────────────────────
-- Keep the EARLIEST row per dograh_run_id: that is the original webhook insert,
-- which carries the recording/transcript/gathered_context. The later copies are
-- reconcile re-inserts and are strictly poorer.
with ranked as (
  select
    id,
    row_number() over (
      partition by dograh_run_id
      order by created_at asc, id asc
    ) as rn
  from public.call_logs
  where dograh_run_id is not null
)
delete from public.call_logs cl
using ranked
where cl.id = ranked.id
  and ranked.rn > 1;

-- ─── Step 2: restore the idempotency barrier ────────────────────────────────
-- THIS is the line that was silently missing. Everything above is cleanup;
-- this is the fix. NULLs are permitted and do not conflict with each other,
-- which is what we want — a call we cannot identify is still worth logging.
create unique index if not exists call_logs_dograh_run_id_key
  on public.call_logs (dograh_run_id)
  where dograh_run_id is not null;

-- ─── Step 3: rebuild retry_count from the deduplicated truth ────────────────
-- retry_count is the number of real calls placed to this person. After Step 1
-- there is exactly one row per real call, so it is a plain count.
update public.leads l
set retry_count = coalesce(c.n, 0)
from (
  select lead_id, count(*)::int as n
  from public.call_logs
  where lead_id is not null
  group by lead_id
) c
where l.id = c.lead_id
  and l.retry_count is distinct from c.n;

-- A lead with no call_logs at all must read 0, not a stale inflated number.
update public.leads
set retry_count = 0
where retry_count <> 0
  and id not in (select lead_id from public.call_logs where lead_id is not null);

-- ─── Step 4: renumber attempt_no ────────────────────────────────────────────
-- attempt_no was written from the inflated retry_count, so the audit trail shows
-- things like "attempt 30" for a third call. Renumber chronologically per lead.
with numbered as (
  select
    id,
    row_number() over (partition by lead_id order by called_at asc, created_at asc, id asc) as n
  from public.call_logs
  where lead_id is not null
)
update public.call_logs cl
set attempt_no = numbered.n
from numbered
where cl.id = numbered.id
  and cl.attempt_no is distinct from numbered.n;

commit;

-- ═══════════════════════════════════════════════════════════════════════════
-- REVIEW (read-only — run after the migration, act on it manually)
--
-- Leads that the inflated counter pushed to unreachable/no_answer even though
-- they were really dialled FEWER times than the max-retries setting allows.
-- These people were written off early and can legitimately be called again.
-- Decide deliberately: releasing them means real, billed calls.
--
--   select id, name, phone, status, retry_count, last_attempt_at
--   from public.leads
--   where status in ('unreachable', 'no_answer')
--     and retry_count < (
--       select coalesce((value ->> 'maxRetries')::int, 2)
--       from public.settings where key = 'call_behavior'
--     )
--   order by retry_count, last_attempt_at desc;
--
-- To put them back into the automatic rotation (ONLY when you mean it):
--
--   update public.leads
--   set status = 'retry_pending'
--   where status in ('unreachable', 'no_answer')
--     and retry_count < (
--       select coalesce((value ->> 'maxRetries')::int, 2)
--       from public.settings where key = 'call_behavior'
--     );
-- ═══════════════════════════════════════════════════════════════════════════
