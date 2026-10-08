import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { verticalFromParam } from '@/lib/verticals';

/**
 * Aggregated call-quality problems, so recurring agent faults are visible
 * instead of having to be found by listening to recordings one at a time.
 *
 * Dograh's QA node grades each call against a rubric and the verdict is stored
 * on call_logs.gathered_context.qa by the webhook and the reconcile sweep. This
 * counts the tags, keeps one example reason each, and tells you what to fix.
 */

export const dynamic = 'force-dynamic';

/** Plain-language meaning and the lever that fixes each tag. */
const TAG_GUIDE: Record<string, { label: string; fix: string }> = {
  DEAD_AIR: { label: 'Long silences', fix: 'Usually the LLM rate limit or a stalled turn — check the Groq quota.' },
  HEARING_ISSUES: { label: 'Could not hear each other', fix: 'Line quality, or the agent speaking over the caller.' },
  STT_GARBLED: { label: 'Speech misrecognised', fix: 'Add the misheard words to the Dograh dictionary (keyterms).' },
  GUESSED_MISHEARD: { label: 'Agent guessed instead of asking', fix: 'Reinforce the "ask them to repeat" rule in the prompt.' },
  READ_OPTION_LIST: { label: 'Recited a list of options', fix: 'Reinforce the one-open-question rule in the prompt.' },
  ASKED_EXTRA_QUESTION: { label: 'Asked something off-script', fix: 'Tighten the allowed-questions rule in the prompt.' },
  TWO_QUESTIONS_AT_ONCE: { label: 'Two questions in one breath', fix: 'Reinforce one question per turn.' },
  ASSISTANT_IN_LOOP: { label: 'Repeated itself', fix: 'The agent is not registering answers — check extraction.' },
  ASSISTANT_REPLY_IMPROPER: { label: 'Replied off-topic', fix: 'Often follows a misrecognition — check STT tags too.' },
  USER_FRUSTRATED: { label: 'Caller got annoyed', fix: 'Read the example calls; usually latency or repetition.' },
  USER_NOT_UNDERSTANDING: { label: 'Caller confused', fix: 'Simplify the wording of the questions.' },
  USER_DETECTS_AI: { label: 'Caller realised it is a bot', fix: 'Voice speed/volume and more natural phrasing.' },
  NOT_TELUGU: { label: 'Agent left Telugu', fix: 'Reinforce the language rule in the global prompt.' },
  ASSISTANT_LACKS_EMPATHY: { label: 'Ignored the caller', fix: 'Allow acknowledgement before the next question.' },
  UNCLEAR_CONVERSATION: { label: 'Incoherent conversation', fix: 'Review the transcript end to end.' },
  USER_REQUESTING_FEATURE: { label: 'Asked for something unsupported', fix: 'Consider adding it to the script.' },
};

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url);
    const days = Math.min(Math.max(parseInt(searchParams.get('days') || '30', 10) || 30, 1), 365);

    const since = new Date();
    since.setDate(since.getDate() - days);

    const supabase = createServerClient();
    const vertical = verticalFromParam(searchParams.get('vertical'));
    const qualityBase = vertical
      ? supabase
          .from('call_logs')
          .select('id, called_at, duration, outcome, gathered_context, lead_id, leads!inner(vertical)')
          .eq('leads.vertical', vertical)
      : supabase.from('call_logs').select('id, called_at, duration, outcome, gathered_context, lead_id');

    const { data, error } = await qualityBase
      .gte('called_at', since.toISOString())
      .order('called_at', { ascending: false })
      .limit(1000);

    if (error) {
      console.error('[reports/quality] query failed', error);
      return NextResponse.json({ error: 'Failed to load call quality.' }, { status: 500 });
    }

    const counts: Record<string, { count: number; example: string | null; callId: string | null }> = {};
    const scores: number[] = [];
    const sentiments: Record<string, number> = {};
    let reviewed = 0;

    for (const call of data ?? []) {
      const qa = (call.gathered_context as any)?.qa;
      if (!qa) continue;
      reviewed++;

      if (typeof qa.score === 'number') scores.push(qa.score);
      if (qa.sentiment) sentiments[qa.sentiment] = (sentiments[qa.sentiment] || 0) + 1;

      for (const entry of qa.tags ?? []) {
        const tag = entry?.tag;
        if (!tag) continue;
        if (!counts[tag]) counts[tag] = { count: 0, example: null, callId: null };
        counts[tag].count++;
        if (!counts[tag].example && entry.reason) {
          counts[tag].example = entry.reason;
          counts[tag].callId = call.id;
        }
      }
    }

    const issues = Object.entries(counts)
      .map(([tag, info]) => ({
        tag,
        label: TAG_GUIDE[tag]?.label ?? tag.replace(/_/g, ' ').toLowerCase(),
        fix: TAG_GUIDE[tag]?.fix ?? null,
        count: info.count,
        share: reviewed > 0 ? Math.round((info.count / reviewed) * 100) : 0,
        example: info.example,
        call_id: info.callId,
      }))
      .sort((a, b) => b.count - a.count);

    return NextResponse.json({
      window_days: days,
      calls_total: data?.length ?? 0,
      calls_reviewed: reviewed,
      avg_quality_score: scores.length ? Number((scores.reduce((a, b) => a + b, 0) / scores.length).toFixed(1)) : null,
      sentiments,
      issues,
    });
  } catch (error: any) {
    console.error('[reports/quality] unexpected', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
