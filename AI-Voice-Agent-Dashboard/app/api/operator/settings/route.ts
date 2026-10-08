import { NextResponse } from 'next/server';
import { createBillingClient, isBillingConfigured } from '@/lib/supabase-billing';
import { isAuthorisedOperator, operatorNotFound } from '@/lib/operator-auth';

export const dynamic = 'force-dynamic';

/**
 * Pricing, rate card and payment details. OPERATOR ONLY.
 *
 * These are the settings the client must never be able to reach: he could
 * otherwise set his own price per minute to zero, or read what we pay our
 * providers. They live in the billing database precisely so that his Supabase
 * login cannot touch them.
 */

export async function GET(request: Request) {
  if (!isAuthorisedOperator(request)) return operatorNotFound();
  if (!isBillingConfigured()) {
    return NextResponse.json({ error: 'Billing is not configured.' }, { status: 503 });
  }

  try {
    const billing = createBillingClient();
    const { data, error } = await billing
      .from('billing_account')
      .select('*')
      .eq('id', 'default')
      .single();
    if (error) throw new Error(error.message);
    return NextResponse.json({ account: data });
  } catch (err: any) {
    console.error('[operator/settings] GET failed', err?.message);
    return NextResponse.json({ error: 'Could not load settings.' }, { status: 500 });
  }
}

/**
 * Only these columns may be written. An explicit allowlist, so a future field
 * cannot be changed by accident — and notably `balance_milli_credits` is NOT
 * on it: the balance moves only through the ledger, never by direct edit.
 */
const WRITABLE = new Set([
  'display_name',
  'rate_milli_per_minute',
  'billing_increment_seconds',
  'low_balance_milli',
  'critical_balance_milli',
  'auto_pause_at_milli',
  'auto_pause_enabled',
  'rate_card',
  'payment_instructions',
]);

export async function PATCH(request: Request) {
  if (!isAuthorisedOperator(request)) return operatorNotFound();
  if (!isBillingConfigured()) {
    return NextResponse.json({ error: 'Billing is not configured.' }, { status: 503 });
  }

  try {
    const body = await request.json().catch(() => ({}));
    const patch: Record<string, unknown> = {};

    for (const [key, value] of Object.entries(body ?? {})) {
      if (WRITABLE.has(key)) patch[key] = value;
    }
    if (Object.keys(patch).length === 0) {
      return NextResponse.json({ error: 'Nothing to update.' }, { status: 400 });
    }

    // Guard the two that can silently destroy revenue if fat-fingered.
    if ('rate_milli_per_minute' in patch) {
      const n = Number(patch.rate_milli_per_minute);
      if (!Number.isFinite(n) || n < 100 || n > 1_000_000) {
        return NextResponse.json(
          { error: 'Rate must be between 0.1 and 1000 credits per minute.' }, { status: 400 },
        );
      }
      patch.rate_milli_per_minute = Math.round(n);
    }
    if ('billing_increment_seconds' in patch) {
      const n = Number(patch.billing_increment_seconds);
      if (!Number.isFinite(n) || n < 1 || n > 600) {
        return NextResponse.json(
          { error: 'Billing increment must be between 1 and 600 seconds.' }, { status: 400 },
        );
      }
      patch.billing_increment_seconds = Math.round(n);
    }

    const billing = createBillingClient();
    const { data, error } = await billing
      .from('billing_account')
      .update(patch)
      .eq('id', 'default')
      .select('*')
      .single();
    if (error) throw new Error(error.message);

    return NextResponse.json({ success: true, account: data });
  } catch (err: any) {
    console.error('[operator/settings] PATCH failed', err?.message);
    return NextResponse.json({ error: 'Could not save settings.' }, { status: 500 });
  }
}
