import { createBrowserClient as createSsrBrowserClient } from '@supabase/ssr';

/**
 * Browser-side Supabase client.
 *
 * MUST come from @supabase/ssr, not @supabase/supabase-js.
 *
 * The plain createClient() stores the session in localStorage, which the server
 * cannot see. middleware.ts authenticates with @supabase/ssr's createServerClient,
 * which reads the session from COOKIES. Pairing the two means a successful login
 * writes a session the middleware never finds: the user is redirected straight
 * back to /login, forever, with correct credentials. This client writes the
 * cookies the middleware reads, so the two halves agree.
 *
 * Auth only — all data goes through /api/* routes, which use the service role.
 */

let cached: ReturnType<typeof createSsrBrowserClient> | null = null;

export function createBrowserClient() {
  if (cached) return cached;

  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

  if (!supabaseUrl) {
    throw new Error('Missing env var NEXT_PUBLIC_SUPABASE_URL');
  }
  if (!supabaseAnonKey) {
    throw new Error('Missing env var NEXT_PUBLIC_SUPABASE_ANON_KEY');
  }

  cached = createSsrBrowserClient(supabaseUrl, supabaseAnonKey);
  return cached;
}
