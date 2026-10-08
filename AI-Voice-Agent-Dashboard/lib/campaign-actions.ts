import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { DograhApiError, DograhClient, DograhConfigError, dograh } from '@/lib/dograh';
import { evaluateTransition } from '@/lib/campaign-state';

type Action = 'pause' | 'resume' | 'cancel';

/**
 * Shared implementation for the pause/resume routes.
 *
 * Fixes several problems the duplicated route handlers had:
 *  - no state validation (could pause a completed campaign)
 *  - no guard for a null dograh_campaign_id (produced /campaign/null/pause)
 *  - non-idempotent (pausing twice threw a 409 from Dograh and surfaced as a 500)
 *  - Dograh succeeded but DB write failed -> silent state divergence
 *  - raw internal error strings returned to the browser
 */
export async function handleCampaignAction(action: Action, campaignId: string) {
  if (!DograhClient.isConfigured()) {
    return NextResponse.json(
      { error: 'Calling provider is not configured. Set DOGRAH_API_KEY to enable campaign controls.' },
      { status: 503 },
    );
  }

  const supabase = createServerClient();

  const { data: campaign, error } = await supabase
    .from('campaign_runs')
    .select('id, status, dograh_campaign_id, paused_at')
    .eq('id', campaignId)
    .maybeSingle();

  if (error) {
    console.error(`[campaign:${action}] lookup failed`, error);
    return NextResponse.json({ error: 'Failed to load campaign.' }, { status: 500 });
  }
  if (!campaign) {
    return NextResponse.json({ error: 'Campaign not found' }, { status: 404 });
  }

  const transition = evaluateTransition(action, campaign.status);

  if (!transition.ok) {
    // 409 Conflict is the correct signal for "valid request, wrong state".
    return NextResponse.json({ error: transition.reason, status: campaign.status }, { status: 409 });
  }

  // Already in the desired state - return success so repeated clicks are harmless.
  if (transition.noop) {
    return NextResponse.json({ campaign, noop: true });
  }

  if (campaign.dograh_campaign_id == null) {
    return NextResponse.json(
      { error: 'This campaign has no provider campaign attached, so it cannot be controlled.' },
      { status: 409 },
    );
  }

  const providerId = Number(campaign.dograh_campaign_id);
  if (!Number.isFinite(providerId)) {
    return NextResponse.json({ error: 'Campaign has an invalid provider id.' }, { status: 422 });
  }

  try {
    if (action === 'pause') await dograh.pauseCampaign(providerId);
    else if (action === 'resume') await dograh.resumeCampaign(providerId);
  } catch (err) {
    if (err instanceof DograhConfigError) {
      return NextResponse.json({ error: err.message }, { status: 503 });
    }
    if (err instanceof DograhApiError) {
      // Provider already in the target state: converge our DB rather than failing.
      if (err.status === 409) {
        await supabase
          .from('campaign_runs')
          .update({
            status: transition.nextStatus,
            paused_at: action === 'pause' ? new Date().toISOString() : null,
          })
          .eq('id', campaignId);
        return NextResponse.json({ campaign: { ...campaign, status: transition.nextStatus }, reconciled: true });
      }
      console.error(`[campaign:${action}] provider error`, err.status, err.body?.slice(0, 300));
      return NextResponse.json(
        { error: `Calling provider rejected the ${action} request.`, providerStatus: err.status },
        { status: err.isClientError ? 422 : 502 },
      );
    }
    console.error(`[campaign:${action}] unexpected provider failure`, err);
    return NextResponse.json({ error: `Failed to ${action} campaign.` }, { status: 502 });
  }

  const { data: updated, error: updateError } = await supabase
    .from('campaign_runs')
    .update({
      status: transition.nextStatus,
      paused_at: action === 'pause' ? new Date().toISOString() : null,
      updated_at: new Date().toISOString(),
    })
    .eq('id', campaignId)
    .select()
    .single();

  if (updateError) {
    // The provider action DID succeed. Surfacing this explicitly prevents the UI
    // from showing "running" while calls are actually paused.
    console.error(`[campaign:${action}] provider ok but DB write failed`, updateError);
    return NextResponse.json(
      {
        error: `Campaign was ${action}d at the provider, but saving the new state failed. The reconcile job will correct this shortly.`,
        providerActionApplied: true,
      },
      { status: 500 },
    );
  }

  return NextResponse.json({ campaign: updated });
}
