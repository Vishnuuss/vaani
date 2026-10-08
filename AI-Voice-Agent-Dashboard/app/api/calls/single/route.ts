import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { DograhApiError, DograhClient, DograhConfigError, dograh } from '@/lib/dograh';
import { buildCampaignCsv } from '@/lib/csv-builder';
import { createBillingClient, isBillingConfigured } from '@/lib/supabase-billing';
import { getBillingConfig, toCredits } from '@/lib/billing';
import { DEFAULT_VERTICAL, parseVertical, VERTICAL_LABELS } from '@/lib/verticals';
import { noAgentMessage, workflowIdFor } from '@/lib/workflow-routing';

/**
 * Call ONE person, now.
 *
 * There is no "dial one number" API — Dograh only dials campaigns — so a manual
 * call is placed as a one-person campaign. That is deliberate rather than a
 * workaround: it reuses the credit guard, the lead claim, the provider ids and
 * the `/api/webhook/call-result` write-back that already work in production, so
 * a manual call lands in `leads` and `call_logs` identically to a campaign call.
 * `buildCampaignCsv` already carries `lead_id` and `vertical`, which is what
 * makes the write-back attach to the right lead with no changes there.
 *
 * TWO THINGS DIFFER FROM A CAMPAIGN, both intentional:
 *
 *  1. `max_retries: 0`. A manual call dials ONCE. A campaign may redial; a human
 *     pressing a button expects exactly one call to happen.
 *  2. The retry ceiling is NOT applied. The ceiling exists to stop the automatic
 *     campaign calling someone over and over. A deliberate, confirmed, one-person
 *     decision by an operator overrides it — the UI shows "already called N
 *     times" so the choice is informed rather than accidental.
 *
 * The credit guard is NOT bypassed and must not be.
 */

/** Statuses that mean a campaign already has hold of this lead. */
const IN_FLIGHT = ['pending', 'queued', 'running', 'paused'];

/** Digits, optionally with a leading +. Deliberately permissive about spacing/dashes. */
function normalisePhone(raw: unknown): string | null {
  const s = String(raw ?? '').trim();
  if (!s) return null;
  const cleaned = s.replace(/[\s()\-.]/g, '');
  if (!/^\+?\d{7,15}$/.test(cleaned)) return null;
  // Indian numbers are stored with the +91 country code throughout this database;
  // a bare 10-digit number typed into the form must match the same lead that an
  // import created, or the "already exists" check silently misses and the person
  // gets a duplicate record.
  if (/^\d{10}$/.test(cleaned)) return `+91${cleaned}`;
  return cleaned.startsWith('+') ? cleaned : `+${cleaned}`;
}

export async function POST(request: Request) {
  if (!DograhClient.isConfigured()) {
    return NextResponse.json(
      { error: 'Calling provider is not configured. Set DOGRAH_API_KEY to place calls.' },
      { status: 503 },
    );
  }

  const supabase = createServerClient();

  // Tracked so the failure path knows exactly what to undo.
  let campaignRunId: string | null = null;
  let claimedLeadId: string | null = null;
  let priorLeadState: { status: string; campaign_run_id: string | null } | null = null;
  let dograhCampaignId: number | null = null;
  let campaignStarted = false;

  try {
    const body = await request.json().catch(() => null);
    if (!body || typeof body !== 'object') {
      return NextResponse.json({ error: 'Invalid JSON body' }, { status: 400 });
    }

    // --- Step 1: resolve the lead ------------------------------------------
    let lead: any = null;

    if (body.leadId) {
      const { data, error } = await supabase
        .from('leads')
        .select('id, name, phone, city, property_type, budget, email, vertical, status, retry_count, campaign_run_id')
        .eq('id', String(body.leadId))
        .single();
      if (error || !data) {
        return NextResponse.json({ error: 'That lead no longer exists.' }, { status: 404 });
      }
      lead = data;
    } else {
      const phone = normalisePhone(body.phone);
      if (!phone) {
        return NextResponse.json(
          { error: 'Enter a valid phone number (10 digits, or with the country code).' },
          { status: 400 },
        );
      }
      // Never guessed. A wrong business line means the wrong agent, with the
      // wrong script, calls a real person — the same failure the lead import had.
      const vertical = parseVertical(body.vertical);
      if (!vertical) {
        return NextResponse.json(
          { error: 'Choose which business line is calling. It cannot be worked out from the number.' },
          { status: 400 },
        );
      }

      // Find-or-create on (phone, vertical) — the same natural key the import
      // uses, so this can never produce a second copy of someone.
      const { data: existing } = await supabase
        .from('leads')
        .select('id, name, phone, city, property_type, budget, email, vertical, status, retry_count, campaign_run_id')
        .eq('phone', phone)
        .eq('vertical', vertical)
        .maybeSingle();

      if (existing) {
        lead = existing;
        // Fill in details the operator typed that we did not already hold. Never
        // overwrite an existing value — the imported record is the better source.
        const patch: Record<string, any> = {};
        if (body.name && !existing.name) patch.name = String(body.name).slice(0, 200);
        if (body.city && !existing.city) patch.city = String(body.city).slice(0, 120);
        if (Object.keys(patch).length > 0) {
          const { data: updated } = await supabase
            .from('leads')
            .update(patch)
            .eq('id', existing.id)
            .select('id, name, phone, city, property_type, budget, email, vertical, status, retry_count, campaign_run_id')
            .single();
          if (updated) lead = updated;
        }
      } else {
        /*
         * A new lead must carry a name. `leads.name` is NOT NULL in the live
         * database, and the name is also sent to the agent as `customer_name`
         * and spoken in the greeting — so it is refused here explicitly rather
         * than left to fail as a constraint violation reported as a 500.
         */
        const providedName = body.name ? String(body.name).trim().slice(0, 200) : '';
        if (!providedName) {
          return NextResponse.json(
            { error: 'A name is required — the agent greets them by name on the call.' },
            { status: 400 },
          );
        }

        const { data: created, error: createError } = await supabase
          .from('leads')
          .insert({
            name: providedName,
            phone,
            city: body.city ? String(body.city).slice(0, 120) : null,
            vertical,
            status: 'new',
            // Marks where this person came from, so a manually added lead is
            // always distinguishable from an imported one in reports.
            source: 'manual',
          })
          .select('id, name, phone, city, property_type, budget, email, vertical, status, retry_count, campaign_run_id')
          .single();

        if (createError || !created) {
          console.error('[calls:single] could not create lead', createError);
          // The database's own words. The generic message sent every failure —
          // a duplicate, a missing column, a constraint — to the same dead end
          // with nothing to act on.
          return NextResponse.json(
            {
              error: createError?.message
                ? `Could not save this person as a lead: ${createError.message}`
                : 'Could not save this person as a lead.',
            },
            { status: 500 },
          );
        }
        lead = created;
      }
    }

    if (!lead.phone) {
      return NextResponse.json({ error: 'That lead has no phone number, so it cannot be called.' }, { status: 400 });
    }

    const vertical = parseVertical(lead.vertical) ?? DEFAULT_VERTICAL;

    // --- Step 2: refuse if a campaign already has this lead ----------------
    // Without this, a campaign and an operator can dial the same person at the
    // same moment. Confirmed by checking the campaign's real status, not just
    // the lead's, because a lead can be left `queued` by a finished run.
    //
    // Checked BEFORE the agent-configuration check below: both refuse before
    // anything is claimed, so the order is free, and "this person is already
    // being called" is the more urgent thing to say.
    if (lead.status === 'queued' && lead.campaign_run_id) {
      const { data: holder } = await supabase
        .from('campaign_runs')
        .select('id, campaign_name, status')
        .eq('id', lead.campaign_run_id)
        .maybeSingle();
      if (holder && IN_FLIGHT.includes(holder.status)) {
        return NextResponse.json(
          {
            error: `This person is already queued in the campaign "${holder.campaign_name}". Calling them now would ring them twice.`,
            code: 'ALREADY_QUEUED',
          },
          { status: 409 },
        );
      }
    }

    // Checked BEFORE the lead is claimed, so a business line with no agent
    // refuses without having touched anything.
    const workflowId = workflowIdFor(vertical);
    if (workflowId == null) {
      return NextResponse.json({ error: noAgentMessage(vertical) }, { status: 400 });
    }

    // --- Step 3: credit pre-flight -----------------------------------------
    // Enforced, never bypassed. One call at a time, so the floor is one minimum
    // charge rather than a whole parallel batch.
    if (isBillingConfigured()) {
      try {
        const billing = createBillingClient();
        const billingConfig = await getBillingConfig(billing);
        const balance = billingConfig.balanceMilli;
        const minChargeMilli = Math.round(
          (billingConfig.minimumBillableSeconds * billingConfig.rateMilliPerMinute) / 60,
        );
        if (balance < minChargeMilli) {
          return NextResponse.json(
            {
              error:
                balance <= 0
                  ? 'Out of credits. Add credits before placing a call.'
                  : `Not enough credits for one call. ${toCredits(balance)} left, one call can cost up to ${toCredits(minChargeMilli)}.`,
              code: 'INSUFFICIENT_CREDITS',
              balance_credits: toCredits(balance),
              required_credits: toCredits(minChargeMilli),
            },
            { status: 402 },
          );
        }
      } catch (billingError: any) {
        // Same stance as the campaign launcher: our own accounting outage must
        // not block the client's operations.
        console.error('[calls:single] credit pre-flight failed, allowing call', billingError?.message);
      }
    }

    const who = lead.name?.trim() || lead.phone;
    const name = `Manual call — ${who}`;

    // --- Step 4: reserve the run row FIRST ---------------------------------
    // Before anything is dialled, so a crash can never leave a live call with no
    // record of it. `manual: true` is what keeps manual calls distinguishable
    // from campaigns in history for ever.
    const { data: reserved, error: reserveError } = await supabase
      .from('campaign_runs')
      .insert({
        campaign_name: name,
        requested_count: 1,
        concurrency: 1,
        status: 'pending',
        vertical,
        target_segment: { manual: true, lead_id: lead.id },
        retry_config: { max_retries: 0, enabled: false },
        schedule_config: {},
      })
      .select()
      .single();

    if (reserveError || !reserved) {
      console.error('[calls:single] failed to reserve run row', reserveError);
      return NextResponse.json({ error: 'Could not create the call record.' }, { status: 500 });
    }
    campaignRunId = reserved.id;
    priorLeadState = { status: lead.status, campaign_run_id: lead.campaign_run_id ?? null };

    // --- Step 5: claim the lead, compare-and-set ---------------------------
    // `neq('status','queued')` is what makes a double-clicked button safe: the
    // second request updates zero rows and aborts instead of dialling again.
    //
    // Deliberately NOT filtered by the retry ceiling — a confirmed manual call
    // overrides it (see the header comment).
    const { data: claimedRows, error: claimError } = await supabase
      .from('leads')
      .update({ status: 'queued', campaign_run_id: campaignRunId })
      .eq('id', lead.id)
      .neq('status', 'queued')
      .select('id');

    if (claimError) {
      throw new Error(`Failed to claim the lead: ${claimError.message}`);
    }
    if (!claimedRows || claimedRows.length === 0) {
      await supabase.from('campaign_runs').delete().eq('id', campaignRunId);
      campaignRunId = null;
      return NextResponse.json(
        { error: 'This person was just queued by something else. No call was placed.', code: 'ALREADY_QUEUED' },
        { status: 409 },
      );
    }
    claimedLeadId = lead.id;

    // --- Step 6: build + upload the one-row CSV ----------------------------
    const csvString = buildCampaignCsv([lead], vertical);
    const filename = `manual_call_${lead.id}_${Date.now()}.csv`;
    const presigned = await dograh.getPresignedUploadUrl(filename, Buffer.byteLength(csvString, 'utf-8'));
    await dograh.uploadCsvToS3(presigned.upload_url, csvString);

    // --- Step 7: create the campaign at the provider -----------------------
    const dograhCampaign = await dograh.createCampaign({
      name,
      workflow_id: workflowId,
      source_id: presigned.file_key,
      max_concurrency: 1,
      // Reusing the run row id means a retried request cannot create a second
      // provider campaign — i.e. cannot place a second call.
      idempotencyKey: `manual-call-${campaignRunId}`,
      // ONE dial. The provider counts max_retries as dials AFTER the first, so 0
      // means exactly one call is placed and nothing is auto-redialled.
      retry_config: { max_retries: 0, enabled: false },
      circuit_breaker: { enabled: false },
    });
    dograhCampaignId = dograhCampaign.id;

    await supabase
      .from('campaign_runs')
      .update({
        dograh_campaign_id: dograhCampaignId,
        workflow_id: workflowId,
        source_id: presigned.file_key,
        actual_count: 1,
        status: 'queued',
      })
      .eq('id', campaignRunId);

    // --- Step 8: dial ------------------------------------------------------
    await dograh.startCampaign(dograhCampaignId);
    campaignStarted = true;

    const { data: run } = await supabase
      .from('campaign_runs')
      .update({ status: 'running', started_at: new Date().toISOString() })
      .eq('id', campaignRunId)
      .select()
      .single();

    return NextResponse.json({
      success: true,
      lead: { id: lead.id, name: lead.name, phone: lead.phone, vertical },
      campaign: run ?? reserved,
      dograh_campaign_id: dograhCampaignId,
      message: `Calling ${who} now with the ${VERTICAL_LABELS[vertical]} agent.`,
    });
  } catch (error: any) {
    console.error('[calls:single] failed', error);

    // Dialling already began: do NOT free the lead, it is being called. Stop the
    // provider campaign instead so it cannot keep spending.
    if (campaignStarted && dograhCampaignId != null) {
      try {
        await dograh.pauseCampaign(dograhCampaignId);
      } catch (pauseErr) {
        console.error('[calls:single] could not pause orphaned provider campaign', pauseErr);
      }
      if (campaignRunId) {
        await supabase
          .from('campaign_runs')
          .update({ status: 'paused', paused_at: new Date().toISOString() })
          .eq('id', campaignRunId);
      }
      return NextResponse.json(
        { error: 'The call started but could not be fully recorded, so it was stopped. Check the call history.' },
        { status: 500 },
      );
    }

    // Nothing was dialled: put the lead back exactly as it was.
    if (claimedLeadId && priorLeadState) {
      const { error: rollbackError } = await supabase
        .from('leads')
        .update({ status: priorLeadState.status, campaign_run_id: priorLeadState.campaign_run_id })
        .eq('id', claimedLeadId);
      if (rollbackError) console.error('[calls:single] lead rollback failed', rollbackError);
    }
    if (campaignRunId) {
      await supabase
        .from('campaign_runs')
        .update({ status: 'failed', error_message: String(error?.message ?? error).slice(0, 500) })
        .eq('id', campaignRunId);
    }

    if (error instanceof DograhConfigError) {
      return NextResponse.json({ error: error.message }, { status: 503 });
    }
    if (error instanceof DograhApiError) {
      return NextResponse.json(
        { error: 'The calling provider rejected the call.', providerStatus: error.status },
        { status: error.isClientError ? 422 : 502 },
      );
    }
    return NextResponse.json({ error: 'Could not place the call. Nobody was dialled.' }, { status: 500 });
  }
}
