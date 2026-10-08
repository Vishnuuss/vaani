import { createClient, type SupabaseClient } from '@supabase/supabase-js';

/**
 * Connection to the BILLING database — a separate Supabase project that only we
 * control.
 *
 * Why this is not just another schema in the main project: the client owns that
 * project and has the dashboard login for it. Anything stored there, he can
 * edit. A credits table sitting next to his leads is one click away from being
 * topped up to a million, and no application code can stop the owner of a
 * database. So the credits simply are not there — there is no lock to pick,
 * because there is nothing to find.
 *
 * Rules for this client:
 *   - SERVER ONLY. The env vars are deliberately not NEXT_PUBLIC_ prefixed, so
 *     importing this into a client component fails at build rather than
 *     shipping the key to a browser.
 *   - Never used for lead/call/campaign data. That stays in supabase-server.ts.
 *   - Never joined to the client's database in SQL — cross-project joins are not
 *     possible. Ledger rows carry their own display text instead, which is
 *     better practice anyway: a bill should stay readable after the lead it
 *     refers to is deleted.
 */

let cached: SupabaseClient | null = null;

export function createBillingClient(): SupabaseClient {
  if (cached) return cached;

  const url = process.env.BILLING_SUPABASE_URL;
  const serviceKey = process.env.BILLING_SUPABASE_SERVICE_KEY;

  if (!url) {
    throw new Error('Missing env var BILLING_SUPABASE_URL');
  }
  if (!serviceKey) {
    throw new Error('Missing env var BILLING_SUPABASE_SERVICE_KEY');
  }

  cached = createClient(url, serviceKey, {
    auth: { persistSession: false, autoRefreshToken: false, detectSessionInUrl: false },
    // Tag the connection so billing traffic is identifiable in Supabase logs
    // when reconciling a disputed charge.
    global: { headers: { 'x-application-name': 'bswealth-billing' } },
  });

  return cached;
}

/**
 * True when billing is configured. Lets callers degrade gracefully — a missing
 * billing database must never take down the leads dashboard, which has to keep
 * working whether or not we are charging for it.
 */
export function isBillingConfigured(): boolean {
  return Boolean(process.env.BILLING_SUPABASE_URL && process.env.BILLING_SUPABASE_SERVICE_KEY);
}
