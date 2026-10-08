import { createClient } from '@supabase/supabase-js';

/**
 * Does this error mean "that table does not exist yet"?
 *
 * PostgREST does NOT surface the Postgres code for an unknown relation (42P01).
 * It resolves table names against its own schema cache and returns PGRST205
 * with a "Could not find the table" message instead. Checking for 42P01 looks
 * right and never matches, which is how the Recycle Bin came back as a blank
 * 500 on a deployment that had simply not run migration 008 yet.
 *
 * Both codes are accepted: 42P01 can still arrive from a raw RPC path.
 */
export function isMissingTableError(error: { code?: string | null } | null | undefined): boolean {
  return error?.code === 'PGRST205' || error?.code === '42P01';
}

export function createServerClient() {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY;

  if (!supabaseUrl) {
    throw new Error('Missing env var NEXT_PUBLIC_SUPABASE_URL');
  }
  if (!supabaseServiceKey) {
    throw new Error('Missing env var SUPABASE_SERVICE_ROLE_KEY');
  }

  return createClient(supabaseUrl, supabaseServiceKey, {
    auth: {
      persistSession: false,
      autoRefreshToken: false,
      detectSessionInUrl: false
    }
  });
}
