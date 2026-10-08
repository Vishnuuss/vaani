import { NextResponse } from 'next/server';
import { createBillingClient, isBillingConfigured } from '@/lib/supabase-billing';
import { isAuthorisedOperator, operatorNotFound } from '@/lib/operator-auth';
import {
  getBillingConfig, isBillableCall, billableSecondsFor, creditsForSeconds,
  estimateProviderCost, toCredits, type MeteredCall,
} from '@/lib/billing';

export const dynamic = 'force-dynamic';

const PAGE = 1000;
const MAX_ROWS = 50_000;

function istDay(iso: string): string {
  return new Date(new Date(iso).getTime() + 5.5 * 3600_000).toISOString().slice(0, 10);
}

/**
 * Unit economics. OPERATOR ONLY.
 *
 * Everything here — provider cost, margin, the rate card — is the data that
 * must never reach the client. It is computed from call_usage rather than the
 * ledger so it covers calls that have been metered but not yet billed.
 */
export async function GET(request: Request) {
  if (!isAuthorisedOperator(request)) return operatorNotFound();
  if (!isBillingConfigured()) {
    return NextResponse.json({ error: 'Billing is not configured.' }, { status: 503 });
  }

  try {
    const url = new URL(request.url);
    const days = Math.min(365, Math.max(1, Number(url.searchParams.get('days')) || 30));
    const since = new Date(Date.now() - days * 86_400_000).toISOString();

    const billing = createBillingClient();
    const config = await getBillingConfig(billing);

    const rows: any[] = [];
    for (let from = 0; from < MAX_ROWS; from += PAGE) {
      const { data, error } = await billing
        .from('call_usage')
        .select('*')
        .gte('called_at', since)
        .order('called_at', { ascending: false })
        .range(from, from + PAGE - 1);
      if (error) throw new Error(error.message);
      if (!data || data.length === 0) break;
      rows.push(...data);
      if (data.length < PAGE) break;
    }

    let revenueMilli = 0, costMilli = 0, billableCalls = 0, actualSeconds = 0, billedSeconds = 0;
    let withUsage = 0;
    const daily = new Map<string, { revenue: number; cost: number; calls: number }>();
    const providers = { llm: 0, tts: 0, stt: 0, telephony: 0, overhead: 0 };
    const unpriced = new Set<string>();
    const estimated = new Set<string>();
    const losers: any[] = [];
    let testCalls = 0;
    // Which models were ACTUALLY used, read off the call records. Deriving this
    // from the rate card instead would be wrong: the rate card lists every model
    // we have a price for, not the one running. That is precisely how a switch
    // to the cheaper model gets believed without ever having taken effect.
    const modelsInUse: Record<string, Set<string>> = {
      llm: new Set(), tts: new Set(), stt: new Set(), telephony: new Set(),
    };

    // Raw units, as each provider counts them. This is the only honest way to
    // check what we are actually being charged: no provider here exposes a
    // billing API we can read (Dograh reports no money at all on a self-hosted
    // instance, and Vobiz only ever calls us), so reconciliation means holding
    // these totals up against the invoice each provider sends.
    const measured = {
      talk_seconds: 0,
      telephony_billed_seconds: 0,
      llm_input_tokens: 0,
      llm_output_tokens: 0,
      tts_characters: 0,
    };

    for (const raw of rows) {
      const call = raw as MeteredCall;
      if (!isBillableCall(call).billable) { testCalls += 1; continue; }

      billableCalls += 1;
      const secs = billableSecondsFor(call, config);
      const revenue = creditsForSeconds(secs, config);
      revenueMilli += revenue;
      billedSeconds += secs;
      actualSeconds += Number(call.duration_seconds) || 0;

      if (call.call_mode) modelsInUse.telephony.add(call.call_mode);

      // The carrier bills whole minutes, so this is what Vobiz should invoice —
      // NOT the raw talk time, which is always lower.
      measured.talk_seconds += Number(call.duration_seconds) || 0;
      measured.telephony_billed_seconds += secs;

      let cost = 0;
      if (call.usage_info) {
        // Keys look like "GroqLLMService#7|||llama-3.3-70b-versatile".
        for (const kind of ['llm', 'tts', 'stt'] as const) {
          for (const key of Object.keys((call.usage_info as any)[kind] ?? {})) {
            const model = key.split('|||')[1];
            if (model) modelsInUse[kind].add(model);
          }
        }

        for (const v of Object.values((call.usage_info as any).llm ?? {})) {
          if (v && typeof v === 'object') {
            measured.llm_input_tokens += Number((v as any).prompt_tokens) || 0;
            measured.llm_output_tokens += Number((v as any).completion_tokens) || 0;
          }
        }
        for (const v of Object.values((call.usage_info as any).tts ?? {})) {
          measured.tts_characters += Number(v) || 0;
        }

        const c = estimateProviderCost(call, config.rateCard);
        cost = c.totalMilli;
        costMilli += cost;
        withUsage += 1;
        for (const k of Object.keys(providers) as (keyof typeof providers)[]) {
          providers[k] += (c.breakdown as any)[k] ?? 0;
        }
        c.unpricedModels.forEach((m) => unpriced.add(m));
        c.estimatedComponents.forEach((m) => estimated.add(m));

        if (cost >= revenue) {
          losers.push({
            dograh_run_id: call.dograh_run_id,
            called_at: raw.called_at,
            actual_seconds: Number(call.duration_seconds) || 0,
            revenue_credits: toCredits(revenue),
            cost_credits: toCredits(cost),
            loss_credits: toCredits(cost - revenue),
            breakdown: c.breakdown,
          });
        }
      }

      const day = istDay(raw.called_at);
      const d = daily.get(day) ?? { revenue: 0, cost: 0, calls: 0 };
      d.revenue += revenue; d.cost += cost; d.calls += 1;
      daily.set(day, d);
    }

    const actualMinutes = actualSeconds / 60;
    const marginMilli = revenueMilli - costMilli;

    return NextResponse.json({
      range_days: days,
      coverage: {
        billable_calls: billableCalls,
        test_calls_excluded: testCalls,
        calls_with_cost_data: withUsage,
        // Cost is only known for calls Dograh reported usage on, so the margin
        // is understated when this is below billable_calls. Surfaced rather
        // than hidden, so the number is never trusted more than it deserves.
        cost_data_complete: withUsage === billableCalls,
      },
      revenue: {
        credits: toCredits(revenueMilli),
        billed_minutes: Math.round(billedSeconds / 60),
        actual_minutes: Math.round(actualMinutes * 10) / 10,
        // The gap between these two is the rounding uplift — currently the
        // main thing keeping short calls profitable.
        rounding_uplift_pct: actualMinutes > 0
          ? Math.round(((billedSeconds / 60) / actualMinutes - 1) * 100) : 0,
      },
      cost: {
        credits: toCredits(costMilli),
        by_provider: Object.fromEntries(
          Object.entries(providers).map(([k, v]) => [k, toCredits(v)]),
        ),
        per_actual_minute: actualMinutes > 0 ? toCredits(Math.round(costMilli / actualMinutes)) : 0,
      },
      margin: {
        credits: toCredits(marginMilli),
        pct: revenueMilli > 0 ? Math.round((marginMilli / revenueMilli) * 100) : 0,
        per_billed_minute: billedSeconds > 0
          ? toCredits(Math.round(marginMilli / (billedSeconds / 60))) : 0,
      },
      rate: {
        credits_per_minute: config.rateMilliPerMinute / 1000,
        // Below this, a full minute of talk time costs more than it earns.
        breakeven_cost_per_minute: config.rateMilliPerMinute / 1000,
      },
      daily: [...daily.entries()]
        .map(([date, v]) => ({
          date,
          revenue_credits: toCredits(v.revenue),
          cost_credits: toCredits(v.cost),
          margin_credits: toCredits(v.revenue - v.cost),
          calls: v.calls,
        }))
        .sort((a, b) => a.date.localeCompare(b.date)),
      loss_making_calls: losers.sort((a, b) => b.loss_credits - a.loss_credits).slice(0, 25),
      warnings: {
        unpriced_models: [...unpriced],
        estimated_components: [...estimated],
      },
      models_in_use: Object.fromEntries(
        Object.entries(modelsInUse).map(([k, v]) => [k, [...v]]),
      ),
      // Hold these against each provider's invoice. They are counted, not
      // estimated — the estimate only enters when they are multiplied by the
      // rate card.
      measured_usage: {
        talk_minutes: Math.round((measured.talk_seconds / 60) * 10) / 10,
        telephony_billed_minutes: Math.round(measured.telephony_billed_seconds / 60),
        llm_input_tokens: measured.llm_input_tokens,
        llm_output_tokens: measured.llm_output_tokens,
        tts_characters: measured.tts_characters,
        // Deepgram bills on audio length and reports nothing back to us, so the
        // only figure we can offer is the talk time it processed.
        stt_minutes: Math.round((measured.talk_seconds / 60) * 10) / 10,
      },
      rate_card: config.rateCard,
    });
  } catch (err: any) {
    console.error('[operator/economics] failed', err?.message);
    return NextResponse.json({ error: 'Could not compute economics.' }, { status: 500 });
  }
}
