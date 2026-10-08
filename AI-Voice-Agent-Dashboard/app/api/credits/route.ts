import { NextResponse } from 'next/server';
import { createBillingClient, isBillingConfigured } from '@/lib/supabase-billing';
import { getBillingConfig, balanceState, toCredits } from '@/lib/billing';

export const dynamic = 'force-dynamic';

/**
 * Balance, burn rate and runway — the numbers behind the top-bar pill.
 *
 * CLIENT-FACING. Every column is listed explicitly and provider_cost_milli is
 * never among them: one `select('*')` here would hand our margin to the client
 * in their browser devtools.
 */
export async function GET() {
  if (!isBillingConfigured()) {
    return NextResponse.json({ error: 'Billing is not configured.' }, { status: 503 });
  }

  try {
    const billing = createBillingClient();
    const config = await getBillingConfig(billing);

    // Burn over the last 7 days, used for the runway estimate.
    const sevenDaysAgo = new Date(Date.now() - 7 * 86_400_000).toISOString();
    const { data: recent, error: recentError } = await billing
      .from('credit_ledger')
      .select('amount_milli_credits, billed_seconds, created_at')
      .eq('account_id', config.accountId)
      .eq('entry_type', 'call_debit')
      .gte('created_at', sevenDaysAgo);

    if (recentError) throw new Error(recentError.message);

    const debits = recent ?? [];
    const spent7dMilli = debits.reduce((sum, r) => sum + Math.abs(Number(r.amount_milli_credits) || 0), 0);
    const calls7d = debits.length;

    // Spread over the days that actually saw calls rather than a flat 7. A
    // client who called on one day should not be told their credits last a
    // month because the other six days averaged in as zero.
    const activeDays = new Set(
      debits.map((r) => String(r.created_at).slice(0, 10)),
    ).size || 1;
    const avgDailyMilli = spent7dMilli > 0 ? spent7dMilli / activeDays : 0;

    const balanceMilli = config.balanceMilli;
    const runwayDays = avgDailyMilli > 0 ? balanceMilli / avgDailyMilli : null;
    const avgPerCallMilli = calls7d > 0 ? spent7dMilli / calls7d : 0;

    return NextResponse.json({
      balance_credits: toCredits(balanceMilli),
      balance_milli: balanceMilli,
      state: balanceState(balanceMilli, config),
      currency: config.currency,
      thresholds: {
        low_credits: toCredits(config.lowBalanceMilli),
        critical_credits: toCredits(config.criticalBalanceMilli),
      },
      rate: {
        credits_per_minute: config.rateMilliPerMinute / 1000,
        billing_increment_seconds: config.billingIncrementSeconds,
      },
      burn: {
        last_7d_credits: toCredits(spent7dMilli),
        avg_daily_credits: toCredits(Math.round(avgDailyMilli)),
        calls_7d: calls7d,
        avg_credits_per_call: toCredits(Math.round(avgPerCallMilli)),
      },
      runway: {
        days: runwayDays === null ? null : Math.round(runwayDays * 10) / 10,
        minutes: Math.max(0, Math.floor(balanceMilli / config.rateMilliPerMinute)),
        calls_est: avgPerCallMilli > 0 ? Math.max(0, Math.floor(balanceMilli / avgPerCallMilli)) : null,
        basis: calls7d > 0 ? '7d_average' : 'no_recent_calls',
      },
      lifetime: {
        purchased_credits: toCredits(config.lifetimePurchasedMilli),
        spent_credits: toCredits(config.lifetimeSpentMilli),
      },
      auto_pause: {
        enabled: config.autoPauseEnabled,
        at_credits: toCredits(config.autoPauseAtMilli),
      },
      updated_at: new Date().toISOString(),
    });
  } catch (err: any) {
    console.error('[api/credits] failed', err?.message);
    return NextResponse.json({ error: 'Could not load credits.' }, { status: 500 });
  }
}
