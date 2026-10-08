import { NextResponse } from 'next/server';
import { createBillingClient, isBillingConfigured } from '@/lib/supabase-billing';
import { isAuthorisedOperator, operatorNotFound } from '@/lib/operator-auth';
import { getBillingConfig, toMilli, toCredits } from '@/lib/billing';

export const dynamic = 'force-dynamic';

/**
 * The money-in queue.
 *
 * Approving here is THE ONLY WAY credits are ever created. The client can file
 * requests; only this route, behind the operator key, turns one into a balance.
 */

export async function GET(request: Request) {
  if (!isAuthorisedOperator(request)) return operatorNotFound();
  if (!isBillingConfigured()) {
    return NextResponse.json({ error: 'Billing is not configured.' }, { status: 503 });
  }

  try {
    const url = new URL(request.url);
    const status = url.searchParams.get('status') ?? 'pending';

    const billing = createBillingClient();
    let query = billing
      .from('topup_requests')
      .select('*')
      .eq('account_id', 'default')
      .order('requested_at', { ascending: false })
      .limit(100);

    if (status !== 'all') query = query.eq('status', status);

    const { data, error } = await query;
    if (error) throw new Error(error.message);

    const { count: pendingCount } = await billing
      .from('topup_requests')
      .select('id', { count: 'exact', head: true })
      .eq('account_id', 'default')
      .eq('status', 'pending');

    return NextResponse.json({ requests: data ?? [], pending_count: pendingCount ?? 0 });
  } catch (err: any) {
    console.error('[operator/topups] GET failed', err?.message);
    return NextResponse.json({ error: 'Could not load requests.' }, { status: 500 });
  }
}

/** Approve or reject. Approval posts a topup_credit to the ledger. */
export async function POST(request: Request) {
  if (!isAuthorisedOperator(request)) return operatorNotFound();
  if (!isBillingConfigured()) {
    return NextResponse.json({ error: 'Billing is not configured.' }, { status: 503 });
  }

  try {
    const body = await request.json().catch(() => ({}));
    const { id, action } = body ?? {};
    const note = typeof body?.note === 'string' ? body.note.trim().slice(0, 300) : null;
    const externalRef = typeof body?.external_ref === 'string' ? body.external_ref.trim().slice(0, 120) : null;

    if (!id || !['approve', 'reject'].includes(action)) {
      return NextResponse.json({ error: 'Provide an id and action of approve or reject.' }, { status: 400 });
    }

    const billing = createBillingClient();
    const config = await getBillingConfig(billing);

    const { data: req, error: loadError } = await billing
      .from('topup_requests')
      .select('*')
      .eq('id', id)
      .single();

    if (loadError || !req) {
      return NextResponse.json({ error: 'Request not found.' }, { status: 404 });
    }
    if (req.status !== 'pending') {
      // Already decided. Return success so a double-click is harmless.
      return NextResponse.json({ success: true, already: req.status, request: req });
    }

    if (action === 'reject') {
      const { data, error } = await billing
        .from('topup_requests')
        .update({
          status: 'rejected',
          decided_at: new Date().toISOString(),
          decided_by: 'operator',
          decision_note: note,
        })
        .eq('id', id)
        .eq('status', 'pending')   // lost update guard
        .select('*')
        .single();
      if (error) throw new Error(error.message);
      return NextResponse.json({ success: true, request: data });
    }

    // ── Approve ──────────────────────────────────────────────────────────────
    // Credits granted are credits_requested, NOT amount_inr. Those two now
    // differ by the GST on the request, and tax must never become calling time.
    const credits = Number(req.credits_requested) || 0;
    if (credits <= 0) {
      return NextResponse.json({ error: 'Request has no credits on it.' }, { status: 400 });
    }

    // Keyed on the request id, so approving twice can never credit twice —
    // even if two operator tabs click at the same moment.
    const { data: posted, error: postError } = await billing.rpc('post_credit_entry', {
      p_idempotency_key: `topup:${req.id}`,
      p_entry_type: 'topup_credit',
      p_amount_milli: toMilli(credits),
      p_ledger_secret: process.env.LEDGER_SECRET ?? '',
      p_account_id: config.accountId,
      p_description: `Credits added — ${credits.toLocaleString('en-IN')} credits`,
      p_external_ref: externalRef ?? req.reference_note ?? null,
      p_created_by: 'operator',
      p_metadata: {
        topup_request_id: req.id,
        method: req.method,
        // The money side of the same event, kept on the ledger entry so the tax
        // collected is recoverable from the ledger alone.
        base_amount_inr: Number(req.base_amount_inr ?? credits),
        gst_rate_percent: Number(req.gst_rate_percent ?? 0),
        gst_amount_inr: Number(req.gst_amount_inr ?? 0),
        amount_paid_inr: Number(req.amount_inr ?? credits),
      },
    });

    if (postError) throw new Error(postError.message);

    const row = Array.isArray(posted) ? posted[0] : posted;

    const { data: updated, error: updateError } = await billing
      .from('topup_requests')
      .update({
        status: 'approved',
        decided_at: new Date().toISOString(),
        decided_by: 'operator',
        decision_note: note,
        ledger_entry_id: row?.entry_id ?? null,
      })
      .eq('id', id)
      .select('*')
      .single();

    if (updateError) throw new Error(updateError.message);

    return NextResponse.json({
      success: true,
      request: updated,
      credited: credits,
      duplicate: Boolean(row?.already_posted),
      balance_credits: toCredits(Number(row?.balance_milli) || 0),
    });
  } catch (err: any) {
    console.error('[operator/topups] POST failed', err?.message);
    return NextResponse.json({ error: 'Could not process the request.' }, { status: 500 });
  }
}
