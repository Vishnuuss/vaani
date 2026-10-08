import { NextResponse } from 'next/server';
import { createBillingClient, isBillingConfigured } from '@/lib/supabase-billing';
import { isAuthorisedOperator, operatorNotFound } from '@/lib/operator-auth';
import { toCredits } from '@/lib/billing';

export const dynamic = 'force-dynamic';

/**
 * Is the ledger intact?
 *
 * Two independent checks:
 *   1. Drift — does the cached balance still equal the sum of the ledger?
 *   2. Hash chain — does every entry still follow from the one before it?
 *
 * The chain is what makes tampering *provable* rather than merely unlikely.
 * Anyone with direct database access can change a row; they cannot regenerate
 * the chain without LEDGER_SECRET, which lives only in the server environment.
 */
export async function GET(request: Request) {
  if (!isAuthorisedOperator(request)) return operatorNotFound();
  if (!isBillingConfigured()) {
    return NextResponse.json({ error: 'Billing is not configured.' }, { status: 503 });
  }

  try {
    const billing = createBillingClient();

    const { data: drift, error: driftError } = await billing
      .from('billing_balance_check')
      .select('*')
      .eq('account_id', 'default')
      .single();
    if (driftError) throw new Error(driftError.message);

    const { data: chain, error: chainError } = await billing.rpc('verify_ledger_chain', {
      p_ledger_secret: process.env.LEDGER_SECRET ?? '',
      p_account_id: 'default',
    });
    if (chainError) throw new Error(chainError.message);

    const breaks = (chain ?? []) as any[];
    const { count: entryCount } = await billing
      .from('credit_ledger')
      .select('id', { count: 'exact', head: true })
      .eq('account_id', 'default');

    const { count: unbilledCount } = await billing
      .from('call_usage')
      .select('id', { count: 'exact', head: true })
      .is('billed_at', null);

    const driftMilli = Number(drift?.drift_milli) || 0;

    return NextResponse.json({
      healthy: driftMilli === 0 && breaks.length === 0,
      balance: {
        cached_credits: toCredits(Number(drift?.cached_milli) || 0),
        ledger_sum_credits: toCredits(Number(drift?.ledger_sum_milli) || 0),
        drift_credits: toCredits(driftMilli),
        drift_ok: driftMilli === 0,
      },
      chain: {
        intact: breaks.length === 0,
        entries_checked: entryCount ?? 0,
        first_break: breaks[0] ?? null,
      },
      metering: {
        calls_awaiting_billing: unbilledCount ?? 0,
      },
      checked_at: new Date().toISOString(),
    });
  } catch (err: any) {
    console.error('[operator/integrity] failed', err?.message);
    return NextResponse.json({ error: 'Could not run the integrity check.' }, { status: 500 });
  }
}
