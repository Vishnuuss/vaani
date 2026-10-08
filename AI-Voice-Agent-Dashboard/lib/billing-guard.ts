import type { SupabaseClient } from '@supabase/supabase-js';
import { dograh } from './dograh';
import type { BillingConfig } from './billing';

/**
 * Stops the calling when the credits run out.
 *
 * The threshold sits ABOVE zero on purpose. Dograh dials up to max_concurrency
 * calls in parallel, so several are always already ringing by the time a pause
 * lands — pausing at exactly 0 would guarantee an overdraft on every campaign.
 * The buffer absorbs the calls that are already in flight.
 *
 * Takes the client's Supabase (where campaigns live) and the billing config
 * separately, because the balance and the campaigns are in different databases.
 */
export async function enforceBalanceGuard(
  clientDb: SupabaseClient,
  config: BillingConfig,
  balanceMilli: number,
): Promise<{ paused: number[]; triggered: boolean }> {
  const result = { paused: [] as number[], triggered: false };

  if (!config.autoPauseEnabled) return result;
  if (balanceMilli > config.autoPauseAtMilli) return result;

  result.triggered = true;

  try {
    const { data: active, error } = await clientDb
      .from('campaign_runs')
      .select('id, dograh_campaign_id, status')
      .in('status', ['queued', 'running'])
      .not('dograh_campaign_id', 'is', null);

    if (error) {
      console.error('[billing-guard] could not list active campaigns', error.message);
      return result;
    }
    if (!active || active.length === 0) return result;

    for (const campaign of active) {
      const dograhId = Number(campaign.dograh_campaign_id);
      if (!Number.isFinite(dograhId)) continue;

      try {
        await dograh.pauseCampaign(dograhId);
        await clientDb
          .from('campaign_runs')
          .update({
            status: 'paused',
            paused_at: new Date().toISOString(),
            // Surfaced by the dashboard's existing notification derivation, so
            // the client is told WHY rather than finding calling silently dead.
            error_message: 'Paused automatically — credit balance exhausted. Add credits to resume.',
          })
          .eq('id', campaign.id);

        result.paused.push(dograhId);
        console.warn('[billing-guard] paused campaign for low balance', {
          campaign: dograhId,
          balance_milli: balanceMilli,
        });
      } catch (pauseError: any) {
        // Dograh 409s when a campaign is already paused or finished, which is
        // the outcome we wanted anyway.
        console.error('[billing-guard] pause failed', { campaign: dograhId, error: pauseError?.message });
      }
    }
  } catch (err: any) {
    console.error('[billing-guard] guard threw', err?.message);
  }

  return result;
}
