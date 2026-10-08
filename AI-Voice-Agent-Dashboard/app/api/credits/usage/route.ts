import { NextResponse } from 'next/server';
import { createBillingClient, isBillingConfigured } from '@/lib/supabase-billing';
import { toCredits } from '@/lib/billing';
import { billingPeriodStart, usageWindowStart } from '@/lib/billing-period';

export const dynamic = 'force-dynamic';

const PAGE = 1000;      // PostgREST caps a single response at 1000 rows
const MAX_ROWS = 50_000;

/**
 * Buckets a timestamp by calendar day in Asia/Kolkata.
 *
 * NOT toISOString().slice(0,10) — that buckets in UTC, so an evening IST call
 * lands on the next day and the client's "today" figure looks wrong.
 * /api/reports/overview has that bug; this does not inherit it.
 */
function istDay(iso: string): string {
  return new Date(new Date(iso).getTime() + 5.5 * 3600_000).toISOString().slice(0, 10);
}

/** Daily burn and per-campaign spend. CLIENT-FACING — no cost columns. */
export async function GET(request: Request) {
  if (!isBillingConfigured()) {
    return NextResponse.json({ error: 'Billing is not configured.' }, { status: 503 });
  }

  try {
    const url = new URL(request.url);
    const days = Math.min(365, Math.max(1, Number(url.searchParams.get('days')) || 30));
    // Clamped to the billing period start, so selecting 90 days cannot drag the
    // pre-handover test traffic back into the client's chart.
    const since = usageWindowStart(days);

    const billing = createBillingClient();

    // Page through, mirroring the guard already used in /api/calls/stats. A
    // single .select() silently stops at 1000 rows, which would quietly
    // under-report usage as soon as the client gets busy.
    const rows: any[] = [];
    for (let from = 0; from < MAX_ROWS; from += PAGE) {
      const { data, error } = await billing
        .from('credit_ledger')
        .select('amount_milli_credits, billed_seconds, campaign_id, created_at, entry_type')
        .eq('account_id', 'default')
        .eq('entry_type', 'call_debit')
        .gte('created_at', since)
        .order('created_at', { ascending: false })
        .range(from, from + PAGE - 1);

      if (error) throw new Error(error.message);
      if (!data || data.length === 0) break;
      rows.push(...data);
      if (data.length < PAGE) break;
    }

    const daily = new Map<string, { credits: number; calls: number; billed_seconds: number }>();
    const campaigns = new Map<number | string, { credits: number; calls: number; billed_seconds: number }>();
    let totalMilli = 0, totalSeconds = 0;

    for (const row of rows) {
      const milli = Math.abs(Number(row.amount_milli_credits) || 0);
      const secs = Number(row.billed_seconds) || 0;
      totalMilli += milli;
      totalSeconds += secs;

      const day = istDay(row.created_at);
      const d = daily.get(day) ?? { credits: 0, calls: 0, billed_seconds: 0 };
      d.credits += milli; d.calls += 1; d.billed_seconds += secs;
      daily.set(day, d);

      const key = row.campaign_id ?? 'unattributed';
      const c = campaigns.get(key) ?? { credits: 0, calls: 0, billed_seconds: 0 };
      c.credits += milli; c.calls += 1; c.billed_seconds += secs;
      campaigns.set(key, c);
    }

    // Fill gaps so the chart draws a continuous line rather than skipping
    // quiet days, which would misleadingly flatten the burn curve. Days before
    // the billing period opened are omitted entirely rather than drawn as zero -
    // a flat run of empty days in front of the real data reads as "we did
    // nothing that week" instead of "we were not billing yet".
    const firstDay = istDay(billingPeriodStart());
    const series: { date: string; credits: number; calls: number; billed_seconds: number }[] = [];
    for (let i = days - 1; i >= 0; i -= 1) {
      const date = istDay(new Date(Date.now() - i * 86_400_000).toISOString());
      if (date < firstDay) continue;
      const v = daily.get(date);
      series.push({
        date,
        credits: v ? toCredits(v.credits) : 0,
        calls: v?.calls ?? 0,
        billed_seconds: v?.billed_seconds ?? 0,
      });
    }

    const byCampaign = [...campaigns.entries()]
      .map(([id, v]) => ({
        campaign_id: id === 'unattributed' ? null : Number(id),
        calls: v.calls,
        billed_seconds: v.billed_seconds,
        credits: toCredits(v.credits),
        credits_per_call: v.calls > 0 ? toCredits(Math.round(v.credits / v.calls)) : 0,
      }))
      .sort((a, b) => b.credits - a.credits);

    // Whole-period totals, independent of the 7/30/90 day selector. The client
    // asks "what have I spent since we went live", and answering that with a
    // number that changes when they flick a chart tab is not an answer.
    const periodRows: any[] = [];
    for (let from = 0; from < MAX_ROWS; from += PAGE) {
      const { data, error } = await billing
        .from('credit_ledger')
        .select('amount_milli_credits, entry_type')
        .eq('account_id', 'default')
        .gte('created_at', billingPeriodStart())
        .range(from, from + PAGE - 1);
      if (error) throw new Error(error.message);
      if (!data || data.length === 0) break;
      periodRows.push(...data);
      if (data.length < PAGE) break;
    }

    let periodAdded = 0, periodSpent = 0, periodCalls = 0;
    for (const row of periodRows) {
      const milli = Number(row.amount_milli_credits) || 0;
      if (milli > 0) periodAdded += milli;
      else periodSpent += Math.abs(milli);
      if (row.entry_type === 'call_debit') periodCalls += 1;
    }

    return NextResponse.json({
      range_days: days,
      timezone: 'Asia/Kolkata',
      period_start: billingPeriodStart(),
      period: {
        credits_added: toCredits(periodAdded),
        credits_spent: toCredits(periodSpent),
        calls: periodCalls,
      },
      daily: series,
      by_campaign: byCampaign,
      totals: {
        credits: toCredits(totalMilli),
        calls: rows.length,
        billed_minutes: Math.round(totalSeconds / 60),
        avg_credits_per_call: rows.length > 0 ? toCredits(Math.round(totalMilli / rows.length)) : 0,
      },
    });
  } catch (err: any) {
    console.error('[api/credits/usage] failed', err?.message);
    return NextResponse.json({ error: 'Could not load usage.' }, { status: 500 });
  }
}
