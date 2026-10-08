-- ════════════════════════════════════════════════════════════════════════════
-- 004 — GST on top-ups
--
-- Run this against the BILLING database (the same one 002 was run against).
--
-- Until now a top-up for 1000 credits stored amount_inr = 1000 and the QR asked
-- for Rs 1000, with no tax anywhere. GST is charged on top of the credits: the
-- client pays Rs 1180 and still receives 1000 credits of calling.
--
-- amount_inr keeps its meaning of "what the client actually pays", so it now
-- holds the GROSS figure, and the two new columns record how that figure was
-- arrived at. Storing the rate per row (rather than reading today's constant
-- when displaying an old request) means a future rate change cannot silently
-- rewrite the history of what was charged.
--
-- Safe to run more than once.
-- ════════════════════════════════════════════════════════════════════════════

alter table public.topup_requests
  add column if not exists base_amount_inr  numeric(12,2),
  add column if not exists gst_rate_percent numeric(5,2) not null default 18,
  add column if not exists gst_amount_inr   numeric(12,2) not null default 0;

comment on column public.topup_requests.base_amount_inr is
  'Rupees before tax. Equals credits_requested at 1 credit = Rs 1.';
comment on column public.topup_requests.gst_amount_inr is
  'Tax charged on top of the base. Never becomes credits.';
comment on column public.topup_requests.amount_inr is
  'GROSS rupees payable — base_amount_inr + gst_amount_inr. This is the figure on the UPI QR.';

-- Backfill rows created before GST existed. Those clients were quoted, and paid,
-- the tax-free amount, so their history is recorded as exactly that: base equal
-- to what they paid, zero GST. Do NOT retro-add tax to a settled payment.
update public.topup_requests
   set base_amount_inr  = amount_inr,
       gst_rate_percent = 0,
       gst_amount_inr   = 0
 where base_amount_inr is null;

alter table public.topup_requests
  alter column base_amount_inr set not null;
