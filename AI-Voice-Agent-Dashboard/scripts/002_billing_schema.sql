-- ============================================================================
-- 002_billing_schema.sql
--
-- RUN THIS AGAINST THE **BILLING** SUPABASE PROJECT ONLY.
--   billing project : iethhjknjnecnslsafms   <- here
--   client project  : ciydgkugezflbhgqokwn   <- NOT here (001_init_schema.sql)
--
-- The client owns and can freely edit the client project. Credits therefore
-- live in this separate project, which he has no login to. Nothing in here
-- references his tables: cross-project foreign keys do not exist, and any
-- display text a statement needs is denormalised onto the row.
--
-- MONEY UNIT: integer MILLI-CREDITS (bigint). 1 credit = Rs 1 = 1000 milli.
-- Never floats. With whole-minute billing, 1 minute = exactly 4000 milli.
-- ============================================================================

-- Supabase ships pgcrypto in the `extensions` schema, not `public`. Both
-- functions below therefore set search_path to include it, or hmac() will not
-- resolve at runtime and every ledger write will fail.
create extension if not exists pgcrypto with schema extensions;

-- ─── shared trigger helper ──────────────────────────────────────────────────
create or replace function public.touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;


-- ════════════════════════════════════════════════════════════════════════════
-- call_usage — our own meter reading
--
-- Deliberately a SEPARATE copy of what the client's call_logs holds. Billing is
-- computed from THIS table, populated straight from the Dograh API on our own
-- server. If the client deletes rows from his call_logs to dodge a charge, his
-- bill does not move.
--
-- We store a hash of the phone number rather than the number. We do not need
-- his lead list, and not holding it is the better position to be in.
-- ════════════════════════════════════════════════════════════════════════════
create table if not exists public.call_usage (
  id                uuid primary key default gen_random_uuid(),
  account_id        text not null default 'default',

  -- Dograh's run id. UNIQUE and NOT NULL: this is the idempotency barrier for
  -- metering. Unlike the client's call_logs (where it is nullable, so duplicate
  -- rows are already possible), we refuse to record a run we cannot identify.
  dograh_run_id     bigint not null unique,

  campaign_id       bigint,
  duration_seconds  integer not null default 0,
  call_mode         text,              -- 'vobiz' billable; 'smallwebrtc'/'textchat' not
  outcome           text,
  phone_hash        text,              -- sha256(phone), for dispute lookup only
  usage_info        jsonb,             -- llm tokens / tts chars -> provider cost
  cost_info         jsonb,
  called_at         timestamptz not null,
  fetched_at        timestamptz not null default now(),

  -- Set once the debit is posted, so the sweep can find unbilled rows cheaply.
  billed_at         timestamptz,
  ledger_entry_id   uuid
);

create index if not exists call_usage_unbilled_idx
  on public.call_usage (called_at)
  where billed_at is null;
create index if not exists call_usage_called_idx on public.call_usage (called_at desc);
create index if not exists call_usage_campaign_idx on public.call_usage (campaign_id);


-- ════════════════════════════════════════════════════════════════════════════
-- billing_account
--
-- One row per client. Today there is exactly one ('default'), but every table
-- carries account_id so a second client is a data change, not a rebuild.
--
-- balance_milli_credits is a CACHE for fast reads. credit_ledger is the truth;
-- billing_balance_check below proves they agree.
-- ════════════════════════════════════════════════════════════════════════════
create table if not exists public.billing_account (
  id                        text primary key default 'default',
  display_name              text not null default 'BS Wealth Finance',

  balance_milli_credits     bigint  not null default 0,
  lifetime_purchased_milli  bigint  not null default 0,
  lifetime_spent_milli      bigint  not null default 0,

  -- Pricing. 4 credits per minute, billed in whole minutes because the telco
  -- bills us the same way (a 17s call came back with BillDuration 60).
  rate_milli_per_minute     integer not null default 4000,
  -- Whole-minute pulse billing: a 61-second call is charged as two minutes.
  -- A minute of talking costs about Rs 4.66 in provider fees against Rs 4.00
  -- charged, so per-second billing loses money on any call over a minute.
  -- Pulse billing is standard for Indian telephony and the carrier bills us the
  -- same way. Set billing_increment_seconds to 1 to switch to per-second.
  billing_increment_seconds integer not null default 60,
  minimum_billable_seconds  integer not null default 60,

  low_balance_milli         bigint  not null default 200000,  -- 200 credits
  critical_balance_milli    bigint  not null default 50000,   --  50 credits

  -- Above zero on purpose. Dograh dials several calls in parallel, so some are
  -- always already ringing when the pause lands. Stopping at exactly 0
  -- guarantees an overdraft.
  auto_pause_at_milli       bigint  not null default 20000,   --  20 credits
  auto_pause_enabled        boolean not null default true,

  currency                  text not null default 'INR',

  -- What WE pay each provider, used only for the operator margin view. This is
  -- the most commercially sensitive data in the system and is the main reason
  -- none of this lives in the client's project. Never returned by a
  -- client-facing endpoint.
  rate_card                 jsonb not null default '{
    "currency": "INR",
    "telephony": { "vobiz": { "inr_per_minute": 0.55, "increment_seconds": 60 } },
    "llm":  { "groq/llama-3.3-70b-versatile": { "inr_per_million_input": 52, "inr_per_million_output": 70 },
              "groq/llama-3.1-8b-instant":    { "inr_per_million_input": 4.4, "inr_per_million_output": 7.0 } },
    "tts":  { "cartesia/sonic-3.5": { "inr_per_1k_chars": 2.5 } },
    "stt":  { "deepgram/nova-3-general": { "inr_per_minute": 0.68, "billed_on": "call_duration" } },
    "overhead": { "inr_per_call": 0.05 }
  }'::jsonb,

  updated_at                timestamptz not null default now(),

  constraint billing_account_rate_ck check (rate_milli_per_minute > 0),
  constraint billing_account_increment_ck check (billing_increment_seconds between 1 and 600)
);

insert into public.billing_account (id) values ('default') on conflict (id) do nothing;

-- Payment details shown on the client's top-up screen. Kept in the database
-- rather than an env var so they can be changed from the operator console
-- without a redeploy. `create table if not exists` above will not add this to
-- an existing table, hence the explicit ALTER — this whole file is re-runnable.
alter table public.billing_account
  add column if not exists minimum_billable_seconds integer not null default 60;

alter table public.billing_account
  add column if not exists payment_instructions jsonb not null default '{
    "upi_id": "",
    "payee_name": "BS Financial Services",
    "qr_image_url": "",
    "bank": { "account_name": "", "account_number": "", "ifsc": "", "bank_name": "" },
    "note": "After paying, enter the UPI reference number so we can match your payment."
  }'::jsonb;

drop trigger if exists billing_account_touch on public.billing_account;
create trigger billing_account_touch before update on public.billing_account
  for each row execute function public.touch_updated_at();


-- ════════════════════════════════════════════════════════════════════════════
-- credit_ledger — append-only, hash-chained
--
-- Signed amounts, so the balance is just sum(amount_milli_credits).
-- Every row hashes the row before it, keyed with a secret held only in the
-- application environment. Editing, inserting or deleting any row breaks the
-- chain from that point on and verify_ledger_chain() reports exactly where.
-- ════════════════════════════════════════════════════════════════════════════
create table if not exists public.credit_ledger (
  id                    uuid primary key default gen_random_uuid(),
  account_id            text not null default 'default'
                          references public.billing_account (id) on delete restrict,
  seq                   bigserial not null,

  entry_type            text not null check (entry_type in (
                          'call_debit','topup_credit','opening_balance',
                          'adjustment_credit','adjustment_debit',
                          'refund_credit','promo_credit')),
  amount_milli_credits  bigint not null check (amount_milli_credits <> 0),
  balance_after_milli   bigint,

  -- THE idempotency barrier. Built only by debitIdempotencyKey() in
  -- lib/billing.ts. If two code paths ever compute this differently, the client
  -- gets charged twice — so it is constructed in exactly one place.
  idempotency_key       text not null unique,

  -- What this line item was, in plain words, frozen at write time. A bill must
  -- stay readable years later, and we cannot join to the client's lead table.
  description           text,

  dograh_run_id         bigint,
  call_usage_id         uuid references public.call_usage (id) on delete set null,
  campaign_id           bigint,

  -- How the number was derived, so a disputed charge can be defended without
  -- re-running the code that produced it.
  billed_seconds            integer,
  actual_seconds            integer,
  rate_milli_per_minute     integer,
  billing_increment_seconds integer,

  -- OPERATOR ONLY. Never selected by any client-facing endpoint.
  provider_cost_milli       bigint,
  provider_cost_breakdown   jsonb,

  external_ref          text,          -- UPI txn ref / bank ref / payment id
  created_by            text,          -- 'system' | 'operator'
  metadata              jsonb,

  prev_hash             text,
  entry_hash            text,

  created_at            timestamptz not null default now(),

  constraint credit_ledger_sign_ck check (
       (entry_type in ('call_debit','adjustment_debit') and amount_milli_credits < 0)
    or (entry_type in ('topup_credit','adjustment_credit','refund_credit','promo_credit')
        and amount_milli_credits > 0)
    or  entry_type = 'opening_balance'
  )
);

-- Belt and braces beyond idempotency_key: one debit per metered call, ever.
create unique index if not exists credit_ledger_call_debit_uniq
  on public.credit_ledger (dograh_run_id)
  where entry_type = 'call_debit' and dograh_run_id is not null;

create index if not exists credit_ledger_created_idx on public.credit_ledger (created_at desc);
create index if not exists credit_ledger_seq_idx     on public.credit_ledger (account_id, seq);
create index if not exists credit_ledger_type_idx    on public.credit_ledger (entry_type, created_at desc);


-- ─── append-only enforcement ────────────────────────────────────────────────
-- Corrections are made with a reversing entry, never by rewriting history.
create or replace function public.credit_ledger_append_only()
returns trigger language plpgsql as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'credit_ledger is append-only: entry % cannot be deleted', old.id;
  end if;
  if old.amount_milli_credits is distinct from new.amount_milli_credits
     or old.entry_type      is distinct from new.entry_type
     or old.idempotency_key is distinct from new.idempotency_key
     or old.entry_hash      is distinct from new.entry_hash
     or old.created_at      is distinct from new.created_at then
    raise exception
      'credit_ledger is append-only: entry % cannot be amended; post a reversing entry', old.id;
  end if;
  return new;
end;
$$;

drop trigger if exists credit_ledger_no_amend on public.credit_ledger;
create trigger credit_ledger_no_amend before update or delete on public.credit_ledger
  for each row execute function public.credit_ledger_append_only();


-- ════════════════════════════════════════════════════════════════════════════
-- topup_requests — the manual payment queue
--
-- The client can CREATE a request from the dashboard. Only an operator can
-- approve one, and approval is the only path by which credits come into
-- existence. There is deliberately no code path where the client's browser can
-- increase a balance.
-- ════════════════════════════════════════════════════════════════════════════
create table if not exists public.topup_requests (
  id                uuid primary key default gen_random_uuid(),
  account_id        text not null default 'default'
                      references public.billing_account (id) on delete restrict,
  credits_requested integer not null check (credits_requested > 0),
  amount_inr        numeric(12,2) not null,
  status            text not null default 'pending'
                      check (status in ('pending','approved','rejected','cancelled')),
  method            text not null default 'manual_upi',
  reference_note    text,        -- what the client typed (UPI ref, etc.)
  requested_at      timestamptz not null default now(),

  decided_at        timestamptz,
  decided_by        text,
  decision_note     text,
  ledger_entry_id   uuid references public.credit_ledger (id) on delete set null,
  updated_at        timestamptz not null default now()
);

create index if not exists topup_requests_pending_idx
  on public.topup_requests (requested_at desc) where status = 'pending';

drop trigger if exists topup_requests_touch on public.topup_requests;
create trigger topup_requests_touch before update on public.topup_requests
  for each row execute function public.touch_updated_at();


-- ════════════════════════════════════════════════════════════════════════════
-- post_credit_entry — the ONLY way money moves
--
-- Insert happens FIRST. A duplicate delivery raises 23505 and returns before
-- the balance is touched, which is what makes the webhook and the cron sweep
-- safe to run against the same call.
--
-- p_ledger_secret is passed in per call and never stored, so the hash chain
-- cannot be recomputed by someone holding only a database dump.
-- ════════════════════════════════════════════════════════════════════════════
create or replace function public.post_credit_entry(
  p_idempotency_key           text,
  p_entry_type                text,
  p_amount_milli              bigint,
  p_ledger_secret             text,
  p_account_id                text    default 'default',
  p_description               text    default null,
  p_dograh_run_id             bigint  default null,
  p_call_usage_id             uuid    default null,
  p_campaign_id               bigint  default null,
  p_billed_seconds            integer default null,
  p_actual_seconds            integer default null,
  p_rate_milli_per_minute     integer default null,
  p_billing_increment_seconds integer default null,
  p_provider_cost_milli       bigint  default null,
  p_provider_cost_breakdown   jsonb   default null,
  p_external_ref              text    default null,
  p_created_by                text    default 'system',
  p_metadata                  jsonb   default null
)
returns table (entry_id uuid, balance_milli bigint, already_posted boolean)
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  v_entry_id  uuid;
  v_balance   bigint;
  v_prev_hash text;
  v_hash      text;
begin
  if p_amount_milli = 0 then
    raise exception 'post_credit_entry: amount must be non-zero';
  end if;
  if p_ledger_secret is null or length(p_ledger_secret) < 16 then
    raise exception 'post_credit_entry: a ledger secret is required';
  end if;

  -- Serialise postings on the account row. Without this, two debits landing in
  -- the same millisecond both read the old balance and one update is lost.
  perform 1 from public.billing_account where id = p_account_id for update;

  select entry_hash into v_prev_hash
    from public.credit_ledger
   where account_id = p_account_id
   order by seq desc limit 1;

  v_hash := encode(hmac(
    coalesce(v_prev_hash, 'genesis') || '|' || p_idempotency_key || '|' ||
    p_entry_type || '|' || p_amount_milli::text,
    p_ledger_secret, 'sha256'), 'hex');

  begin
    insert into public.credit_ledger (
      account_id, entry_type, amount_milli_credits, idempotency_key, description,
      dograh_run_id, call_usage_id, campaign_id, billed_seconds, actual_seconds,
      rate_milli_per_minute, billing_increment_seconds, provider_cost_milli,
      provider_cost_breakdown, external_ref, created_by, metadata, prev_hash, entry_hash
    ) values (
      p_account_id, p_entry_type, p_amount_milli, p_idempotency_key, p_description,
      p_dograh_run_id, p_call_usage_id, p_campaign_id, p_billed_seconds, p_actual_seconds,
      p_rate_milli_per_minute, p_billing_increment_seconds, p_provider_cost_milli,
      p_provider_cost_breakdown, p_external_ref, p_created_by, p_metadata,
      v_prev_hash, v_hash
    )
    returning id into v_entry_id;

  exception when unique_violation then
    -- Already billed. Return the existing entry and leave the balance alone.
    select l.id, l.balance_after_milli into v_entry_id, v_balance
      from public.credit_ledger l
     where l.idempotency_key = p_idempotency_key
        or (p_dograh_run_id is not null
            and l.dograh_run_id = p_dograh_run_id
            and l.entry_type = 'call_debit')
     limit 1;

    if v_balance is null then
      select b.balance_milli_credits into v_balance
        from public.billing_account b where b.id = p_account_id;
    end if;

    return query select v_entry_id, v_balance, true;
    return;
  end;

  -- No insufficient-funds check here, deliberately. The call already happened;
  -- refusing to record it would under-charge. Balances may go negative, and
  -- prevention lives in the pre-flight check and the auto-pause guard instead.
  update public.billing_account
     set balance_milli_credits    = balance_milli_credits + p_amount_milli,
         lifetime_purchased_milli = lifetime_purchased_milli + greatest(p_amount_milli, 0),
         lifetime_spent_milli     = lifetime_spent_milli + greatest(-p_amount_milli, 0)
   where id = p_account_id
  returning balance_milli_credits into v_balance;

  update public.credit_ledger set balance_after_milli = v_balance where id = v_entry_id;

  if p_call_usage_id is not null then
    update public.call_usage
       set billed_at = now(), ledger_entry_id = v_entry_id
     where id = p_call_usage_id;
  end if;

  return query select v_entry_id, v_balance, false;
end;
$$;


-- ─── verify_ledger_chain — tamper detection ─────────────────────────────────
-- Walks the chain and returns the first row whose hash does not follow from its
-- predecessor. Empty result = intact.
create or replace function public.verify_ledger_chain(
  p_ledger_secret text,
  p_account_id    text default 'default'
)
returns table (bad_seq bigint, bad_entry_id uuid, reason text)
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  r           record;
  v_expected  text;
  v_prev      text := null;
begin
  for r in
    select * from public.credit_ledger
     where account_id = p_account_id order by seq asc
  loop
    v_expected := encode(hmac(
      coalesce(v_prev, 'genesis') || '|' || r.idempotency_key || '|' ||
      r.entry_type || '|' || r.amount_milli_credits::text,
      p_ledger_secret, 'sha256'), 'hex');

    if r.entry_hash is distinct from v_expected then
      bad_seq := r.seq; bad_entry_id := r.id;
      reason := 'hash mismatch — row altered, or a row before it was inserted or removed';
      return next;
      return;
    end if;

    v_prev := r.entry_hash;
  end loop;
end;
$$;


-- ─── unbilled_calls — what the sweep charges for ────────────────────────────
-- Money POLICY (which modes and outcomes are billable) lives in lib/billing.ts.
-- This only narrows to rows not yet billed.
create or replace function public.unbilled_calls(
  p_limit int default 200,
  p_account_id text default 'default'
)
returns setof public.call_usage
language sql stable as $$
  select * from public.call_usage
   where account_id = p_account_id
     and billed_at is null
   order by called_at asc
   limit p_limit;
$$;


-- ─── drift detector ─────────────────────────────────────────────────────────
create or replace view public.billing_balance_check as
  select b.id as account_id,
         b.balance_milli_credits as cached_milli,
         coalesce((select sum(amount_milli_credits) from public.credit_ledger l
                    where l.account_id = b.id), 0) as ledger_sum_milli,
         b.balance_milli_credits
           - coalesce((select sum(amount_milli_credits) from public.credit_ledger l
                        where l.account_id = b.id), 0) as drift_milli
    from public.billing_account b;


-- ════════════════════════════════════════════════════════════════════════════
-- Lockdown
--
-- Postgres grants EXECUTE on new functions to PUBLIC by default, and PostgREST
-- exposes every public-schema function as an RPC. Without these revokes, anyone
-- holding this project's anon key could call post_credit_entry() and mint
-- themselves credits. RLS does NOT protect a SECURITY DEFINER function.
--
-- Nothing here is reachable by anon/authenticated at all: this database is only
-- ever spoken to by our server, with the service-role key.
-- ════════════════════════════════════════════════════════════════════════════
alter table public.billing_account enable row level security;
alter table public.credit_ledger   enable row level security;
alter table public.call_usage      enable row level security;
alter table public.topup_requests  enable row level security;

revoke all on public.billing_account from anon, authenticated;
revoke all on public.credit_ledger   from anon, authenticated;
revoke all on public.call_usage      from anon, authenticated;
revoke all on public.topup_requests  from anon, authenticated;
revoke all on public.billing_balance_check from anon, authenticated;

revoke execute on function public.post_credit_entry(
  text, text, bigint, text, text, text, bigint, uuid, bigint, integer, integer,
  integer, integer, bigint, jsonb, text, text, jsonb) from public, anon, authenticated;
revoke execute on function public.verify_ledger_chain(text, text) from public, anon, authenticated;
revoke execute on function public.unbilled_calls(int, text) from public, anon, authenticated;
