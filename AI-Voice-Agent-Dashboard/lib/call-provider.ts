import type { SupabaseClient } from '@supabase/supabase-js';

/**
 * Which calling backend a run id belongs to.
 *
 * A run id is only unique WITHIN one backend. voice.bswealthfinance.com reached
 * run 2097; Vaani numbers from 1 and is currently around 400, so a thousand of
 * the ids it is about to issue are already in call_logs and call_usage from the
 * old backend. Without this, the first Vaani call to reuse one is treated as a
 * redelivery of a call from August: the log insert is skipped, the lead is not
 * updated, and the call is never billed — silently, with a 200 OK.
 *
 * See scripts/009_call_provider.sql and scripts/010_call_usage_provider.sql.
 */

/** Anything not explicitly recognised is the old backend, which is what every historical row is. */
export function callProvider(): string {
  const explicit = (process.env.CALL_PROVIDER ?? '').trim();
  if (explicit) return explicit;
  const base = (process.env.DOGRAH_API_URL ?? '').toLowerCase();
  return base.includes('vaani') ? 'vaani' : 'voice';
}

/** The provider every row written before the 2026-09-03 cutover belongs to. */
export const LEGACY_PROVIDER = 'voice';

/**
 * Does this database have the `provider` column yet?
 *
 * The migrations are pasted into the Supabase SQL editor by hand, so the code
 * has to work on both sides of that. Sending a column the table does not have
 * fails the whole insert, and losing call logs is far worse than losing the
 * collision guard — so this probes once, caches, and lets the caller degrade.
 *
 * Cached per table name, per process. A deploy re-probes.
 */
const capability = new Map<string, boolean>();

export async function hasProviderColumn(
  db: SupabaseClient,
  table: 'call_logs' | 'call_usage',
): Promise<boolean> {
  const cached = capability.get(table);
  if (cached !== undefined) return cached;
  // `head` so nothing is transferred; PostgREST still validates the column.
  const { error } = await db.from(table).select('provider', { head: true, count: 'exact' }).limit(1);
  const present = !error;
  if (!present) {
    console.warn(
      `[call-provider] ${table}.provider is missing — run-id collisions between ` +
        `backends are NOT guarded. Apply scripts/009_call_provider.sql and ` +
        `scripts/010_call_usage_provider.sql.`,
      { code: (error as any)?.code },
    );
  }
  capability.set(table, present);
  return present;
}

/** Only for tests, which need a fresh probe per case. */
export function resetProviderCapabilityCache(): void {
  capability.clear();
}

/**
 * Has this run already been recorded?
 *
 * Scoped by provider once the column exists, so Vaani run 464 and voice run 464
 * are two different calls rather than one.
 */
export async function findExistingCallLog(
  supabase: SupabaseClient,
  runId: number,
): Promise<{ found: boolean; row: Record<string, any> | null }> {
  // The row itself, not just its existence: the reconcile sweep needs to see
  // whether what is already stored is COMPLETE. The webhook fires at hang-up and
  // can land before the provider has finalised duration and uploaded the
  // recording, so "a row exists" does not mean "the call is recorded properly".
  // gathered_context and lead_id come back too: the sweep needs to know whether
  // what is stored actually carries a qualification, and which lead to complete.
  let query = supabase
    .from('call_logs')
    .select('id, lead_id, duration, recording_url, transcript_url, gathered_context')
    .eq('dograh_run_id', runId);
  if (await hasProviderColumn(supabase, 'call_logs')) {
    query = query.eq('provider', callProvider());
  }
  const { data } = await query.limit(1);
  const row = data && data.length > 0 ? (data[0] as Record<string, any>) : null;
  return { found: Boolean(row), row };
}

/**
 * Stamp a call_logs row with its backend, when the column is there to take it.
 */
export async function withProvider(
  supabase: SupabaseClient,
  row: Record<string, any>,
): Promise<Record<string, any>> {
  if (!(await hasProviderColumn(supabase, 'call_logs'))) return row;
  return { ...row, provider: callProvider() };
}
