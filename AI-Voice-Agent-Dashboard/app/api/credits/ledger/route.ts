import { NextResponse } from 'next/server';
import { createBillingClient, isBillingConfigured } from '@/lib/supabase-billing';
import { toCredits } from '@/lib/billing';
import { billingPeriodStart } from '@/lib/billing-period';

export const dynamic = 'force-dynamic';

const MAX_LIMIT = 100;

/** Plain-English label for each ledger entry type, for the statement. */
const TYPE_LABEL: Record<string, string> = {
  call_debit: 'Call',
  topup_credit: 'Credits added',
  opening_balance: 'Opening balance',
  adjustment_credit: 'Adjustment',
  adjustment_debit: 'Adjustment',
  refund_credit: 'Refund',
  promo_credit: 'Bonus credits',
};

/**
 * The client's statement: every credit and debit, newest first.
 *
 * CLIENT-FACING. The select list is explicit and deliberately omits
 * provider_cost_milli and provider_cost_breakdown — those are ours.
 */
export async function GET(request: Request) {
  if (!isBillingConfigured()) {
    return NextResponse.json({ error: 'Billing is not configured.' }, { status: 503 });
  }

  try {
    const url = new URL(request.url);
    const page = Math.max(1, Number(url.searchParams.get('page')) || 1);
    const limit = Math.min(MAX_LIMIT, Math.max(1, Number(url.searchParams.get('limit')) || 25));
    const type = url.searchParams.get('type');
    const from = url.searchParams.get('from');
    const to = url.searchParams.get('to');

    const billing = createBillingClient();

    let query = billing
      .from('credit_ledger')
      .select(
        'id, created_at, entry_type, amount_milli_credits, balance_after_milli, description, ' +
        'billed_seconds, actual_seconds, dograh_run_id, campaign_id, external_ref, created_by',
        { count: 'exact' },
      )
      .eq('account_id', 'default')
      // The client's statement starts when their billing period opened. Anything
      // older is our build-and-test traffic and was never their charge. The rows
      // are still in the ledger - this hides them, it does not delete them, so
      // the hash chain and the integrity check are untouched.
      .gte('created_at', billingPeriodStart())
      .order('created_at', { ascending: false })
      .range((page - 1) * limit, page * limit - 1);

    if (type) query = query.eq('entry_type', type);
    // A caller-supplied `from` can narrow the window but never widen it past the
    // period start.
    if (from && from > billingPeriodStart()) query = query.gte('created_at', from);
    if (to) query = query.lte('created_at', to);

    const { data, error, count } = await query;
    if (error) throw new Error(error.message);

    // Cast because the select list is a concatenated string, which defeats
    // supabase-js's literal-type inference and leaves it as a union.
    const entries = ((data ?? []) as any[]).map((row) => ({
      id: row.id,
      created_at: row.created_at,
      entry_type: row.entry_type,
      label: TYPE_LABEL[row.entry_type] ?? row.entry_type,
      amount_credits: toCredits(Number(row.amount_milli_credits) || 0),
      balance_after_credits:
        row.balance_after_milli === null ? null : toCredits(Number(row.balance_after_milli)),
      description: row.description,
      billed_seconds: row.billed_seconds,
      actual_seconds: row.actual_seconds,
      dograh_run_id: row.dograh_run_id,
      campaign_id: row.campaign_id,
      external_ref: row.external_ref,
      created_by: row.created_by,
    }));

    const total = count ?? 0;
    return NextResponse.json({
      entries,
      period_start: billingPeriodStart(),
      pagination: { page, limit, total, totalPages: Math.max(1, Math.ceil(total / limit)) },
    });
  } catch (err: any) {
    console.error('[api/credits/ledger] failed', err?.message);
    return NextResponse.json({ error: 'Could not load the statement.' }, { status: 500 });
  }
}
