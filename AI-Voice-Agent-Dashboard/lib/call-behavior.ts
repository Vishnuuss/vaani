import type { SupabaseClient } from '@supabase/supabase-js';

const DEFAULT_MAX_RETRIES = 2;
const MIN_MAX_RETRIES = 0;
const MAX_MAX_RETRIES = 10;

const DEFAULT_CALL_GAP_MINUTES = 30;
const MIN_CALL_GAP_MINUTES = 0;
const MAX_CALL_GAP_MINUTES = 1440;

/**
 * How many times the retry sweep re-attempts an unanswered lead before giving
 * up and marking it unreachable.
 *
 * Previously hardcoded as `MAX_RETRIES = 2` in both the webhook handler and
 * the reconcile job, while the "Call behaviour" card on the AI Agent page
 * wrote a `maxRetries` value to the settings table that nothing ever read -
 * the Save button worked, the number it saved just did nothing. This reads
 * that same value back, so the dashboard control is real.
 */
export interface CallBehaviorSettings {
  maxRetries: number;
  callGapMinutes: number;
}

/** Both call-behaviour values in one query, for callers that need both. */
export async function getCallBehaviorSettings(supabase: SupabaseClient): Promise<CallBehaviorSettings> {
  const { data } = await supabase.from('settings').select('value').eq('key', 'call_behavior').maybeSingle();
  const value = (data?.value as any) ?? {};

  const retries = Number(value.maxRetries);
  const gap = Number(value.callGapMinutes);

  return {
    maxRetries: Number.isFinite(retries)
      ? Math.min(Math.max(Math.round(retries), MIN_MAX_RETRIES), MAX_MAX_RETRIES)
      : DEFAULT_MAX_RETRIES,
    callGapMinutes: Number.isFinite(gap)
      ? Math.min(Math.max(Math.round(gap), MIN_CALL_GAP_MINUTES), MAX_CALL_GAP_MINUTES)
      : DEFAULT_CALL_GAP_MINUTES,
  };
}

export async function getMaxRetries(supabase: SupabaseClient): Promise<number> {
  return (await getCallBehaviorSettings(supabase)).maxRetries;
}
