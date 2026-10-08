import { NextResponse } from 'next/server';
import { createBillingClient, isBillingConfigured } from '@/lib/supabase-billing';
import { getBillingConfig, gstOn, GST_RATE_PERCENT } from '@/lib/billing';
import {
  TOPUP_SELECT_FULL,
  TOPUP_SELECT_LEGACY,
  isMissingGstColumn,
  topupInsert,
  warnMissingGstColumns,
  withDerivedGst,
} from '@/lib/topup-columns';

export const dynamic = 'force-dynamic';

const MIN_CREDITS = 100;
const MAX_CREDITS = 500_000;
const MAX_PENDING = 3;

/**
 * The client's top-up flow.
 *
 * ────────────────────────────────────────────────────────────────────────────
 * THIS ROUTE NEVER CREATES CREDITS. It only files a request.
 *
 * That is the whole security model for money coming in. The client has a login
 * to this dashboard, so anything reachable here is reachable by him. Credits
 * come into existence in exactly one place — an operator approving a request in
 * /operator, authenticated with OPERATOR_KEY, which he does not have.
 *
 * Worst case he spams requests and we reject them. He can never move a balance.
 * ────────────────────────────────────────────────────────────────────────────
 */

/** Payment instructions plus this client's own recent requests. */
export async function GET() {
  if (!isBillingConfigured()) {
    return NextResponse.json({ error: 'Billing is not configured.' }, { status: 503 });
  }

  try {
    const billing = createBillingClient();

    const { data: account } = await billing
      .from('billing_account')
      .select('payment_instructions, rate_milli_per_minute, currency')
      .eq('id', 'default')
      .single();

    // Typed loosely on purpose: supabase-js parses the select string at the TYPE
    // level, so passing it as a variable makes it infer ParserError. The shape is
    // pinned by TOPUP_SELECT_* instead.
    const recentRequests = async (select: string): Promise<{ data: any[] | null; error: any }> =>
      billing
        .from('topup_requests')
        .select(select)
        .eq('account_id', 'default')
        .order('requested_at', { ascending: false })
        .limit(10);

    let { data: requests, error } = await recentRequests(TOPUP_SELECT_FULL);

    // 004_gst_on_topups.sql may not have been run yet. Serve the request history
    // in its pre-GST shape rather than failing the whole dialog — see
    // lib/topup-columns.ts.
    if (isMissingGstColumn(error)) {
      warnMissingGstColumns('api/credits/topup');
      const legacy = await recentRequests(TOPUP_SELECT_LEGACY);
      error = legacy.error;
      requests = legacy.data?.map(withDerivedGst) ?? null;
    }

    if (error) throw new Error(error.message);

    return NextResponse.json({
      payment: account?.payment_instructions ?? {},
      credits_per_minute: (account?.rate_milli_per_minute ?? 4000) / 1000,
      currency: account?.currency ?? 'INR',
      presets: [500, 1000, 2500, 5000],
      // The dialog must never do tax arithmetic of its own — it renders what
      // this says, and the QR route computes the same figure from gstOn().
      gst_rate_percent: GST_RATE_PERCENT,
      requests: requests ?? [],
    });
  } catch (err: any) {
    console.error('[api/credits/topup] GET failed', err?.message);
    return NextResponse.json({ error: 'Could not load top-up details.' }, { status: 500 });
  }
}

/** File a top-up request. Creates a pending row and nothing else. */
export async function POST(request: Request) {
  if (!isBillingConfigured()) {
    return NextResponse.json({ error: 'Billing is not configured.' }, { status: 503 });
  }

  try {
    const body = await request.json().catch(() => ({}));
    const credits = Math.round(Number(body?.credits));
    const note = typeof body?.reference_note === 'string'
      ? body.reference_note.trim().slice(0, 200)
      : null;

    if (!Number.isFinite(credits) || credits < MIN_CREDITS || credits > MAX_CREDITS) {
      return NextResponse.json(
        { error: `Enter an amount between ${MIN_CREDITS} and ${MAX_CREDITS.toLocaleString('en-IN')} credits.` },
        { status: 400 },
      );
    }

    const billing = createBillingClient();
    const config = await getBillingConfig(billing);

    // Cap outstanding requests so the queue cannot be flooded. Nothing here can
    // cost money, but an operator should not have to wade through 500 rows.
    const { count } = await billing
      .from('topup_requests')
      .select('id', { count: 'exact', head: true })
      .eq('account_id', config.accountId)
      .eq('status', 'pending');

    if ((count ?? 0) >= MAX_PENDING) {
      return NextResponse.json(
        { error: 'You already have top-up requests waiting to be confirmed. We will process them shortly.' },
        { status: 429 },
      );
    }

    // 1 credit = 1 rupee of calling, plus GST on top. The client pays
    // amount_inr; the credits he receives on approval stay credits_requested,
    // because tax is not calling time.
    const gst = gstOn(credits);

    const baseRow = {
      account_id: config.accountId,
      credits_requested: credits,
      status: 'pending',
      method: 'manual_upi',
      reference_note: note,
    };

    const fileRequest = async (withGst: boolean): Promise<{ data: any; error: any }> =>
      billing
        .from('topup_requests')
        .insert(topupInsert(baseRow, gst, withGst))
        .select(withGst ? TOPUP_SELECT_FULL : TOPUP_SELECT_LEGACY)
        .single();

    let { data, error } = await fileRequest(true);

    // Without this the client simply could not buy credits while 004 was
    // unapplied — the insert named three columns that did not exist. The row
    // still records the gross the client is asked to pay, so an operator
    // approving it credits the right number and collects the right amount.
    if (isMissingGstColumn(error)) {
      warnMissingGstColumns('api/credits/topup');
      const legacy = await fileRequest(false);
      error = legacy.error;
      data = legacy.data ? withDerivedGst(legacy.data) : null;
    }

    if (error) throw new Error(error.message);

    return NextResponse.json({
      success: true,
      request: data,
      message: 'Request received. Your credits will appear once we confirm the payment.',
    }, { status: 201 });
  } catch (err: any) {
    console.error('[api/credits/topup] POST failed', err?.message);
    return NextResponse.json({ error: 'Could not submit the request.' }, { status: 500 });
  }
}
