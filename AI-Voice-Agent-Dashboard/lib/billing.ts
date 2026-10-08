import type { SupabaseClient } from '@supabase/supabase-js';
import { createHash } from 'crypto';
import { LEGACY_PROVIDER } from './call-provider';

/**
 * The billing engine.
 *
 * Follows the lib/reconcile.ts convention: every function takes a Supabase
 * client as a parameter rather than constructing one, so the pure maths stays
 * testable and nothing here reaches for global state.
 *
 * All money is integer MILLI-CREDITS. 1 credit = Rs 1 = 1000 milli. With
 * whole-minute billing, one minute is exactly 4000 milli — no rounding, no
 * float drift, ever.
 */

export const MILLI_PER_CREDIT = 1000;

export interface BillingConfig {
  accountId: string;
  balanceMilli: number;
  lifetimePurchasedMilli: number;
  lifetimeSpentMilli: number;
  rateMilliPerMinute: number;
  billingIncrementSeconds: number;
  minimumBillableSeconds: number;
  lowBalanceMilli: number;
  criticalBalanceMilli: number;
  autoPauseAtMilli: number;
  autoPauseEnabled: boolean;
  currency: string;
  rateCard: RateCard;
}

export interface RateCard {
  currency?: string;
  telephony?: Record<string, { inr_per_minute: number; increment_seconds?: number }>;
  llm?: Record<string, { inr_per_million_input: number; inr_per_million_output: number }>;
  tts?: Record<string, { inr_per_1k_chars: number }>;
  stt?: Record<string, { inr_per_minute: number; billed_on?: string }>;
  overhead?: { inr_per_call?: number };
}

/** A metered call, as stored in call_usage in the billing database. */
export interface MeteredCall {
  id?: string;
  dograh_run_id: number;
  duration_seconds: number | null;
  call_mode: string | null;
  outcome: string | null;
  usage_info: Record<string, any> | null;
  campaign_id?: number | null;
  called_at?: string;
  /**
   * Which backend issued `dograh_run_id`. Absent until 010 is applied, and
   * absent means the old backend, which is what every pre-cutover row is.
   */
  provider?: string | null;
}

const DEFAULTS = {
  rateMilliPerMinute: 4000,
  // Whole-minute pulse billing: a 61-second call is charged as two minutes.
  //
  // This is the owner's pricing decision, and the arithmetic supports it. A
  // minute of talking costs roughly Rs 4.66 in provider fees against Rs 4.00
  // charged, so per-second billing loses money on every call over a minute.
  // Pulse billing is also the norm for Indian telephony, and the carrier bills
  // us the same way.
  //
  // The trade is that a 61-second call costs the client the same as a
  // 119-second one. That is defensible only because it is stated plainly
  // wherever the rate appears — see the pill tooltip, the top-up dialog and the
  // billing panel, which all say "rounded up to the minute".
  billingIncrementSeconds: 60,
  minimumBillableSeconds: 60,
  lowBalanceMilli: 200_000,
  criticalBalanceMilli: 50_000,
  autoPauseAtMilli: 20_000,
};

/**
 * Call modes that represent a real phone call. Everything else is a test:
 * 'smallwebrtc' is the browser tester and 'textchat' is the text simulator,
 * both of which appear in real Dograh data and must never be charged for.
 */
const BILLABLE_MODES = new Set(['vobiz']);

/** Outcomes where the customer actually talked to the agent. */
const BILLABLE_OUTCOMES = new Set(['completed']);

// ─────────────────────────────────────────────────────────────────────────────
// Config
// ─────────────────────────────────────────────────────────────────────────────

export async function getBillingConfig(
  billing: SupabaseClient,
  accountId = 'default',
): Promise<BillingConfig> {
  const { data, error } = await billing
    .from('billing_account')
    .select('*')
    .eq('id', accountId)
    .single();

  if (error || !data) {
    throw new Error(`billing: could not load account ${accountId}: ${error?.message ?? 'not found'}`);
  }

  const clampInt = (v: any, fallback: number, min: number, max: number) => {
    const n = Number(v);
    return Number.isFinite(n) && n >= min && n <= max ? Math.round(n) : fallback;
  };

  return {
    accountId: data.id,
    balanceMilli: Number(data.balance_milli_credits) || 0,
    lifetimePurchasedMilli: Number(data.lifetime_purchased_milli) || 0,
    lifetimeSpentMilli: Number(data.lifetime_spent_milli) || 0,
    rateMilliPerMinute: clampInt(data.rate_milli_per_minute, DEFAULTS.rateMilliPerMinute, 1, 1_000_000),
    billingIncrementSeconds: clampInt(data.billing_increment_seconds, DEFAULTS.billingIncrementSeconds, 1, 600),
    minimumBillableSeconds: clampInt(data.minimum_billable_seconds, DEFAULTS.minimumBillableSeconds, 0, 600),
    lowBalanceMilli: clampInt(data.low_balance_milli, DEFAULTS.lowBalanceMilli, 0, Number.MAX_SAFE_INTEGER),
    criticalBalanceMilli: clampInt(data.critical_balance_milli, DEFAULTS.criticalBalanceMilli, 0, Number.MAX_SAFE_INTEGER),
    autoPauseAtMilli: clampInt(data.auto_pause_at_milli, DEFAULTS.autoPauseAtMilli, 0, Number.MAX_SAFE_INTEGER),
    autoPauseEnabled: data.auto_pause_enabled !== false,
    currency: data.currency ?? 'INR',
    rateCard: (data.rate_card ?? {}) as RateCard,
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Pure billing maths
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Whether a call may be charged for, and why not when it may not.
 *
 * Unknown modes are treated as NOT billable on purpose. A missed charge is
 * recoverable on the next sweep once the mode is known; an incorrect charge to
 * a client is not.
 */
export function isBillableCall(
  call: MeteredCall,
): { billable: boolean; reason: string } {
  const mode = (call.call_mode ?? '').toLowerCase();
  if (!mode) return { billable: false, reason: 'call mode unknown' };
  if (!BILLABLE_MODES.has(mode)) return { billable: false, reason: `mode "${mode}" is a test, not a phone call` };

  const outcome = (call.outcome ?? '').toLowerCase();
  if (!BILLABLE_OUTCOMES.has(outcome)) return { billable: false, reason: `outcome "${outcome || 'unknown'}" never connected` };

  const seconds = Number(call.duration_seconds);
  if (!Number.isFinite(seconds) || seconds <= 0) return { billable: false, reason: 'zero talk time' };

  return { billable: true, reason: 'connected phone call' };
}

/**
 * Is this run finished enough to be charged on?
 *
 * Rows in call_usage are insert-if-absent, so the FIRST write decides what the
 * client is billed, permanently. A run read mid-call arrives with no duration
 * and no disposition; postDebit then sees zero talk time, stamps billed_at to
 * stop reconsidering it, and a real conversation is settled at zero for ever.
 *
 * Measured on run 571 (2026-09-04): a 29-second call that began at 12:59:40 was
 * swept at 13:00:10, while it was still connected, and billed as nothing.
 *
 * `is_completed` is the test rather than `duration > 0`, because a FINISHED call
 * with no talk time is a genuine no-answer and should be recorded once as
 * unbillable. An unfinished one is simply left for the next tick.
 */
export function isMeterable(run: any): boolean {
  return Boolean(run?.is_completed);
}

/**
 * Billed seconds: a minimum charge, then rounded UP to the next whole minute.
 *
 *   14s  ->  60s  ->  4.00 credits
 *   54s  ->  60s  ->  4.00
 *   61s  -> 120s  ->  8.00   <- a second past the minute costs a full minute
 *  143s  -> 180s  -> 12.00
 *
 * Both the minimum and the increment are configurable per account, so this can
 * be softened to per-second billing (increment 1) without a code change if the
 * cost side ever improves enough to afford it.
 */
export function billableSecondsFor(call: MeteredCall, config: BillingConfig): number {
  if (!isBillableCall(call).billable) return 0;
  const seconds = Math.max(0, Math.round(Number(call.duration_seconds) || 0));
  const charged = Math.max(seconds, config.minimumBillableSeconds);
  const inc = Math.max(1, config.billingIncrementSeconds);
  return Math.ceil(charged / inc) * inc;
}

/** Milli-credits for a span of billed seconds. Exact when increment is 60. */
export function creditsForSeconds(seconds: number, config: BillingConfig): number {
  return Math.round((seconds * config.rateMilliPerMinute) / 60);
}

export const toCredits = (milli: number): number => Math.round(milli / 10) / 100;
export const toMilli = (credits: number): number => Math.round(credits * MILLI_PER_CREDIT);

// ─────────────────────────────────────────────────────────────────────────────
// GST
// ─────────────────────────────────────────────────────────────────────────────

/**
 * GST on a top-up. 18% is the rate for the service being sold here.
 *
 * The rate lives in ONE constant and every rupee figure the client is ever
 * shown — the dialog, the UPI QR, the stored request, the operator queue — is
 * derived from gstOn(). If it were computed in each place, the QR would sooner
 * or later ask for a different amount than the screen above it, and every one of
 * those payments would land as a mismatched manual reconciliation.
 */
export const GST_RATE_PERCENT = 18;

export interface GstBreakdown {
  /** Credits bought. 1 credit = Rs 1 of calling, and this is what gets credited. */
  credits: number;
  /** Rs before tax. */
  baseInr: number;
  gstRatePercent: number;
  gstInr: number;
  /** Rs actually payable — base + GST. This is what the QR carries. */
  totalInr: number;
}

/**
 * Rupees payable for a number of credits.
 *
 * GST is charged ON TOP: 1000 credits is Rs 1000 + Rs 180 = Rs 1180 payable,
 * and the client still receives exactly 1000 credits of calling. Tax is not
 * calling time, so it must never be turned into credits.
 *
 * Rounded to paise, then to whole rupees for the total, because a UPI QR
 * carrying Rs 1180.00 is what the client's app will collect and an operator
 * matching payments should not have to reason about fractions of a paisa.
 */
export function gstOn(credits: number, ratePercent: number = GST_RATE_PERCENT): GstBreakdown {
  const base = Math.max(0, Math.round(Number(credits) || 0));
  const gst = Math.round((base * ratePercent) / 100 * 100) / 100;
  return {
    credits: base,
    baseInr: base,
    gstRatePercent: ratePercent,
    gstInr: gst,
    totalInr: Math.round((base + gst) * 100) / 100,
  };
}

/**
 * THE single place a call-debit idempotency key is constructed.
 *
 * Never inline this string anywhere else. If the webhook path and the cron
 * sweep ever compute it differently, both post and the client is charged twice
 * for one call.
 */
export function debitIdempotencyKey(dograhRunId: number, provider?: string): string {
  // The historical format is preserved for the old backend on purpose. Every
  // charge posted before the 2026-09-03 cutover carries `call:run:<id>`, and
  // rewriting the shape would make a thousand already-billed calls look unbilled
  // to the next sweep, which would then charge the client for them a second
  // time. Only the new backend gets a namespaced key — which is what stops a
  // Vaani call being waved through as "already billed" because voice used that
  // run id in August.
  if (provider && provider !== LEGACY_PROVIDER) {
    return `call:${provider}:run:${dograhRunId}`;
  }
  return `call:run:${dograhRunId}`;
}

export function hashPhone(phone: string | null | undefined): string | null {
  if (!phone) return null;
  return createHash('sha256').update(String(phone).replace(/\D/g, '')).digest('hex').slice(0, 32);
}

// ─────────────────────────────────────────────────────────────────────────────
// Provider cost — OPERATOR ONLY, never exposed to the client
// ─────────────────────────────────────────────────────────────────────────────

export interface ProviderCost {
  totalMilli: number;
  breakdown: { llm: number; tts: number; stt: number; telephony: number; overhead: number };
  estimatedComponents: string[];
  unpricedModels: string[];
}

/**
 * What a call actually cost us, in milli-credits (1000 = Rs 1).
 *
 * Dograh reports no money at all — `dograh_token_usage` is always 0 on a
 * self-hosted instance with your own provider keys — so every figure here is
 * computed from raw usage against the rate card.
 *
 * usage_info shape, from real data:
 *   llm: { "GroqLLMService#7|||llama-3.3-70b-versatile": { prompt_tokens, completion_tokens } }
 *   tts: { "CartesiaTTSService#6|||sonic-3.5": 110 }     <- CHARACTER count
 *   stt: {}                                              <- Deepgram reports nothing
 */
export function estimateProviderCost(
  call: MeteredCall,
  rateCard: RateCard,
): ProviderCost {
  const breakdown = { llm: 0, tts: 0, stt: 0, telephony: 0, overhead: 0 };
  const estimated: string[] = [];
  const unpriced: string[] = [];
  const usage = call.usage_info ?? {};
  const seconds = Math.max(0, Number(call.duration_seconds) || 0);
  const inr = (rupees: number) => Math.round(rupees * MILLI_PER_CREDIT);

  // Dograh keys look like "GroqLLMService#7|||llama-3.3-70b-versatile".
  const modelOf = (key: string) => key.split('|||')[1] ?? key;
  const providerOf = (key: string) => {
    const cls = key.split('|||')[0] ?? '';
    const m = cls.match(/^([A-Za-z]+?)(LLM|TTS|STT)Service/);
    return m ? m[1].toLowerCase() : '';
  };

  // ── LLM ────────────────────────────────────────────────────────────────────
  for (const [key, val] of Object.entries(usage.llm ?? {})) {
    if (!val || typeof val !== 'object') continue;
    const rateKey = `${providerOf(key)}/${modelOf(key)}`;
    const rate = rateCard.llm?.[rateKey];
    if (!rate) { unpriced.push(rateKey); continue; }
    const v = val as any;
    breakdown.llm += inr(
      ((Number(v.prompt_tokens) || 0) / 1e6) * rate.inr_per_million_input +
      ((Number(v.completion_tokens) || 0) / 1e6) * rate.inr_per_million_output,
    );
  }

  // ── TTS (value is a character count) ───────────────────────────────────────
  for (const [key, val] of Object.entries(usage.tts ?? {})) {
    const chars = Number(val);
    if (!Number.isFinite(chars)) continue;
    const rateKey = `${providerOf(key)}/${modelOf(key)}`;
    const rate = rateCard.tts?.[rateKey];
    if (!rate) { unpriced.push(rateKey); continue; }
    breakdown.tts += inr((chars / 1000) * rate.inr_per_1k_chars);
  }

  // ── STT (never reported, so derived from duration) ─────────────────────────
  const sttEntries = Object.entries(usage.stt ?? {});
  const sttRateKey = Object.keys(rateCard.stt ?? {})[0];
  if (sttRateKey && sttEntries.length === 0 && seconds > 0) {
    breakdown.stt += inr((seconds / 60) * (rateCard.stt![sttRateKey].inr_per_minute || 0));
    estimated.push('stt');
  }

  // ── Telephony (billed in whole minutes by the carrier) ─────────────────────
  const telcoKey = (call.call_mode ?? 'vobiz').toLowerCase();
  const telco = rateCard.telephony?.[telcoKey];
  if (telco && seconds > 0) {
    const inc = telco.increment_seconds || 60;
    breakdown.telephony += inr((Math.ceil(seconds / inc) * inc / 60) * telco.inr_per_minute);
  } else if (seconds > 0) {
    unpriced.push(`telephony/${telcoKey}`);
  }

  if (rateCard.overhead?.inr_per_call) breakdown.overhead += inr(rateCard.overhead.inr_per_call);

  const totalMilli = Object.values(breakdown).reduce((a, b) => a + b, 0);
  return { totalMilli, breakdown, estimatedComponents: estimated, unpricedModels: [...new Set(unpriced)] };
}

// ─────────────────────────────────────────────────────────────────────────────
// Writes
// ─────────────────────────────────────────────────────────────────────────────

export interface DebitResult {
  posted: boolean;
  duplicate: boolean;
  balanceMilli: number | null;
  amountMilli: number;
  skippedReason?: string;
}

/**
 * Charge for one metered call.
 *
 * NEVER THROWS. A billing failure must never cost us the call record or break
 * an ingestion path — the sweep will retry on the next cron tick.
 */
export async function postDebit(
  billing: SupabaseClient,
  call: MeteredCall,
  config: BillingConfig,
): Promise<DebitResult> {
  const nil: DebitResult = { posted: false, duplicate: false, balanceMilli: null, amountMilli: 0 };

  try {
    const verdict = isBillableCall(call);
    if (!verdict.billable) {
      // Mark it settled at zero so the sweep stops reconsidering it forever.
      if (call.id) {
        await billing.from('call_usage').update({ billed_at: new Date().toISOString() }).eq('id', call.id);
      }
      return { ...nil, skippedReason: verdict.reason };
    }

    const billedSeconds = billableSecondsFor(call, config);
    const amountMilli = creditsForSeconds(billedSeconds, config);
    if (amountMilli <= 0) return { ...nil, skippedReason: 'computed zero' };

    const cost = estimateProviderCost(call, config.rateCard);
    const minutes = Math.round(billedSeconds / 60);

    const { data, error } = await billing.rpc('post_credit_entry', {
      // `call.provider` is present once 010 is applied; before that it is
      // undefined and the key keeps its historical shape, which is correct for
      // every row that predates the cutover.
      p_idempotency_key: debitIdempotencyKey(call.dograh_run_id, call.provider ?? undefined),
      p_entry_type: 'call_debit',
      p_amount_milli: -amountMilli,
      p_ledger_secret: process.env.LEDGER_SECRET ?? '',
      p_account_id: config.accountId,
      p_description: `Call — ${minutes} min${minutes === 1 ? '' : 's'}${
        call.duration_seconds ? ` (talk time ${call.duration_seconds}s)` : ''
      }`,
      p_dograh_run_id: call.dograh_run_id,
      p_call_usage_id: call.id ?? null,
      p_campaign_id: call.campaign_id ?? null,
      p_billed_seconds: billedSeconds,
      p_actual_seconds: Math.round(Number(call.duration_seconds) || 0),
      p_rate_milli_per_minute: config.rateMilliPerMinute,
      p_billing_increment_seconds: config.billingIncrementSeconds,
      p_provider_cost_milli: cost.totalMilli,
      p_provider_cost_breakdown: {
        ...cost.breakdown,
        estimated: cost.estimatedComponents,
        unpriced: cost.unpricedModels,
      },
      p_created_by: 'system',
      p_metadata: null,
    });

    if (error) {
      console.error('[billing] postDebit rpc failed', { run: call.dograh_run_id, error: error.message });
      return nil;
    }

    const row = Array.isArray(data) ? data[0] : data;
    return {
      posted: !row?.already_posted,
      duplicate: Boolean(row?.already_posted),
      balanceMilli: row?.balance_milli ?? null,
      amountMilli,
    };
  } catch (err: any) {
    console.error('[billing] postDebit threw', { run: call.dograh_run_id, error: err?.message });
    return nil;
  }
}

/**
 * Charge for every metered call that has not been billed yet.
 *
 * This is the correctness guarantee. Inline debits elsewhere are only a latency
 * optimisation; this is what makes "no call escapes billing" actually true.
 */
export async function sweepUnbilledCalls(
  billing: SupabaseClient,
  config: BillingConfig,
  limit = 200,
): Promise<{ scanned: number; billed: number; skipped: number; creditsCharged: number }> {
  const result = { scanned: 0, billed: 0, skipped: 0, creditsCharged: 0 };

  const { data, error } = await billing.rpc('unbilled_calls', {
    p_limit: limit,
    p_account_id: config.accountId,
  });
  if (error) {
    console.error('[billing] sweep could not list unbilled calls', error.message);
    return result;
  }

  for (const call of (data ?? []) as MeteredCall[]) {
    result.scanned += 1;
    const outcome = await postDebit(billing, call, config);
    if (outcome.posted) {
      result.billed += 1;
      result.creditsCharged += toCredits(outcome.amountMilli);
    } else {
      result.skipped += 1;
    }
  }

  return result;
}

/** Balance state used by the pill and the enforcement guard. */
export type BalanceState = 'healthy' | 'low' | 'critical' | 'empty';

export function balanceState(balanceMilli: number, config: BillingConfig): BalanceState {
  if (balanceMilli <= 0) return 'empty';
  if (balanceMilli <= config.criticalBalanceMilli) return 'critical';
  if (balanceMilli <= config.lowBalanceMilli) return 'low';
  return 'healthy';
}
