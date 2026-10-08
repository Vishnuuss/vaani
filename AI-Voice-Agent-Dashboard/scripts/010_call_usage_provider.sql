-- ═══════════════════════════════════════════════════════════════════════════
-- 010 — The same backend-identity fix, on the BILLING database
-- ═══════════════════════════════════════════════════════════════════════════
--
-- RUN THIS AGAINST THE BILLING PROJECT, not the client's project.
-- 009 is the matching migration for call_logs and runs against the client's.
--
-- WHY
--
-- `meterCampaign` and `meterSingleRun` both upsert with
-- `onConflict: 'dograh_run_id', ignoreDuplicates: true`, and the ledger's
-- idempotency key is `call:run:<id>`. Both identify a call by the backend's run
-- id alone.
--
-- 183 of the ids Vaani has already issued are present in call_usage from
-- voice.bswealthfinance.com. For each of those, a real Vaani call is treated as
-- a call that was metered and charged in August: the usage row is ignored and no
-- debit is posted. The call is placed, the client is not charged, and nothing
-- reports a problem.
--
-- THE IDEMPOTENCY KEY IS DELIBERATELY NOT REWRITTEN FOR OLD ROWS.
-- `debitIdempotencyKey()` keeps returning `call:run:<id>` for provider 'voice'
-- and returns `call:vaani:run:<id>` for Vaani. Changing the historical format
-- would make every past call look unbilled and the next sweep would charge the
-- client a second time for a thousand calls.
--
-- Idempotent and safe to re-run.
-- ═══════════════════════════════════════════════════════════════════════════

begin;

alter table public.call_usage
  add column if not exists provider text not null default 'voice';

update public.call_usage
   set provider = 'voice'
 where provider is null;

-- The upsert's conflict target has to match this index exactly, so the single
-- column version must go or PostgREST will keep resolving to it.
--
-- THE CONSTRAINT COMES FIRST. On this table the uniqueness was declared as a
-- table constraint, and Postgres refuses to drop the index underneath one:
--
--   ERROR: 2BP01: cannot drop index call_usage_dograh_run_id_key because
--   constraint call_usage_dograh_run_id_key on table call_usage requires it
--
-- `drop index if exists` does NOT skip that case - the index exists, it is
-- simply not droppable on its own - so the whole migration aborts. Dropping the
-- constraint takes its index with it, and the drops below then clean up any
-- plain index left over from an earlier hand-made attempt.
alter table public.call_usage
  drop constraint if exists call_usage_dograh_run_id_key;

drop index if exists call_usage_dograh_run_id_key;
drop index if exists call_usage_dograh_run_id_idx;

create unique index if not exists call_usage_provider_run_unique
  on public.call_usage (provider, dograh_run_id);

commit;

-- `unbilled_calls()` is declared `returns setof public.call_usage` and selects
-- `*`, so it starts returning `provider` the moment this runs. No function needs
-- redefining, and the sweep's debit keys become provider-correct on its own.

-- ═══════════════════════════════════════════════════════════════════════════
-- VERIFY
-- ═══════════════════════════════════════════════════════════════════════════
--
--   select provider, count(*) from public.call_usage group by provider;
--
--   select indexname, indexdef from pg_indexes
--    where tablename = 'call_usage' and indexdef ilike '%unique%';
--
--   -- nothing should come back:
--   select provider, dograh_run_id, count(*)
--     from public.call_usage group by 1,2 having count(*) > 1;
-- ═══════════════════════════════════════════════════════════════════════════
