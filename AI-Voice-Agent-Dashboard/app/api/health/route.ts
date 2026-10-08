import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { DograhApiError, DograhClient, dograh } from '@/lib/dograh';

/**
 * Real integration status for the dashboard badges, which used to be three
 * hard-coded "Connected" pills that stayed green even with no credentials set.
 *
 * Only booleans and states are returned - never a key, URL or error body.
 */

export const dynamic = 'force-dynamic';

type State = 'connected' | 'error' | 'not_configured';

async function checkSupabase(): Promise<{ state: State; detail: string }> {
  if (!process.env.NEXT_PUBLIC_SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
    return { state: 'not_configured', detail: 'Supabase env vars missing' };
  }
  try {
    const supabase = createServerClient();
    const { error } = await supabase.from('leads').select('id', { count: 'exact', head: true });
    if (error) return { state: 'error', detail: 'Query failed' };
    return { state: 'connected', detail: 'Reachable' };
  } catch {
    return { state: 'error', detail: 'Client could not be created' };
  }
}

async function checkDograh(): Promise<{ state: State; detail: string }> {
  if (!DograhClient.isConfigured()) {
    return { state: 'not_configured', detail: 'DOGRAH_API_KEY not set' };
  }
  if (!Number.isFinite(Number.parseInt(process.env.DOGRAH_WORKFLOW_ID ?? '', 10))) {
    return { state: 'error', detail: 'DOGRAH_WORKFLOW_ID missing — campaigns cannot launch' };
  }

  // A key being *present* proves nothing: an expired key still passes that check
  // while every campaign action, progress sync and reconcile sweep fails with 401.
  // Actually call the API so the dashboard reports the truth.
  try {
    await dograh.listCampaigns();
    return { state: 'connected', detail: 'API key valid' };
  } catch (err) {
    if (err instanceof DograhApiError && (err.status === 401 || err.status === 403)) {
      return { state: 'error', detail: 'DOGRAH_API_KEY is invalid or expired — calls cannot be placed or synced' };
    }
    return { state: 'error', detail: 'Dograh API is unreachable right now' };
  }
}

export async function GET() {
  const [supabase, dograh] = await Promise.all([checkSupabase(), checkDograh()]);

  const webhook: { state: State; detail: string } = process.env.DOGRAH_WEBHOOK_SECRET
    ? { state: 'connected', detail: 'Secret configured — deliveries accepted' }
    : {
        state: 'not_configured',
        // This is the single most common reason the dashboard stops updating:
        // without the secret the endpoint fails closed with 503 on every call.
        detail: 'DOGRAH_WEBHOOK_SECRET not set — call results are being rejected',
      };

  const n8n: { state: State; detail: string } = process.env.N8N_IMPORT_WEBHOOK_URL
    ? { state: 'connected', detail: 'Import webhook configured' }
    : { state: 'not_configured', detail: 'N8N_IMPORT_WEBHOOK_URL not set — using fallback URL' };

  const cron: { state: State; detail: string } = process.env.CRON_SECRET
    ? { state: 'connected', detail: 'Reconcile job authorised' }
    : { state: 'not_configured', detail: 'CRON_SECRET not set — reconcile sweep is rejected' };

  // Surfaced as a service so the dashboard shows a standing reminder that the
  // deployment is unauthenticated, rather than it being invisible in an env var.
  const auth: { state: State; detail: string } =
    process.env.SKIP_AUTH === 'true'
      ? { state: 'error', detail: 'SKIP_AUTH=true — anyone with this URL can view leads and start campaigns' }
      : { state: 'connected', detail: 'Login required' };

  const services = { supabase, dograh, webhook, n8n, cron, auth };
  const healthy = Object.values(services).every((s) => s.state === 'connected');

  return NextResponse.json({ healthy, services, checked_at: new Date().toISOString() });
}
