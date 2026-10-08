import { NextResponse } from 'next/server';
import { createBillingClient, isBillingConfigured } from '@/lib/supabase-billing';
import { isAuthorisedOperator, operatorNotFound } from '@/lib/operator-auth';
import { getBillingConfig, toMilli, toCredits } from '@/lib/billing';
import { randomUUID } from 'crypto';

export const dynamic = 'force-dynamic';

/**
 * Manually add or remove credits — goodwill, a correction, an opening balance.
 *
 * A reason is mandatory, and every adjustment is an ordinary ledger entry that
 * appears on the client's own statement. Nothing happens off the books.
 */
export async function POST(request: Request) {
  if (!isAuthorisedOperator(request)) return operatorNotFound();
  if (!isBillingConfigured()) {
    return NextResponse.json({ error: 'Billing is not configured.' }, { status: 503 });
  }

  try {
    const body = await request.json().catch(() => ({}));
    const credits = Number(body?.credits);
    const reason = typeof body?.reason === 'string' ? body.reason.trim() : '';
    const kind = body?.kind === 'opening_balance' ? 'opening_balance' : 'adjustment';

    if (!Number.isFinite(credits) || credits === 0) {
      return NextResponse.json({ error: 'Enter a non-zero number of credits.' }, { status: 400 });
    }
    if (Math.abs(credits) > 1_000_000) {
      return NextResponse.json({ error: 'That is larger than the allowed adjustment.' }, { status: 400 });
    }
    if (reason.length < 3) {
      return NextResponse.json({ error: 'A reason is required for every adjustment.' }, { status: 400 });
    }

    const billing = createBillingClient();
    const config = await getBillingConfig(billing);

    const entryType =
      kind === 'opening_balance' ? 'opening_balance'
        : credits > 0 ? 'adjustment_credit'
          : 'adjustment_debit';

    // An opening balance uses a fixed key so it can only ever be posted once,
    // no matter how many times the setup step is run. Ordinary adjustments are
    // intentionally repeatable and get a fresh key each time.
    const idempotencyKey =
      kind === 'opening_balance' ? 'opening_balance:v1' : `adjust:${randomUUID()}`;

    const { data, error } = await billing.rpc('post_credit_entry', {
      p_idempotency_key: idempotencyKey,
      p_entry_type: entryType,
      p_amount_milli: toMilli(credits),
      p_ledger_secret: process.env.LEDGER_SECRET ?? '',
      p_account_id: config.accountId,
      p_description: reason.slice(0, 300),
      p_created_by: 'operator',
      p_metadata: { kind },
    });

    if (error) throw new Error(error.message);

    const row = Array.isArray(data) ? data[0] : data;
    return NextResponse.json({
      success: true,
      duplicate: Boolean(row?.already_posted),
      balance_credits: toCredits(Number(row?.balance_milli) || 0),
      entry_id: row?.entry_id ?? null,
    });
  } catch (err: any) {
    console.error('[operator/adjust] failed', err?.message);
    return NextResponse.json({ error: 'Could not post the adjustment.' }, { status: 500 });
  }
}
