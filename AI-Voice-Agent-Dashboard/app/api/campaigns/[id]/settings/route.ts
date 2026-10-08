import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { DograhApiError, DograhClient, DograhConfigError, dograh } from '@/lib/dograh';
import { isTerminal } from '@/lib/campaign-state';
import { getMaxRetries } from '@/lib/call-behavior';

/**
 * Adjusts concurrency / retry behaviour on a campaign that has already
 * launched. Dograh accepts this on any non-terminal campaign (queued,
 * running, paused) - verified against its route handler, which only rejects
 * completed/failed ones.
 */
export async function PATCH(request: Request, { params }: { params: Promise<{ id: string }> }) {
  try {
    if (!DograhClient.isConfigured()) {
      return NextResponse.json({ error: 'Calling provider is not configured.' }, { status: 503 });
    }

    const { id } = await params;
    const body = (await request.json().catch(() => null)) as Record<string, any> | null;
    if (!body || typeof body !== 'object') {
      return NextResponse.json({ error: 'Invalid JSON body' }, { status: 400 });
    }

    const supabase = createServerClient();
    const { data: campaign, error } = await supabase
      .from('campaign_runs')
      .select('id, status, dograh_campaign_id')
      .eq('id', id)
      .maybeSingle();

    if (error) {
      console.error('[campaigns/settings] lookup failed', error);
      return NextResponse.json({ error: 'Failed to load campaign.' }, { status: 500 });
    }
    if (!campaign) {
      return NextResponse.json({ error: 'Campaign not found' }, { status: 404 });
    }
    if (isTerminal(campaign.status)) {
      return NextResponse.json({ error: `Cannot change settings on a ${campaign.status} campaign.` }, { status: 409 });
    }
    if (campaign.dograh_campaign_id == null) {
      return NextResponse.json(
        { error: 'This campaign has no provider campaign attached, so it has no settings to change.' },
        { status: 409 },
      );
    }
    const providerId = Number(campaign.dograh_campaign_id);
    if (!Number.isFinite(providerId)) {
      return NextResponse.json({ error: 'Campaign has an invalid provider id.' }, { status: 422 });
    }

    // Same bounds Dograh itself enforces (RetryConfigRequest / max_concurrency
    // in its campaign.py) - validated here too so a bad value gets a clear
    // error instead of an opaque provider rejection.
    const providerSettings: Record<string, any> = {};
    const dbUpdate: Record<string, any> = {};

    if (body.concurrency !== undefined) {
      const concurrency = Number(body.concurrency);
      if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 100) {
        return NextResponse.json({ error: 'concurrency must be an integer between 1 and 100' }, { status: 400 });
      }
      providerSettings.max_concurrency = concurrency;
      dbUpdate.concurrency = concurrency;
    }

    if (body.retry_config !== undefined) {
      const rc = body.retry_config;
      if (typeof rc !== 'object' || rc === null) {
        return NextResponse.json({ error: 'retry_config must be an object' }, { status: 400 });
      }
      if (rc.max_retries !== undefined) {
        const n = Number(rc.max_retries);
        if (!Number.isInteger(n) || n < 0 || n > 10) {
          return NextResponse.json({ error: 'retry_config.max_retries must be an integer between 0 and 10' }, { status: 400 });
        }
        // The same ceiling the launch route applies. Without it, editing a
        // running campaign was a way to reinstate the extra dials the launch had
        // just capped - the provider counts max_retries as calls AFTER the
        // first, so the budget here is maxRetries - 1.
        const maxRetries = await getMaxRetries(supabase);
        const providerBudget = Math.max(0, maxRetries - 1);
        if (n > providerBudget) {
          return NextResponse.json(
            {
              error:
                `Retries within a campaign cannot exceed ${providerBudget}, because "Max retries" on the ` +
                `AI Agent page limits each person to ${maxRetries} call${maxRetries === 1 ? '' : 's'} in total ` +
                `(the first call plus ${providerBudget} retr${providerBudget === 1 ? 'y' : 'ies'}). ` +
                `Raise that setting first if you want more.`,
            },
            { status: 400 },
          );
        }
      }
      if (rc.retry_delay_seconds !== undefined) {
        const n = Number(rc.retry_delay_seconds);
        if (!Number.isInteger(n) || n < 30 || n > 3600) {
          return NextResponse.json(
            { error: 'retry_config.retry_delay_seconds must be an integer between 30 and 3600' },
            { status: 400 },
          );
        }
      }
      providerSettings.retry_config = rc;
      dbUpdate.retry_config = rc;
    }

    if (Object.keys(providerSettings).length === 0) {
      return NextResponse.json({ error: 'No settings provided. Send concurrency and/or retry_config.' }, { status: 400 });
    }

    const updated = await dograh.updateCampaignSettings(providerId, providerSettings);

    if (Object.keys(dbUpdate).length > 0) {
      dbUpdate.updated_at = new Date().toISOString();
      const { error: dbError } = await supabase.from('campaign_runs').update(dbUpdate).eq('id', id);
      if (dbError) {
        // The provider change DID take effect; say so explicitly rather than
        // letting the dashboard show a now-stale value as current.
        console.error('[campaigns/settings] provider ok but DB write failed', dbError);
        return NextResponse.json(
          {
            error: 'Settings were changed at the provider, but saving that locally failed. Refresh to see the live value.',
            providerActionApplied: true,
          },
          { status: 500 },
        );
      }
    }

    return NextResponse.json({ success: true, campaign: updated });
  } catch (error: any) {
    if (error instanceof DograhConfigError) {
      return NextResponse.json({ error: error.message }, { status: 503 });
    }
    if (error instanceof DograhApiError) {
      console.error('[campaigns/settings] provider error', error.status, error.body?.slice(0, 300));
      return NextResponse.json(
        { error: 'The calling provider rejected the settings change.', providerStatus: error.status },
        { status: error.isClientError ? 422 : 502 },
      );
    }
    console.error('[campaigns/settings] unexpected', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
