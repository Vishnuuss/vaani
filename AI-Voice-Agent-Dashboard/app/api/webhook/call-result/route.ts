import { NextResponse } from 'next/server';
import { timingSafeEqual } from 'node:crypto';
import { createServerClient } from '@/lib/supabase-server';
import { createBillingClient, isBillingConfigured } from '@/lib/supabase-billing';
import { getBillingConfig, postDebit, toCredits } from '@/lib/billing';
import { meterSingleRun } from '@/lib/billing-meter';
import { enforceBalanceGuard } from '@/lib/billing-guard';
import { getMaxRetries } from '@/lib/call-behavior';
import { findExistingCallLog, withProvider } from '@/lib/call-provider';
import { dograh } from '@/lib/dograh';
import { workflowIdFor } from '@/lib/workflow-routing';
import { updateLead } from '@/lib/lead-update';
import { leadStatusFor, scoreCall } from '@/lib/call-scoring';
import { DEFAULT_VERTICAL, isWrongVerticalMatch, parseVertical } from '@/lib/verticals';
import {
  buildGatheredContext,
  buildNoteLine,
  cleanNumber,
  cleanString,
  extractCallSignals,
  extractQaVerdict,
  flattenPayload,
  hasQualificationSignal,
  parseFollowUpDate,
  usableMediaUrl,
} from '@/lib/call-context';

/**
 * PUBLIC endpoint - receives call results from Dograh after each call ends.
 *
 * Auth:        X-API-Key, X-Webhook-Secret or Authorization: Bearer must equal
 *              DOGRAH_WEBHOOK_SECRET.
 * Idempotency: enforced on dograh_run_id. A duplicate delivery never re-scores a
 *              lead or re-increments retry_count.
 *
 * Any payload we cannot act on is acknowledged with 200 - returning 4xx/5xx makes
 * the provider redeliver the same event indefinitely.
 */

const MAX_NOTES_LENGTH = 2_000;

// The handler now also reads the run back from Dograh and posts the charge, so
// give it room beyond the platform's short default.
export const maxDuration = 30;

/** Constant-time comparison so the secret cannot be recovered by timing the response. */
function secretMatches(provided: string | null, expected: string): boolean {
  if (!provided) return false;
  const a = Buffer.from(provided);
  const b = Buffer.from(expected);
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}

/** Dograh can be configured with any of these header styles; accept all three. */
function presentedSecret(request: Request): string | null {
  const auth = request.headers.get('authorization');
  const bearer = auth?.toLowerCase().startsWith('bearer ') ? auth.slice(7).trim() : null;
  return request.headers.get('x-api-key') ?? request.headers.get('x-webhook-secret') ?? bearer;
}

/**
 * Charge for the call that just ended, right now.
 *
 * Billing used to happen ONLY on the ten-minute reconcile cron, so the balance
 * the client watches during a campaign stayed frozen while calls completed.
 * That is the bug: the meter was correct but invisibly late.
 *
 * This is a latency optimisation, not the correctness guarantee — the cron
 * sweep is still what makes "no call escapes billing" true. Both paths build
 * the idempotency key with debitIdempotencyKey(), so whichever runs second
 * posts nothing. Consequently NOTHING here may throw or return an error: the
 * webhook's job is to record the call, and a bad minute in accounting must not
 * turn into a redelivery loop.
 */
async function chargeForRun(
  clientDb: ReturnType<typeof createServerClient>,
  runId: number | null,
): Promise<void> {
  if (!runId || !isBillingConfigured()) return;

  try {
    const billing = createBillingClient();
    const config = await getBillingConfig(billing);

    const call = await meterSingleRun(billing, runId);
    if (!call) return;   // Unreadable from Dograh; the cron sweep will get it.

    const debit = await postDebit(billing, call as any, config);
    if (!debit.posted) return;

    // Stop the calling immediately if that charge emptied the balance, rather
    // than letting up to ten more minutes of calls run on credit.
    if (debit.balanceMilli !== null) {
      await enforceBalanceGuard(clientDb, config, debit.balanceMilli);
    }

    console.info('[webhook] charged call', {
      run: runId,
      credits: toCredits(debit.amountMilli),
      balance_credits: debit.balanceMilli === null ? null : toCredits(debit.balanceMilli),
    });
  } catch (err: any) {
    console.error('[webhook] inline billing failed; cron sweep will retry', {
      run: runId,
      error: err?.message,
    });
  }
}

export async function POST(request: Request) {
  try {
    const expectedSecret = process.env.DOGRAH_WEBHOOK_SECRET;
    if (!expectedSecret) {
      // Fail closed. Previously, a missing secret combined with a missing header
      // still produced a 401 by luck rather than by design.
      console.error('[webhook] DOGRAH_WEBHOOK_SECRET is not configured; rejecting delivery.');
      return NextResponse.json({ error: 'Webhook not configured' }, { status: 503 });
    }

    if (!secretMatches(presentedSecret(request), expectedSecret)) {
      // Never log any portion of the presented credential.
      console.warn('[webhook] rejected delivery: invalid secret');
      return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
    }

    let raw = (await request.json().catch(() => null)) as Record<string, any> | null;
    if (!raw || typeof raw !== 'object') {
      return NextResponse.json({ error: 'Invalid JSON body' }, { status: 400 });
    }

    // Dograh renders its payload from a Jinja template, which means every value
    // arrives as a string and an unresolved variable arrives as "", "None" or
    // "Undefined" rather than being omitted - and the extracted variables may sit
    // flat on the payload or nested under gathered_context. flattenPayload +
    // extractCallSignals handle every one of those shapes.
    let flat = flattenPayload(raw);
    let signals = extractCallSignals(raw);

    const runId = cleanNumber(flat.run_id ?? flat.workflow_run_id ?? flat.call_id ?? flat.id);
    const phone = cleanString(flat.phone ?? flat.phone_number ?? flat.to_number ?? flat.customer_phone);
    const leadIdFromPayload = cleanString(flat.lead_id ?? flat.external_id);
    // Dograh's template maps `outcome` to gathered_context.call_disposition; keep
    // the other spellings as fallbacks for manual/legacy deliveries.
    const outcomeRaw =
      cleanString(
        flat.outcome ??
          flat.call_disposition ??
          flat.mapped_call_disposition ??
          flat.status ??
          flat.call_status,
      ) ?? 'completed';
    let durationRaw =
      cleanNumber(flat.duration ?? flat.call_duration_seconds ?? flat.call_duration) ??
      cleanNumber(raw?.cost_info?.call_duration_seconds) ??
      0;
    // Prefer the public URL variants - the plain ones require an authenticated
    // session, so the dashboard cannot play them back for the client. Anything
    // that is not an absolute link (Dograh sends storage keys like
    // "recordings/23.wav") is dropped rather than stored as a dead link.
    let recording = usableMediaUrl(flat.recording_public_url ?? flat.recording ?? flat.recording_url);
    let transcript = usableMediaUrl(flat.transcript_public_url ?? flat.transcript ?? flat.transcript_url);
    const callTime = cleanString(flat.call_time ?? flat.ended_at ?? flat.created_at);

    const supabase = createServerClient();

    // --- Idempotency pre-check -------------------------------------------
    // Scoped by calling backend. A run id is unique only within one backend,
    // and Vaani's numbering has already reached ids the old one used, so an
    // unscoped check reports a brand new call as "Already processed" and drops
    // it with a 200. See lib/call-provider.ts.
    if (runId) {
      const { found } = await findExistingCallLog(supabase, runId);
      if (found) {
        return NextResponse.json({ message: 'Already processed', duplicate: true }, { status: 200 });
      }
    }

    // --- Trust the run, not the template ----------------------------------
    // Vaani's webhook node fires at hang-up, and `perform_final_variable_extraction`
    // has not always finished by then. When it has not, every
    // {{gathered_context.X}} renders empty and {{cost_info.call_duration_seconds}}
    // renders 0 - and we would store THAT as the record of the call.
    //
    // Run 798 (solar, 2026-09-06) is what this is for: a 70-second call with
    // house_ownership "own", solar_planning true and lead_score 100 was stored as
    // a zero-second call with no answers and a lead scored 0. A qualified
    // customer, presented to the client as a dud. It is a race, so it spoils some
    // calls and not others.
    //
    // The run itself is authoritative and is one request away - the billing path
    // below already re-reads it for exactly this reason. Fetching it here closes
    // the race at the source, so the row is right within seconds instead of
    // waiting up to ten minutes for the reconcile sweep to repair it.
    //
    // Best-effort by design: if this fetch fails we still write the thin row,
    // because a thin record of a real call beats no record, and the sweep will
    // complete it later.
    if (runId && (durationRaw <= 0 || !hasQualificationSignal(signals))) {
      try {
        const vertical = parseVertical(flat.vertical) ?? DEFAULT_VERTICAL;
        // A wrong workflow id is harmless: Vaani resolves the run by id and
        // ignores the workflow in the path (verified on run 798).
        const workflowId = workflowIdFor(vertical) ?? Number(process.env.DOGRAH_WORKFLOW_ID ?? 1);
        const full = await dograh.getWorkflowRun(workflowId, runId);
        if (full && Number(full.id) === runId) {
          // The payload stays the base so anything Vaani only sends in the
          // template (lead_id, vertical) survives; the run wins where both have
          // a value, because the run is the one that is finished.
          const merged: Record<string, any> = {
            ...(raw ?? {}),
            ...full,
            gathered_context: { ...(raw?.gathered_context ?? {}), ...(full.gathered_context ?? {}) },
          };
          raw = merged;
          flat = flattenPayload(merged);
          signals = extractCallSignals(merged);
          durationRaw =
            cleanNumber(full?.cost_info?.call_duration_seconds) ?? durationRaw;
          recording = recording ?? usableMediaUrl(full.recording_public_url);
          transcript = transcript ?? usableMediaUrl(full.transcript_public_url);
        }
      } catch (err) {
        console.warn('[webhook] could not read the run back; storing the payload as sent', { runId, err });
      }
    }

    // --- Resolve the lead -------------------------------------------------
    let lead: any = null;
    // Set when a phone DID match a lead but in another business line. Without
    // this the call is reported as a plain "lead_not_found", which reads like a
    // bad number - and a test call to a number already saved under a different
    // line silently disappears with no call_log and no row on screen.
    let mismatchedVertical: string | null = null;

    if (leadIdFromPayload) {
      const { data } = await supabase.from('leads').select('*').eq('id', leadIdFromPayload).maybeSingle();
      lead = data;
    }
    // The business line this call was FOR, taken raw from the payload and left
    // null when the agent did not say. Deliberately NOT signals.vertical: that
    // one applies DEFAULT_VERTICAL ('loan') whenever the field is absent, so an
    // agent that simply forgets to send `vertical` would look like it said
    // "loan" and every real-estate result matched by phone would be thrown away.
    // Null here means "no opinion", which leaves the guard below inert.
    const expectedVertical = parseVertical(
      flat.vertical ?? flat.business_line ?? flat.product_line ?? flat.agent_type,
    );

    if (!lead && phone) {
      // Phone is not guaranteed unique, so take the most recently contacted match.
      // Also try the bare 10-digit form: the CSV stores +91XXXXXXXXXX but some
      // providers report the number back without the country code.
      const candidates = [phone];
      const digits = phone.replace(/\D/g, '');
      if (digits.length >= 10) {
        const last10 = digits.slice(-10);
        candidates.push(last10, `+91${last10}`, `91${last10}`);
      }

      for (const candidate of candidates) {
        const { data } = await supabase
          .from('leads')
          .select('*')
          .eq('phone', candidate)
          .order('last_attempt_at', { ascending: false, nullsFirst: false })
          .limit(1);
        const match = data?.[0];
        if (!match) continue;

        // Matching by phone is a guess once the same number can be a lead in
        // more than one business line - `leads.phone` is no longer unique. If
        // this call's agent named its business line and it disagrees with the
        // lead's, refuse the match: an unmatched call is a logged warning, but
        // writing a solar call's score and recording onto the same person's
        // loan lead is invisible and corrupts what the client bills from.
        if (isWrongVerticalMatch(match.vertical, expectedVertical)) {
          mismatchedVertical = String(match.vertical);
          console.warn('[webhook] phone matched a lead in a different business line; refusing it', {
            runId,
            phone,
            leadVertical: match.vertical,
            expected: expectedVertical,
          });
          continue;
        }

        lead = match;
        break;
      }
    }

    if (!lead) {
      // Acknowledge so the provider stops retrying an event we can never match.
      if (mismatchedVertical) {
        console.error(
          `[webhook] DROPPED a ${expectedVertical} call: ${phone} is saved as a ${mismatchedVertical} lead. ` +
            'Add the number as a lead in this business line - (phone, vertical) is the key, so the same ' +
            'number can exist in both.',
          { runId },
        );
      } else {
        console.warn('[webhook] no matching lead for run', runId);
      }
      // Still bill it. A call we cannot match to a lead was still dialled, still
      // connected and still cost money; the charge is metered from Dograh and
      // does not depend on the lead at all.
      await chargeForRun(supabase, runId);
      return NextResponse.json(
        {
          success: false,
          reason: mismatchedVertical ? 'lead_vertical_mismatch' : 'lead_not_found',
          ...(mismatchedVertical ? { lead_vertical: mismatchedVertical, call_vertical: expectedVertical } : {}),
        },
        { status: 200 },
      );
    }

    // --- Score the call ---------------------------------------------------
    // Which ladder to score on. The agent's own hard-coded `vertical` wins (it
    // knows which script it ran), then the lead's column. Without this a solar
    // call was scored on the loan rules.
    const callVertical = expectedVertical ?? parseVertical(lead.vertical) ?? DEFAULT_VERTICAL;

    const result = scoreCall({
      vertical: callVertical,
      house_ownership: signals.house_ownership,
      solar_planning: signals.solar_planning,
      currently_investing: signals.currently_investing,
      investment_type: signals.investment_type,
      interested: signals.interested,
      budget: signals.budget,
      visit_date: signals.visit_date,
      loan_type: signals.loan_type,
      profession: signals.profession,
      do_not_call: signals.do_not_call,
      outcome: outcomeRaw,
      duration: durationRaw,
      // The agent scores the call itself (100 named a loan type / 50 needs a loan
      // but no type / 0 not interested). When present it wins outright - the
      // additive rules below it assume budget, income and occupation questions
      // the two-question agent no longer asks, and would mark a perfect call ~60.
      lead_score: signals.lead_score,
    });

    const gatheredContext = buildGatheredContext(
      signals,
      result.outcome,
      { score: result.score, scoredBy: result.scoredBy, reason: result.reason },
      lead.vertical,
    );

    // Dograh's QA node grades the call (DEAD_AIR, HEARING_ISSUES, READ_OPTION_LIST,
    // GUESSED_MISHEARD, ...). Storing it here is what turns the dashboard into a
    // feedback loop: the Reports page aggregates these tags so recurring agent
    // faults are visible instead of having to be found by listening to calls.
    const qa = extractQaVerdict(raw?.qa ?? flat.qa ?? raw?.annotations);
    if (qa) gatheredContext.qa = qa;
    const calledAt = callTime ?? new Date().toISOString();
    const nextRetryCount = (lead.retry_count ?? 0) + 1;

    // --- Insert the call log ----------------------------------------------
    // This insert is the idempotency barrier. If a unique index exists on
    // dograh_run_id, a concurrent duplicate delivery fails here with 23505 and we
    // stop - so the lead is never scored or incremented twice.
    const { error: insertError } = await supabase.from('call_logs').insert(
      await withProvider(supabase, {
        lead_id: lead.id,
        campaign_run_id: lead.campaign_run_id ?? null,
        dograh_run_id: runId,
        attempt_no: nextRetryCount,
        outcome: result.outcome,
        duration: result.durationSeconds,
        recording_url: recording,
        transcript_url: transcript,
        gathered_context: gatheredContext,
        cost_info: raw?.cost_info ?? {},
        called_at: calledAt,
      }),
    );

    if (insertError) {
      if (insertError.code === '23505') {
        return NextResponse.json({ message: 'Already processed', duplicate: true }, { status: 200 });
      }
      // The log is our audit trail; if it cannot be written, do not mutate the lead.
      console.error('[webhook] failed to insert call_log', insertError);
      return NextResponse.json({ error: 'Failed to record call' }, { status: 500 });
    }

    // --- Update the lead --------------------------------------------------
    const maxRetries = await getMaxRetries(supabase);
    const status = leadStatusFor(result.outcome, nextRetryCount, maxRetries);

    // Append-only notes previously grew without bound across retries.
    const noteLine = buildNoteLine(signals, {
      prefix: `[${new Date(calledAt).toISOString().slice(0, 16).replace('T', ' ')}]`,
      attempt: nextRetryCount,
      outcome: result.outcome,
      score: result.answered ? result.score : null,
    });

    const notes = [lead.notes, noteLine].filter(Boolean).join('\n').slice(-MAX_NOTES_LENGTH);

    const update: Record<string, any> = {
      status,
      call_outcome: result.outcome,
      last_attempt_at: calledAt,
      retry_count: nextRetryCount,
      notes,
    };

    // Only overwrite scoring fields when the call actually produced signal, so
    // neither a later unanswered retry nor an answered-but-empty call (customer
    // picks up and hangs up immediately) can wipe a good score from an earlier
    // real conversation. A lead that has never been scored still gets its first
    // score so nothing is left blank.
    const carriesSignal = hasQualificationSignal(signals);
    // "Has this lead ever been scored?" — read off qual_data, NOT off score.
    //
    // score was the obvious flag and it was the wrong one: leads are created by
    // the n8n import, which writes score = 0 rather than leaving it null, so
    // `score === null` was false for all 467 leads in the database and this
    // whole branch had never once run. The visible symptom was a call that
    // connects, gets scored 25 ("talked but gave no usable answers") and carries
    // no extractable signal — the score reached call_logs.gathered_context and
    // the lead stayed at 0 with an empty qual_data, so the one bucket the client
    // is meant to ring back was invisible to them.
    //
    // qual_data is written on this exact branch and nowhere else, which makes
    // empty qual_data a faithful "never scored". It is also strictly safer than
    // treating score 0 as unscored: a lead legitimately scored 0 ("not
    // interested", "rented house") has a populated qual_data, so a later empty
    // call still cannot overwrite a real refusal.
    const neverScored =
      lead.score === null ||
      lead.score === undefined ||
      !lead.qual_data ||
      Object.keys(lead.qual_data).length === 0;

    if (result.answered && (carriesSignal || neverScored)) {
      update.score = result.score;
      update.qualification = result.qualification;
      update.qual_data = gatheredContext;
      if (recording) update.recording_url = recording;
      if (transcript) update.transcript_url = transcript;
      if (signals.budget) update.budget = signals.budget;
      // `property_type` is the loan agent's column (it holds the loan type).
      // Neither a solar nor an investing call may write into it - their own
      // answers live in qual_data, which is what the dashboard reads for those
      // leads. An investing lead's "SIP" landing here would be shown as
      // "SIP loan", the same way a solar lead once read "Own house loan".
      if (signals.loan_type && callVertical !== 'solar' && callVertical !== 'investing') {
        update.property_type = signals.loan_type;
      }
      // The solar agent's two answers, in their own columns (007_solar_fields.sql)
      // as well as in qual_data, so the solar team can query them directly.
      if (signals.house_ownership) update.house_ownership = signals.house_ownership;
      if (signals.solar_planning !== null) update.solar_planning = signals.solar_planning;
      if (signals.customer_name && !lead.name) update.name = signals.customer_name;

      // The Follow-ups page reads leads.follow_up_date; nothing used to set it, so
      // that page was permanently empty even after a customer asked for a callback.
      const followUp = parseFollowUpDate(signals.visit_date);
      if (followUp) update.follow_up_date = followUp;
    }

    // updateLead, not a bare .update(): the two solar columns only exist once
    // 007_solar_fields.sql has been run, and a missing column would otherwise
    // reject the entire update and lose the score with it.
    const { error: updateError } = await updateLead(supabase, lead.id, update);

    if (updateError) {
      console.error('[webhook] failed to update lead', updateError);
      // The call_log is already written, so report failure and let the provider retry;
      // the idempotency barrier makes that retry safe.
      return NextResponse.json({ error: 'Failed to update lead' }, { status: 500 });
    }

    // Last, so the call record and the lead are already safe: the balance moves
    // as soon as the call ends instead of on the next cron tick.
    await chargeForRun(supabase, runId);

    return NextResponse.json({
      success: true,
      lead_id: lead.id,
      outcome: result.outcome,
      score: result.answered ? result.score : null,
      status,
    });
  } catch (error) {
    console.error('[webhook] unexpected error', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}

/**
 * Convenience probe so you can confirm from a browser that the endpoint is
 * deployed and configured, without exposing whether any given secret is correct.
 */
export async function GET() {
  return NextResponse.json({
    endpoint: 'dograh call-result webhook',
    method: 'POST',
    configured: Boolean(process.env.DOGRAH_WEBHOOK_SECRET),
    accepted_headers: ['X-API-Key', 'X-Webhook-Secret', 'Authorization: Bearer'],
  });
}
