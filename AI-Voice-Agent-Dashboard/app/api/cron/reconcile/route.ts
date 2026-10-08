import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { getMaxRetries } from '@/lib/call-behavior';
import { DograhClient, dograh } from '@/lib/dograh';
import { applyRunResult, isAuthorisedCron, repairIncompleteCallLogs } from '@/lib/reconcile';
import { isTerminal, mapDograhStatus } from '@/lib/campaign-state';
import { createBillingClient, isBillingConfigured } from '@/lib/supabase-billing';
import { getBillingConfig, sweepUnbilledCalls } from '@/lib/billing';
import { meterAllCampaigns, enrichUsageInfo } from '@/lib/billing-meter';
import { enforceBalanceGuard } from '@/lib/billing-guard';
import { purgeExpired } from '@/lib/trash';

/**
 * Reconciles our database against Dograh, recovering anything a missed webhook lost.
 *
 * Runs on a schedule (see vercel.json). Vercel Cron issues GET requests, so both
 * GET and POST are exported - the previous POST-only handler was never invoked by
 * the scheduler.
 */

export const maxDuration = 60;

const PAGE_SIZE = 100;
const MAX_PAGES = 20;
/** A lead dialling for longer than this is considered stuck and is released. */
const STUCK_LEAD_MINUTES = 60;

async function reconcileCampaign(
  supabase: ReturnType<typeof createServerClient>,
  campaign: any,
  maxRetries: number,
) {
  const providerId = Number(campaign.dograh_campaign_id);
  // `backfilled` counts calls whose row already existed but was written
  // before the provider finalised it - a zero duration or a missing
  // recording, repaired on this tick.
  const stats = { inserted: 0, duplicate: 0, backfilled: 0, no_lead: 0, error: 0 };

  if (!Number.isFinite(providerId)) return stats;

  // Walk every page. The old version only ever fetched page 1 (100 runs), so any
  // campaign larger than that was permanently under-reconciled.
  for (let page = 1; page <= MAX_PAGES; page++) {
    const { runs, total } = await dograh.getCampaignRuns(providerId, page, PAGE_SIZE);
    if (!runs.length) break;

    for (const run of runs) {
      const outcome = await applyRunResult(supabase, run as any, campaign.id, maxRetries);
      stats[outcome] += 1;
    }

    if (runs.length < PAGE_SIZE || page * PAGE_SIZE >= (total || 0)) break;
  }

  // Sync the campaign's own status.
  try {
    const progress = await dograh.getCampaignProgress(providerId);
    const mapped = mapDograhStatus(progress?.state);
    if (mapped && mapped !== campaign.status) {
      const patch: Record<string, unknown> = { status: mapped, updated_at: new Date().toISOString() };
      if (isTerminal(mapped)) patch.completed_at = new Date().toISOString();
      if (mapped === 'paused') patch.paused_at = new Date().toISOString();
      await supabase.from('campaign_runs').update(patch).eq('id', campaign.id);
    }
  } catch (err) {
    console.warn(`[cron:reconcile] progress sync failed for ${campaign.id}`, err);
  }

  return stats;
}

/** Frees leads left in `queued` by a crashed launch so they can be dialled again. */
async function releaseStuckLeads(supabase: ReturnType<typeof createServerClient>) {
  const cutoff = new Date(Date.now() - STUCK_LEAD_MINUTES * 60_000).toISOString();

  const { data, error } = await supabase
    .from('leads')
    .update({ status: 'new', campaign_run_id: null })
    .eq('status', 'queued')
    .lt('updated_at', cutoff)
    .select('id');

  if (error) {
    console.warn('[cron:reconcile] stuck lead sweep failed', error);
    return 0;
  }
  return data?.length ?? 0;
}

async function handler(request: Request) {
  if (!isAuthorisedCron(request)) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }
  if (!DograhClient.isConfigured()) {
    return NextResponse.json({ error: 'Calling provider is not configured.' }, { status: 503 });
  }

  try {
    const supabase = createServerClient();

    // Include queued and paused campaigns: results can arrive for calls that were
    // already in flight when a campaign was paused.
    const { data: campaigns, error } = await supabase
      .from('campaign_runs')
      .select('id, status, dograh_campaign_id')
      .in('status', ['queued', 'running', 'paused'])
      .not('dograh_campaign_id', 'is', null);

    if (error) {
      console.error('[cron:reconcile] failed to load campaigns', error);
      return NextResponse.json({ error: 'Failed to load campaigns' }, { status: 500 });
    }

    const totals = { inserted: 0, duplicate: 0, backfilled: 0, no_lead: 0, error: 0 };
    const maxRetries = await getMaxRetries(supabase);

    for (const campaign of campaigns ?? []) {
      try {
        const stats = await reconcileCampaign(supabase, campaign, maxRetries);
        for (const key of Object.keys(totals) as (keyof typeof totals)[]) totals[key] += stats[key];
      } catch (err) {
        console.error(`[cron:reconcile] campaign ${campaign.id} failed`, err);
        totals.error += 1;
      }
    }

    // --- Repair pass, independent of campaign status ------------------------
    // The loop above only visits campaigns that are still queued/running/paused.
    // 101 of 113 campaign_runs are `completed`, and a completed campaign is never
    // looked at again - so a row written thin by the webhook under a campaign
    // that has since finished stays thin for ever. That is how run 798 kept
    // duration 0 and a score of 0 for a 70-second call that qualified at 100.
    //
    // This finds incomplete rows directly and repairs them from their run,
    // whatever their campaign is doing. Bounded so a long history cannot make a
    // tick run away.
    const repaired = await repairIncompleteCallLogs(supabase);

    const releasedLeads = await releaseStuckLeads(supabase);

    // --- Billing ------------------------------------------------------------
    // Meter from Dograh (not from call_logs — the client can edit that), charge
    // anything unbilled, then stop the calling if the balance has run out.
    //
    // Every step is non-fatal: reconciliation is the job that keeps call data
    // correct, and it must not start failing because our accounting had a bad
    // minute. The sweep is idempotent, so anything skipped is picked up next tick.
    const billingReport: Record<string, unknown> = {};
    if (isBillingConfigured()) {
      try {
        const billing = createBillingClient();
        const config = await getBillingConfig(billing);

        const metered = await meterAllCampaigns(billing, {
          sinceIso: new Date(Date.now() - 7 * 86_400_000).toISOString(),
        });
        const swept = await sweepUnbilledCalls(billing, config);

        // Re-read: the sweep just moved the balance.
        const after = await getBillingConfig(billing);
        const guard = await enforceBalanceGuard(supabase, after, after.balanceMilli);

        // Bounded per tick — this is one HTTP call per run and only feeds the
        // operator margin view, so it must never dominate the job.
        const enriched = await enrichUsageInfo(billing, { limit: 15 });

        billingReport.metered = metered.inserted;
        billingReport.billed = swept.billed;
        billingReport.credits_charged = swept.creditsCharged;
        billingReport.balance_credits = Math.round(after.balanceMilli / 1000);
        billingReport.campaigns_paused = guard.paused.length;
        billingReport.usage_enriched = enriched.enriched;
      } catch (billingError: any) {
        console.error('[cron:reconcile] billing step failed', billingError?.message);
        billingReport.error = 'billing step failed';
      }
    }

    // --- Recycle Bin retention ----------------------------------------------
    // The 7-day window rides on this job rather than getting a schedule of its
    // own: this already runs every ten minutes (vercel.json), and a second cron
    // entry would be one more thing to configure correctly on every deploy.
    //
    // Non-fatal for the same reason billing is. If the trash tables do not exist
    // yet — migration 008 not run — this logs and the reconcile keeps working.
    let trashPurged = 0;
    try {
      trashPurged = await purgeExpired(supabase);
    } catch (trashError: any) {
      console.error('[cron:reconcile] trash purge failed', trashError?.message);
    }

    return NextResponse.json({
      success: true,
      campaigns_checked: campaigns?.length ?? 0,
      ...totals,
      repaired_call_logs: repaired,
      released_stuck_leads: releasedLeads,
      trash_batches_purged: trashPurged,
      billing: billingReport,
    });
  } catch (error) {
    console.error('[cron:reconcile] unexpected', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}

export const GET = handler;
export const POST = handler;
