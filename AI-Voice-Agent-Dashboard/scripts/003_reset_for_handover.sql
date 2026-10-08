-- ============================================================================
-- 003_reset_for_handover.sql
--
-- Wipes test data so the client starts from a genuine zero.
--
-- IRREVERSIBLE. There is no undo and no soft-delete. Read the whole file before
-- running any of it.
--
-- This is deliberately NOT wired to any API route, cron or button. It must
-- never be reachable over HTTP — it is pasted into the SQL editor by hand, on
-- purpose, once.
--
-- The two halves run against DIFFERENT Supabase projects. Running part B in the
-- client's project does nothing (the tables are not there); running part A in
-- the billing project does nothing either. Check which project the SQL editor
-- is pointed at before each part.
-- ============================================================================


-- ─── PART A — the CLIENT project (ciydgkugezflbhgqokwn) ─────────────────────
-- Leads, calls, campaigns. Run this in the client's Supabase.
--
-- TRUNCATE rather than DELETE: it is far faster, resets identity counters, and
-- CASCADE follows the foreign keys so nothing is left orphaned.

truncate table
  public.call_logs,
  public.rejected_leads,
  public.leads,
  public.campaign_runs,
  public.upload_batches
restart identity cascade;

-- NOTE: public.settings is deliberately NOT cleared. It holds workspace
-- configuration — agent behaviour, call behaviour, notification preferences —
-- not customer data. Wiping it resets the agent's settings to defaults.
-- Uncomment only if you genuinely want that too:
--
-- truncate table public.settings;


-- ─── PART B — the BILLING project (iethhjknjnecnslsafms) ────────────────────
-- Credits, ledger, metered calls. Run this in YOUR billing Supabase.
--
-- Skip this part if you only want to clear leads and keep the credit history.
--
-- credit_ledger has an append-only trigger that blocks DELETE. TRUNCATE is not
-- affected — Postgres does not fire row-level triggers for it — which is the
-- other reason this file uses TRUNCATE throughout.

truncate table
  public.credit_ledger,
  public.call_usage,
  public.topup_requests
restart identity cascade;

-- The account row is kept and reset, rather than deleted: the rate card, your
-- UPI details and the pricing all live on it, and deleting it would take them
-- with it. Only the money is zeroed.
update public.billing_account
   set balance_milli_credits    = 0,
       lifetime_purchased_milli = 0,
       lifetime_spent_milli     = 0
 where id = 'default';


-- ─── VERIFY ─────────────────────────────────────────────────────────────────
-- In the client project:
--   select
--     (select count(*) from public.leads)          as leads,
--     (select count(*) from public.call_logs)      as calls,
--     (select count(*) from public.campaign_runs)  as campaigns;
--
-- In the billing project — drift MUST be 0, and it will be, because a zeroed
-- balance against an empty ledger still agrees:
--   select * from public.billing_balance_check;


-- ─── WHAT THIS DOES NOT TOUCH ───────────────────────────────────────────────
-- * Call recordings and transcripts. Those live on the Dograh server, not in
--   either database. Clearing call_logs removes the dashboard's links to them;
--   the audio files themselves remain until deleted on that server.
-- * Dograh's own campaign and run history. Metering reads from there, so the
--   next cron tick will re-meter every past call and, once credits exist, bill
--   for them again. Either clear the campaigns in Dograh too, or accept that
--   history reappears in call_usage.
-- * Dashboard logins. Those are Supabase Auth users; remove them in the
--   operator console under Logins, or in Supabase → Authentication → Users.
-- * The rate card, UPI details and pricing on billing_account, all kept above.
