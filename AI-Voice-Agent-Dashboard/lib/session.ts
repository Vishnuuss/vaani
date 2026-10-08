/**
 * Who is making this request, for audit fields only.
 *
 * The middleware already decides whether a request is allowed through; nothing
 * here is a security check. It exists so the Recycle Bin can say WHO deleted
 * 4,000 leads, which is the first question anyone asks when rows go missing.
 *
 * Returns null rather than throwing when there is no session — SKIP_AUTH is
 * still honoured in this deployment, and an audit field is never a reason to
 * fail an operation the user is otherwise entitled to perform.
 */

import { cookies } from 'next/headers';
import { createServerClient } from '@supabase/ssr';

export async function currentUserEmail(): Promise<string | null> {
  try {
    const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
    const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
    if (!url || !anonKey) return null;

    const store = await cookies();
    const supabase = createServerClient(url, anonKey, {
      cookies: {
        get: (name: string) => store.get(name)?.value,
        // Route handlers cannot always write cookies, and we are only reading.
        set: () => {},
        remove: () => {},
      },
    });

    const {
      data: { user },
    } = await supabase.auth.getUser();
    return user?.email ?? null;
  } catch {
    return null;
  }
}
