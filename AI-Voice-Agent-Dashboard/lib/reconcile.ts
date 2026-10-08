import type { SupabaseClient } from '@supabase/supabase-js';
import { getMaxRetries } from '@/lib/call-behavior';
import { dograh } from '@/lib/dograh';
import { callProvider, findExistingCallLog, hasProviderColumn, withProvider } from '@/lib/call-provider';
import { updateLead } from '@/lib/lead-update';
import { leadStatusFor, scoreCall } from '@/lib/call-scoring';
import {
  buildGatheredContext,
  buildNoteLine,
  extractCallSignals,
  extractQaVerdict,
  hasQualificationSignal,
  parseFollowUpDate,
  usableMediaUrl,
} from '@/lib/call-context';
import { DEFAULT_VERTICAL, isWrongVerticalMatch, parseVertical } from '@/lib/verticals';
import type { DograhRunRecord } from '@/types';

const MAX_NOTES_LENGTH = 2_000;

/** jsonb arrives as an object normally and as a string on some paths; accept both. */
function parseJsonColumn(value: unknown): Record<string, any> {
  if (!value) return {};
  if (typeof value === 'object') return value as Record<string, any>;
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value);
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch {
      return {};
    }
  }
  return {};
}

/**
 * The recording and transcript links for a run, which the campaign-runs LIST
 * cannot give us.
 *
 * Dograh puts a bare storage key in `recording_url` ("recordings/400.wav") and
 * the only fetchable link in `recording_public_url`. The catch, verified on
 * runs 398 and 400: the LIST endpoint this sweep reads returns
 * `recording_public_url: null` AND `public_access_token: null`, while the
 * per-run fetch returns real links for the same call. So every reconciled call
 * stored NULL for both, and the dashboard showed no recording and no transcript
 * for any call it had recovered — which is every call on the agents whose
 * published workflow has no webhook node.
 *
 * The token cannot be reconstructed locally, so there is no way to build the URL
 * without asking for the run. One extra request per NEW call only: this runs
 * after the duplicate check, so a steady state sweep makes none.
 */
async function resolveMediaUrls(
  run: Record<string, any>,
): Promise<{ recording: string | null; transcript: string | null }> {
  let recording = usableMediaUrl(run.recording_public_url) ?? usableMediaUrl(run.recording_url);
  let transcript = usableMediaUrl(run.transcript_public_url) ?? usableMediaUrl(run.transcript_url);
  if (recording && transcript) return { recording, transcript };

  // Nothing to go and fetch if the call produced neither artefact.
  if (!run.recording_url && !run.transcript_url) return { recording, transcript };

  const workflowId = Number(run.workflow_id);
  const runId = Number(run.id);
  if (!Number.isFinite(workflowId) || !Number.isFinite(runId)) return { recording, transcript };

  try {
    const full = await dograh.getWorkflowRun(workflowId, runId);
    recording =
      recording ?? usableMediaUrl(full?.recording_public_url) ?? usableMediaUrl(full?.recording_url);
    transcript =
      transcript ?? usableMediaUrl(full?.transcript_public_url) ?? usableMediaUrl(full?.transcript_url);
  } catch (err) {
    // A missing link must never cost us the call log itself.
    console.warn('[reconcile] could not fetch run detail for media urls', { runId, err });
  }
  return { recording, transcript };
}

/**
 * Completes a call log that was written before the provider had finished.
 *
 * Vaani's webhook node fires at hang-up. When `perform_final_variable_extraction`
 * has not finished yet, every `{{gathered_context.X}}` in the payload renders
 * empty and `{{cost_info.call_duration_seconds}}` renders 0 - and that is what
 * gets stored as the record of the call. It is a race, so it hits some calls and
 * not others, which is exactly what makes it hard to notice.
 *
 * Run 798 (solar, 2026-09-06) is the case this was written for. Vaani had a
 * 70-second call with `house_ownership: own`, `solar_planning: true`,
 * `lead_score: 100`, a recording and a transcript. The dashboard had duration 0,
 * no media, no answers, and a lead scored 0 - a qualified customer presented to
 * the client as a dud.
 *
 * TWO kinds of repair happen here, and the distinction matters:
 *
 *  - FACTS (duration, media links) are filled whenever they are missing. Safe to
 *    re-evaluate on any tick because they are copied, not judged.
 *
 *  - The QUALIFICATION (answers, score, lead columns) is filled at most ONCE,
 *    and only when the stored row carries no qualification at all while the run
 *    now does. That guard is what keeps migration 006 from coming back: after it
 *    fires the row has a signal, so it can never fire again. `attempt_no`,
 *    `retry_count` and the notes line are never touched here - re-counting
 *    attempts on every ten-minute tick is what produced 47 log rows and
 *    retry_count 31 for 3 real calls.
 *
 * A call where the provider itself extracted nothing (runs 793 and 795: every
 * variable came back as "") is left alone. There is nothing to complete, and
 * inventing a score would be worse than an honest blank.
 */
async function backfillIncompleteLog(
  supabase: SupabaseClient,
  existing: Record<string, any>,
  run: Record<string, any>,
): Promise<'duplicate' | 'backfilled'> {
  const patch: Record<string, any> = {};

  // --- Facts --------------------------------------------------------------
  const realDuration = Number(run?.cost_info?.call_duration_seconds ?? run?.duration ?? 0);
  if (Number.isFinite(realDuration) && realDuration > 0 && !(Number(existing.duration) > 0)) {
    patch.duration = Math.round(realDuration);
  }

  if (!usableMediaUrl(existing.recording_url) || !usableMediaUrl(existing.transcript_url)) {
    const media = await resolveMediaUrls(run);
    if (media.recording && !usableMediaUrl(existing.recording_url)) patch.recording_url = media.recording;
    if (media.transcript && !usableMediaUrl(existing.transcript_url)) patch.transcript_url = media.transcript;
  }

  // --- Qualification, once ------------------------------------------------
  const storedContext = parseJsonColumn(existing.gathered_context);
  const storedSignals = extractCallSignals(storedContext);
  const runSignals = extractCallSignals(run as Record<string, any>);

  const alreadyQualified = hasQualificationSignal(storedSignals);
  const runHasAnswers = hasQualificationSignal(runSignals);
  const completing = !alreadyQualified && runHasAnswers;

  let leadPatch: Record<string, any> | null = null;

  if (completing) {
    const runContext: Record<string, any> = run.gathered_context ?? {};
    const initial: Record<string, any> = run.initial_context ?? {};
    const callVertical =
      parseVertical(runContext.vertical ?? initial.vertical ?? storedContext.vertical) ?? DEFAULT_VERTICAL;

    const result = scoreCall({
      vertical: callVertical,
      house_ownership: runSignals.house_ownership,
      solar_planning: runSignals.solar_planning,
      currently_investing: runSignals.currently_investing,
      investment_type: runSignals.investment_type,
      interested: runSignals.interested,
      budget: runSignals.budget,
      visit_date: runSignals.visit_date,
      loan_type: runSignals.loan_type,
      profession: runSignals.profession,
      do_not_call: runSignals.do_not_call,
      outcome:
        run.status ??
        runContext.mapped_call_disposition ??
        runContext.call_disposition ??
        (run.is_completed ? 'completed' : undefined),
      duration: realDuration || existing.duration,
    });

    patch.gathered_context = {
      ...storedContext,
      ...runContext,
      ...buildGatheredContext(runSignals, result.outcome, undefined, callVertical),
    };

    if (result.answered) {
      leadPatch = {
        score: result.score,
        qualification: result.qualification,
        qual_data: patch.gathered_context,
      };
      if (runSignals.budget) leadPatch.budget = runSignals.budget;
      if (runSignals.loan_type && callVertical !== 'solar') leadPatch.property_type = runSignals.loan_type;
      if (runSignals.house_ownership) leadPatch.house_ownership = runSignals.house_ownership;
      if (runSignals.solar_planning !== null) leadPatch.solar_planning = runSignals.solar_planning;
      const followUp = parseFollowUpDate(runSignals.visit_date);
      if (followUp) leadPatch.follow_up_date = followUp;
      if (patch.recording_url) leadPatch.recording_url = patch.recording_url;
      if (patch.transcript_url) leadPatch.transcript_url = patch.transcript_url;
    }
  }

  if (Object.keys(patch).length === 0) return 'duplicate';

  const { error } = await supabase.from('call_logs').update(patch).eq('id', existing.id);
  if (error) {
    console.error('[reconcile] could not backfill call_log', { id: existing.id, error });
    return 'duplicate';
  }

  // The lead is updated only after the log write succeeded, so the two can never
  // disagree about whether this call was completed.
  if (leadPatch && existing.lead_id) {
    const { error: leadError } = await supabase.from('leads').update(leadPatch).eq('id', existing.lead_id);
    if (leadError) {
      console.error('[reconcile] could not complete lead', { lead: existing.lead_id, error: leadError });
    }
  }

  console.info('[reconcile] backfilled call_log', {
    id: existing.id,
    fields: Object.keys(patch),
    completedQualification: completing,
  });
  return 'backfilled';
}

/**
 * Applies a Dograh run record to our database, using exactly the same scoring and
 * column names as the webhook handler.
 *
 * The cron previously carried its own copy of the scoring rules and wrote to
 * columns that did not exist (`direction`, `gathered_data`), while ignoring every
 * insert error - so reconciliation appeared to succeed while saving nothing.
 */
export async function applyRunResult(
  supabase: SupabaseClient,
  run: DograhRunRecord & Record<string, any>,
  campaignRunId?: string | null,
  // Read once per sweep by the caller and threaded through, rather than
  // re-querying settings for every run record in a large campaign.
  maxRetries?: number,
): Promise<'inserted' | 'duplicate' | 'backfilled' | 'no_lead' | 'error'> {
  const runId = run.id;
  if (runId == null) return 'error';

  // `.limit(1)` + array, NOT `.maybeSingle()`. maybeSingle ERRORS when more than
  // one row matches, and the error was being discarded - so `data` came back
  // null, the run looked unseen, and every sweep inserted yet another copy and
  // incremented retry_count again. Once a run had two rows it grew without
  // bound: one live lead reached 47 log rows and retry_count 31 for 3 real calls.
  // The unique index (scripts/006) prevents duplicates from arising at all; this
  // makes the check correct even if one ever slips through.
  // Scoped by calling backend: Vaani run 464 and voice run 464 are different
  // calls. Unscoped, the first Vaani id that an old voice row already holds
  // makes a real call look like a redelivery and it is dropped. See
  // lib/call-provider.ts.
  const { found, row: existingLog } = await findExistingCallLog(supabase, runId);
  if (found) {
    // A row already exists, but it is not necessarily RIGHT.
    //
    // The webhook fires at hang-up, which can be before the provider has
    // finalised the duration or finished uploading the recording. Run 571 landed
    // that way on 2026-09-04: a 29-second call stored as `duration: 0` with no
    // recording and no transcript, and because this function returned early the
    // sweep never corrected it. The dashboard showed a real conversation as a
    // zero-second call for ever.
    //
    // Facts are repaired whenever they are missing; the qualification is
    // completed at most once. See backfillIncompleteLog.
    return backfillIncompleteLog(supabase, existingLog!, run as Record<string, any>);
  }

  const context: Record<string, any> = run.gathered_context ?? {};
  // The CSV row this call was dialled from. THIS is where Dograh puts the
  // identifiers - verified 2026-08-06 against a live record from the same
  // endpoint this sweep reads (GET /api/v1/campaign/{id}/runs):
  //
  //   metadata          -> null
  //   gathered_context  -> {call_disposition, call_id, call_tags,
  //                         mapped_call_disposition, provider}   (no lead_id)
  //   phone_number/phone at top level -> absent
  //   initial_context   -> {phone_number, customer_name, lead_id, campaign_id, ...}
  //
  // Reading only the first three meant leadId and phone were BOTH null on every
  // run, so this function returned 'no_lead' every time and the missed-call
  // sweep had never recovered a single call.
  const initial: Record<string, any> = (run as any).initial_context ?? {};
  const leadId = run.metadata?.lead_id ?? context.lead_id ?? initial.lead_id ?? null;
  const phone = run.phone_number ?? (run as any).phone ?? initial.phone_number ?? null;

  let lead: any = null;
  if (leadId) {
    const { data } = await supabase.from('leads').select('*').eq('id', leadId).maybeSingle();
    lead = data;
  }
  if (!lead && phone) {
    const { data } = await supabase
      .from('leads')
      .select('*')
      .eq('phone', phone)
      .order('last_attempt_at', { ascending: false, nullsFirst: false })
      .limit(1);
    const candidate = data?.[0] ?? null;

    // A phone match is a GUESS once the same number can be a lead in more than
    // one business line. If the agent named the business line it was calling
    // for and it disagrees with this lead's, drop the match rather than write a
    // solar call's result onto a loan lead. Inert until both sides carry a
    // vertical - see isWrongVerticalMatch.
    const expectedVertical = context.vertical ?? initial.vertical ?? null;
    if (candidate && isWrongVerticalMatch(candidate.vertical, expectedVertical)) {
      console.warn(
        '[reconcile] phone matched a lead in a different business line; refusing it',
        { runId, leadVertical: candidate.vertical, expected: expectedVertical },
      );
    } else {
      lead = candidate;
    }
  }

  if (!lead) return 'no_lead';

  // Same field mapping as the webhook: the loan workflow reports loan_required /
  // loan_amount / loan_type, not interested / budget / visit_date.
  const signals = extractCallSignals(run as Record<string, any>);

  // Which ladder to score on, same precedence as the webhook: what the agent
  // said it was calling for, then the lead's own column.
  const callVertical = parseVertical(context.vertical ?? initial.vertical) ?? parseVertical(lead.vertical) ?? DEFAULT_VERTICAL;

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
    // Dograh run records carry no `status` at all: the outcome lives in
    // gathered_context.call_disposition ("user_hangup", "no_answer", ...) and
    // completion is reported via is_completed.
    outcome:
      run.status ??
      context.mapped_call_disposition ??
      context.call_disposition ??
      ((run as any).is_completed ? 'completed' : undefined),
    duration: (run as any).cost_info?.call_duration_seconds ?? run.duration,
  });

  const gatheredContext = { ...context, ...buildGatheredContext(signals, result.outcome, undefined, lead.vertical) };

  // Same QA verdict the webhook stores, recovered here for any call whose
  // webhook delivery was missed.
  const qa = extractQaVerdict((run as any).annotations);
  if (qa) gatheredContext.qa = qa;

  const nextRetryCount = (lead.retry_count ?? 0) + 1;
  const calledAt = run.created_at ?? run.started_at ?? new Date().toISOString();

  // Resolved once and reused by the lead update below, so a single run never
  // costs two detail fetches.
  const media = await resolveMediaUrls(run as Record<string, any>);

  const { error: insertError } = await supabase.from('call_logs').insert(
    await withProvider(supabase, {
      lead_id: lead.id,
      campaign_run_id: campaignRunId ?? lead.campaign_run_id ?? null,
      dograh_run_id: runId,
      attempt_no: nextRetryCount,
      outcome: result.outcome,
      duration: result.durationSeconds,
      // Dograh's run records carry storage keys ("recordings/23.wav") in these
      // fields and only a real link in the *_public_url variants, which the LIST
      // endpoint omits entirely - see resolveMediaUrls.
      recording_url: media.recording,
      transcript_url: media.transcript,
      gathered_context: gatheredContext,
      cost_info: (run as any).cost_info ?? {},
      called_at: calledAt,
    }),
  );

  if (insertError) {
    if (insertError.code === '23505') return 'duplicate';
    console.error('[reconcile] call_log insert failed', insertError);
    return 'error';
  }

  const noteLine = buildNoteLine(signals, {
    prefix: `[reconciled ${new Date().toISOString().slice(0, 16).replace('T', ' ')}]`,
    attempt: nextRetryCount,
    outcome: result.outcome,
    score: result.answered ? result.score : null,
  });

  const update: Record<string, any> = {
    status: leadStatusFor(result.outcome, nextRetryCount, maxRetries ?? 2),
    call_outcome: result.outcome,
    last_attempt_at: calledAt,
    retry_count: nextRetryCount,
    notes: [lead.notes, noteLine].filter(Boolean).join('\n').slice(-MAX_NOTES_LENGTH),
  };

  // Same guard as the webhook: an answered-but-empty call must not overwrite the
  // score a real conversation already produced.
  const carriesSignal = hasQualificationSignal(signals);
  const neverScored = lead.score === null || lead.score === undefined;

  if (result.answered && (carriesSignal || neverScored)) {
    update.score = result.score;
    update.qualification = result.qualification;
    update.qual_data = gatheredContext;
    if (media.recording) update.recording_url = media.recording;
    if (media.transcript) update.transcript_url = media.transcript;
    if (signals.budget) update.budget = signals.budget;
    // Never on a solar call: property_type holds the LOAN type, and the solar
    // answers belong in qual_data where the dashboard reads them.
    if (signals.loan_type && callVertical !== 'solar') update.property_type = signals.loan_type;
    // Solar's two answers get their own columns too (007_solar_fields.sql).
    if (signals.house_ownership) update.house_ownership = signals.house_ownership;
    if (signals.solar_planning !== null) update.solar_planning = signals.solar_planning;

    const followUp = parseFollowUpDate(signals.visit_date);
    if (followUp) update.follow_up_date = followUp;
  }

  // Same missing-column guard as the webhook - see lib/lead-update.ts.
  const { error: updateError } = await updateLead(supabase, lead.id, update);
  if (updateError) {
    console.error('[reconcile] lead update failed', updateError);
    return 'error';
  }

  return 'inserted';
}

/** Authorises a cron request from either Vercel Cron or a manual trigger. */
export function isAuthorisedCron(request: Request): boolean {
  const secret = process.env.CRON_SECRET;
  if (!secret) return false;
  // Vercel Cron sends `Authorization: Bearer <CRON_SECRET>`; the old code only
  // accepted `x-cron-secret`, so scheduled invocations were always rejected.
  const auth = request.headers.get('authorization');
  if (auth === `Bearer ${secret}`) return true;
  return request.headers.get('x-cron-secret') === secret;
}

/**
 * Finds call logs that were written incomplete and completes them from their run.
 *
 * The campaign loop in the cron only visits campaigns that are still
 * queued/running/paused. On 2026-09-06, 101 of 113 campaign_runs were
 * `completed` — and a completed campaign is never revisited, so a row the
 * webhook wrote thin under it could never be repaired. Run 798 sat at duration 0
 * with a score of 0 for a 70-second call that had qualified at 100.
 *
 * So this looks for the damage directly rather than hoping a campaign is still
 * open. A row is a candidate when it has no talk time, no recording, or no
 * transcript. Rows with all three are already whole and are not fetched.
 *
 * Bounded on both sides: only recent calls, and a hard cap per tick, so a long
 * history can never turn one tick into a crawl. Anything not reached this time
 * is reached on the next one.
 */
export async function repairIncompleteCallLogs(
  supabase: SupabaseClient,
  opts: { sinceDays?: number; limit?: number } = {},
): Promise<{ scanned: number; repaired: number; skipped: number; errors: number }> {
  const { sinceDays = 7, limit = 200 } = opts;
  const out = { scanned: 0, repaired: 0, skipped: 0, errors: 0 };

  const since = new Date(Date.now() - sinceDays * 86_400_000).toISOString();

  let query = supabase
    .from('call_logs')
    .select('id, lead_id, dograh_run_id, duration, recording_url, transcript_url, gathered_context, called_at')
    .gte('called_at', since)
    .not('dograh_run_id', 'is', null)
    .or('duration.is.null,duration.eq.0,recording_url.is.null,transcript_url.is.null');

  // ONLY rows from the backend we are about to ask. A run id identifies a call
  // just within one backend, and this pass repairs a row from whatever run comes
  // back for that id - so without this filter it is a data-corruption bug
  // waiting for a clock to tick.
  //
  // Today the old backend's rows (runs 2091-2097) merely 404 on Vaani, because
  // Vaani is only at ~800. When Vaani reaches 2091 they would stop 404-ing and
  // start returning a DIFFERENT customer's call, which this pass would then
  // merge into an August row. Same collision as scripts/009; it has to be
  // handled everywhere a run id is used as an identity, not just at insert.
  if (await hasProviderColumn(supabase, 'call_logs')) {
    query = query.eq('provider', callProvider());
  }

  const { data: rows, error } = await query
    .order('called_at', { ascending: false })
    .limit(limit);

  if (error) {
    console.error('[reconcile] could not list incomplete call logs', error);
    return { ...out, errors: 1 };
  }

  for (const row of rows ?? []) {
    out.scanned += 1;
    const runId = Number(row.dograh_run_id);
    if (!Number.isFinite(runId)) {
      out.skipped += 1;
      continue;
    }

    try {
      // The workflow id is not stored on the log, and it does not need to be:
      // Vaani resolves a run by id and ignores the workflow in the path
      // (verified on run 798). The configured loan workflow is a valid door.
      const workflowId = Number(process.env.DOGRAH_WORKFLOW_ID ?? 1);
      const run = await dograh.getWorkflowRun(workflowId, runId);

      // Guard against ever repairing a row from the wrong call. Run ids repeat
      // across backends, and this row may predate the cutover.
      if (!run || Number(run.id) !== runId) {
        out.skipped += 1;
        continue;
      }
      // A call still in progress has nothing to give yet.
      if (!run.is_completed) {
        out.skipped += 1;
        continue;
      }

      const verdict = await backfillIncompleteLog(supabase, row as Record<string, any>, run);
      if (verdict === 'backfilled') out.repaired += 1;
      else out.skipped += 1;
    } catch (err: any) {
      // A 404 means "no such run on this backend", which is a normal outcome for
      // a row this pass should not have been asked about - not a failure. Left
      // in `errors` it would report six permanent errors every tick and hide a
      // real one.
      if (err?.status === 404) {
        out.skipped += 1;
        continue;
      }
      console.warn('[reconcile] repair failed for one call log', { run: runId, err });
      out.errors += 1;
    }
  }

  if (out.repaired > 0) console.info('[reconcile] repaired incomplete call logs', out);
  return out;
}
