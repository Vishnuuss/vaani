import type { SupabaseClient } from '@supabase/supabase-js';
import { callProvider, hasProviderColumn } from './call-provider';
import { dograh } from './dograh';
import { hashPhone, isMeterable } from './billing';
import { normaliseOutcome } from './call-scoring';

/**
 * Reads the meter.
 *
 * Call durations are pulled STRAIGHT FROM DOGRAH, on our own server, into our
 * own billing database — never from the client's Supabase copy. He owns that
 * project and can edit or delete rows in it; if the bill were computed from
 * there, deleting call_logs would delete the charges with it. Metering from
 * Dograh means his bill does not move no matter what he does to his database.
 *
 * Idempotency: call_usage.dograh_run_id is UNIQUE NOT NULL, so re-running this
 * over the same window is free. Unlike the client's call_logs (where the run id
 * is nullable and duplicates are already possible), we refuse to meter a run we
 * cannot identify.
 */

/**
 * How to identify a call for insert-if-absent, given what the billing database
 * currently has.
 *
 * A run id alone stopped being an identity on 2026-09-03: Vaani numbers from 1
 * and 183 of the ids it has already issued belong to voice-era rows here. An
 * unscoped `onConflict: 'dograh_run_id'` makes a real Vaani call collide with a
 * call billed in August, `ignoreDuplicates` drops it, and the client is never
 * charged for it.
 *
 * The conflict target has to name the columns of the index that actually
 * exists, so this follows the database rather than assuming 010 has been
 * applied.
 */
async function usageConflict(billing: SupabaseClient): Promise<{
  conflictTarget: string;
  stamp: (row: Record<string, any>) => Record<string, any>;
}> {
  const scoped = await hasProviderColumn(billing, 'call_usage');
  if (!scoped) return { conflictTarget: 'dograh_run_id', stamp: (row) => row };
  const provider = callProvider();
  return {
    conflictTarget: 'provider,dograh_run_id',
    stamp: (row) => ({ ...row, provider }),
  };
}

/**
 * Dograh disposition -> the outcome vocabulary billing understands.
 *
 * Shares normaliseOutcome() with the dashboard on purpose. These were two
 * separate ladders — one substring-based here, one exact-match there — so the
 * same disposition could be billed as a connected call while the dashboard
 * showed it as a no-answer, or the reverse. One classifier, one verdict.
 */
function deriveOutcome(run: any): string {
  const disposition = String(
    run?.gathered_context?.mapped_call_disposition ??
    run?.gathered_context?.call_disposition ??
    '',
  ).toLowerCase();

  if (!disposition) return run?.is_completed ? 'completed' : 'unknown';
  return normaliseOutcome(disposition);
}

function durationOf(run: any): number {
  const raw =
    run?.cost_info?.call_duration_seconds ??
    run?.usage_info?.call_duration_seconds ??
    run?.duration;
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? Math.round(n) : 0;
}

export interface MeterResult {
  campaignsScanned: number;
  runsSeen: number;
  inserted: number;
  alreadyKnown: number;
  errors: number;
  /** Runs skipped because the call had not finished yet; metered on a later tick. */
  inFlight: number;
}

/**
 * Pull every run for one campaign into call_usage.
 *
 * Stops early once a page yields nothing new AND we are past `sinceIso`, so the
 * routine stays cheap when run every few minutes against a long history.
 */
export async function meterCampaign(
  billing: SupabaseClient,
  campaignId: number,
  opts: { sinceIso?: string; maxPages?: number; pageSize?: number; accountId?: string } = {},
): Promise<Omit<MeterResult, 'campaignsScanned'>> {
  const { sinceIso, maxPages = 20, pageSize = 100, accountId = 'default' } = opts;
  const out = { runsSeen: 0, inserted: 0, alreadyKnown: 0, errors: 0, inFlight: 0 };

  for (let page = 1; page <= maxPages; page += 1) {
    let runs: any[] = [];
    try {
      const res = await dograh.getCampaignRuns(campaignId, page, pageSize);
      runs = res.runs ?? [];
    } catch (err: any) {
      console.error('[meter] could not fetch runs', { campaignId, page, error: err?.message });
      out.errors += 1;
      break;
    }
    if (runs.length === 0) break;

    const rows = runs
      .filter((run) => Number.isFinite(Number(run?.id)))
      // A call still ringing or still talking must NOT be metered yet -- see
      // isMeterable. meterSingleRun already refused this case; this path did not.
      .filter((run) => {
        if (isMeterable(run)) return true;
        out.inFlight += 1;
        return false;
      })
      .map((run) => ({
        account_id: accountId,
        dograh_run_id: Number(run.id),
        campaign_id: Number(run?.initial_context?.campaign_id ?? campaignId) || campaignId,
        duration_seconds: durationOf(run),
        call_mode: run?.mode ?? run?.initial_context?.provider ?? null,
        outcome: deriveOutcome(run),
        phone_hash: hashPhone(run?.initial_context?.phone_number ?? run?.phone_number),
        usage_info: run?.usage_info ?? null,
        cost_info: run?.cost_info ?? null,
        called_at: run?.created_at ?? new Date().toISOString(),
      }));

    out.runsSeen += rows.length;

    if (rows.length > 0) {
      // Must match whichever unique index the billing database actually has,
      // or PostgREST resolves the upsert against the wrong one.
      const { conflictTarget, stamp } = await usageConflict(billing);
      // ignoreDuplicates keeps this a pure insert-if-absent: a run we have
      // already metered (and possibly already charged for) is never rewritten,
      // so a re-run cannot alter the basis of a charge that has been issued.
      const { data, error } = await billing
        .from('call_usage')
        .upsert(rows.map(stamp), { onConflict: conflictTarget, ignoreDuplicates: true })
        .select('id');

      if (error) {
        console.error('[meter] upsert failed', { campaignId, page, error: error.message });
        out.errors += 1;
      } else {
        const newRows = data?.length ?? 0;
        out.inserted += newRows;
        out.alreadyKnown += rows.length - newRows;
      }
    }

    if (runs.length < pageSize) break;

    // Everything on this page predates the window we care about, and runs come
    // back newest-first, so nothing older will be new either.
    if (sinceIso) {
      const oldest = rows[rows.length - 1]?.called_at;
      if (oldest && oldest < sinceIso) break;
    }
  }

  return out;
}

/**
 * Meter ONE run, by id — the path taken the moment a call ends.
 *
 * The webhook fires within seconds of hang-up, but the balance used to sit
 * still until the next cron tick, so the client could watch a campaign run for
 * ten minutes with the number frozen and reasonably conclude the meter was
 * broken. This puts the charge on the ledger immediately.
 *
 * It still reads the run FROM DOGRAH rather than trusting the webhook body: the
 * body is a Jinja-rendered template where every value arrives as a string and a
 * missing one arrives as "None", and it does not carry `mode` at all — and mode
 * is exactly what decides whether a call is a billable phone call or a free
 * browser test. Charging from that payload would eventually bill a test call.
 *
 * Returns null when the run cannot be read; the cron sweep then bills it on the
 * next tick. Nothing here is allowed to throw — a billing hiccup must never
 * cost us the call record.
 */
export async function meterSingleRun(
  billing: SupabaseClient,
  dograhRunId: number,
  opts: { workflowId?: number; accountId?: string } = {},
): Promise<{
  id?: string;
  dograh_run_id: number;
  duration_seconds: number;
  call_mode: string | null;
  outcome: string;
  usage_info: any;
  campaign_id: number | null;
} | null> {
  const workflowId = opts.workflowId ?? Number(process.env.DOGRAH_WORKFLOW_ID ?? 1);
  const accountId = opts.accountId ?? 'default';

  try {
    const run = await dograh.getWorkflowRun(workflowId, dograhRunId);
    if (!run || !Number.isFinite(Number(run.id))) return null;

    const duration = durationOf(run);
    const mode = run?.mode ?? run?.initial_context?.provider ?? null;

    // A run read seconds after hang-up can still be missing its duration or its
    // mode. Inserting THAT would be permanent damage, not a delay: rows are
    // written insert-if-absent, so a row stored with zero duration is never
    // corrected by a later pass, and postDebit would settle it at zero and stop
    // the sweep from ever reconsidering it. Leaving it entirely alone costs at
    // most one cron tick, by which point Dograh has finalised the run.
    if (!mode || duration <= 0) return null;

    const row = {
      account_id: accountId,
      dograh_run_id: Number(run.id),
      campaign_id: Number(run?.initial_context?.campaign_id) || null,
      duration_seconds: duration,
      call_mode: mode,
      outcome: deriveOutcome(run),
      phone_hash: hashPhone(run?.initial_context?.phone_number ?? run?.phone_number),
      usage_info: run?.usage_info ?? null,
      cost_info: run?.cost_info ?? null,
      called_at: run?.created_at ?? new Date().toISOString(),
    };

    // Same insert-if-absent contract as meterCampaign: a run we have already
    // metered is never rewritten, so this cannot alter the basis of a charge
    // that has already been issued.
    const { conflictTarget, stamp } = await usageConflict(billing);
    const { error } = await billing
      .from('call_usage')
      .upsert([stamp(row)], { onConflict: conflictTarget, ignoreDuplicates: true });

    if (error) {
      console.error('[meter] single-run upsert failed', { run: dograhRunId, error: error.message });
      return null;
    }

    // Read back for the row id — whether we just inserted it or it was already
    // there — because postDebit stamps billed_at on it by id.
    // Scoped the same way as the write. Unscoped, this reads back the OLD
    // backend's row for a colliding id and postDebit would then stamp billed_at
    // on a call from August instead of the one that just happened. maybeSingle
    // would also error outright once both rows exist.
    // Selecting `provider` before 010 is applied would fail the whole read, so
    // both the column list and the filter follow what the database has.
    const scoped = await hasProviderColumn(billing, 'call_usage');
    const columns =
      'id, dograh_run_id, duration_seconds, call_mode, outcome, usage_info, campaign_id' +
      (scoped ? ', provider' : '');
    let readBack = billing.from('call_usage').select(columns).eq('dograh_run_id', dograhRunId);
    if (scoped) readBack = readBack.eq('provider', callProvider());
    const { data: stored } = await readBack.maybeSingle();

    return (stored as any) ?? { ...row, id: undefined };
  } catch (err: any) {
    console.error('[meter] single-run failed', { run: dograhRunId, error: err?.message });
    return null;
  }
}

/** Pull runs for every campaign Dograh knows about. */
export async function meterAllCampaigns(
  billing: SupabaseClient,
  opts: { sinceIso?: string; accountId?: string } = {},
): Promise<MeterResult> {
  const total: MeterResult = {
    campaignsScanned: 0, runsSeen: 0, inserted: 0, alreadyKnown: 0, errors: 0, inFlight: 0,
  };

  let campaigns: any[] = [];
  try {
    campaigns = await dograh.listCampaigns();
  } catch (err: any) {
    console.error('[meter] could not list campaigns', err?.message);
    total.errors += 1;
    return total;
  }

  for (const campaign of campaigns) {
    const id = Number(campaign?.id);
    if (!Number.isFinite(id)) continue;
    total.campaignsScanned += 1;
    const one = await meterCampaign(billing, id, opts);
    total.runsSeen += one.runsSeen;
    total.inserted += one.inserted;
    total.alreadyKnown += one.alreadyKnown;
    total.errors += one.errors;
  }

  return total;
}

/**
 * Backfill usage_info for metered calls that arrived without it.
 *
 * The campaign-runs list returns usage_info: null, so the token and character
 * counts that provider cost is derived from only appear on a per-run fetch.
 * That is one HTTP call per run, so it is bounded per tick and runs in the
 * background — it feeds the operator margin view only and never blocks billing.
 */
export async function enrichUsageInfo(
  billing: SupabaseClient,
  opts: { workflowId?: number; limit?: number } = {},
): Promise<{ attempted: number; enriched: number; failed: number }> {
  const workflowId = opts.workflowId ?? Number(process.env.DOGRAH_WORKFLOW_ID ?? 1);
  const limit = opts.limit ?? 25;
  const out = { attempted: 0, enriched: 0, failed: 0 };

  const { data, error } = await billing
    .from('call_usage')
    .select('id, dograh_run_id')
    .is('usage_info', null)
    .gt('duration_seconds', 0)
    .order('called_at', { ascending: false })
    .limit(limit);

  if (error) {
    console.error('[meter] could not list rows needing enrichment', error.message);
    return out;
  }

  for (const row of data ?? []) {
    out.attempted += 1;
    try {
      const run = await dograh.getWorkflowRun(workflowId, row.dograh_run_id);
      if (!run?.usage_info) { out.failed += 1; continue; }
      const { error: updateError } = await billing
        .from('call_usage')
        .update({ usage_info: run.usage_info })
        .eq('id', row.id);
      if (updateError) { out.failed += 1; continue; }
      out.enriched += 1;
    } catch (err: any) {
      console.error('[meter] enrich failed', { run: row.dograh_run_id, error: err?.message });
      out.failed += 1;
    }
  }

  return out;
}
