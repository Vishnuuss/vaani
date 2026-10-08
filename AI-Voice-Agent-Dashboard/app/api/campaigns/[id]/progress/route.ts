import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { DograhApiError, DograhClient, DograhConfigError, dograh } from '@/lib/dograh';

/** Live progress for one campaign. Mirrors the error handling of the runs route. */
export async function GET(request: Request, { params }: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await params;
    const supabase = createServerClient();

    const { data: campaign, error } = await supabase
      .from('campaign_runs')
      .select('id, dograh_campaign_id, status')
      .eq('id', id)
      .maybeSingle();

    if (error) {
      console.error('[campaigns/progress] lookup failed', error);
      return NextResponse.json({ error: 'Failed to load the campaign.' }, { status: 500 });
    }
    if (!campaign) {
      return NextResponse.json({ error: 'Campaign not found' }, { status: 404 });
    }

    if (!DograhClient.isConfigured()) {
      return NextResponse.json({ error: 'Calling provider is not configured.' }, { status: 503 });
    }

    const providerId = Number(campaign.dograh_campaign_id);
    if (!Number.isFinite(providerId)) {
      return NextResponse.json(
        { error: 'This campaign never started at the provider, so it has no progress.' },
        { status: 409 },
      );
    }

    const progress = await dograh.getCampaignProgress(providerId);
    return NextResponse.json({ progress });
  } catch (error: any) {
    if (error instanceof DograhConfigError) {
      return NextResponse.json({ error: error.message }, { status: 503 });
    }
    if (error instanceof DograhApiError) {
      if (error.status === 401 || error.status === 403) {
        return NextResponse.json(
          { error: 'The Dograh API key is invalid or expired, so progress cannot be read.' },
          { status: 502 },
        );
      }
      return NextResponse.json(
        { error: 'The calling provider could not return progress.', providerStatus: error.status },
        { status: 502 },
      );
    }
    console.error('[campaigns/progress] unexpected', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
