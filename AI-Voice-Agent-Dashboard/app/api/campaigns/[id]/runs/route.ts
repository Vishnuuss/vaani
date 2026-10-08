import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { DograhApiError, DograhClient, DograhConfigError, dograh } from '@/lib/dograh';

/**
 * Per-call detail for one campaign, read live from Dograh.
 *
 * Every failure here used to collapse into a bare 500 with "Internal Server
 * Error", so an expired API key looked identical to a bug and the UI just showed
 * an empty details panel.
 */
export async function GET(request: Request, { params }: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await params;
    const { searchParams } = new URL(request.url);
    const page = Math.max(parseInt(searchParams.get('page') || '1', 10) || 1, 1);
    const limit = Math.min(Math.max(parseInt(searchParams.get('limit') || '20', 10) || 20, 1), 100);

    const supabase = createServerClient();

    const { data: campaign, error } = await supabase
      .from('campaign_runs')
      .select('id, dograh_campaign_id, status')
      .eq('id', id)
      .maybeSingle();

    if (error) {
      console.error('[campaigns/runs] lookup failed', error);
      return NextResponse.json({ error: 'Failed to load the campaign.' }, { status: 500 });
    }
    if (!campaign) {
      return NextResponse.json({ error: 'Campaign not found' }, { status: 404 });
    }

    if (!DograhClient.isConfigured()) {
      return NextResponse.json(
        { error: 'Calling provider is not configured, so call details cannot be loaded.' },
        { status: 503 },
      );
    }

    // A campaign that never reached the provider has no runs to fetch; asking for
    // /campaign/null/runs produced a confusing provider error.
    const providerId = Number(campaign.dograh_campaign_id);
    if (!Number.isFinite(providerId)) {
      return NextResponse.json({
        runs: [],
        total: 0,
        reason: 'This campaign never started at the provider, so it has no call records.',
      });
    }

    const { runs, total } = await dograh.getCampaignRuns(providerId, page, limit);
    return NextResponse.json({ runs, total, page, limit });
  } catch (error: any) {
    if (error instanceof DograhConfigError) {
      return NextResponse.json({ error: error.message }, { status: 503 });
    }
    if (error instanceof DograhApiError) {
      if (error.status === 401 || error.status === 403) {
        return NextResponse.json(
          { error: 'The Dograh API key is invalid or expired, so call details cannot be loaded.' },
          { status: 502 },
        );
      }
      console.error('[campaigns/runs] provider error', error.status, error.body?.slice(0, 300));
      return NextResponse.json(
        { error: 'The calling provider could not return call details.', providerStatus: error.status },
        { status: 502 },
      );
    }
    console.error('[campaigns/runs] unexpected', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
