import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { verticalFromParam } from '@/lib/verticals';

const PAGE = 1_000;
const MAX_ROWS = 50_000;

/**
 * Call totals for the stat tiles.
 *
 * Two fixes over the previous version:
 *  - it returned `avgDuration` while the dashboard read `avg_duration`, so the
 *    "Avg. duration" tile was permanently 0:00. Both keys are returned now.
 *  - a bare select() is capped at 1000 rows by PostgREST, so every number silently
 *    stopped growing once the table passed a thousand calls. This pages through.
 */
export async function GET(request: Request) {
  try {
    const supabase = createServerClient();
    // A call inherits its business line from the lead it was placed to, so the
    // header switch filters through the join. `!inner` matters: with a plain
    // join, filtering a joined column keeps every call row and merely nulls the
    // lead, which would leave these totals counting other verticals' calls.
    const vertical = verticalFromParam(new URL(request.url).searchParams.get('vertical'));

    const rows: { outcome: string | null; duration: number | null; called_at: string }[] = [];
    for (let from = 0; from < MAX_ROWS; from += PAGE) {
      const base = vertical
        ? supabase.from('call_logs').select('outcome, duration, called_at, leads!inner(vertical)').eq('leads.vertical', vertical)
        : supabase.from('call_logs').select('outcome, duration, called_at');

      const { data, error } = await base
        .order('called_at', { ascending: false })
        .range(from, from + PAGE - 1);

      if (error) {
        console.error('[calls/stats] query failed', error);
        return NextResponse.json({ error: 'Failed to load call stats.' }, { status: 500 });
      }
      if (!data || data.length === 0) break;
      rows.push(...(data as any));
      if (data.length < PAGE) break;
    }

    const byOutcome: Record<string, number> = {};
    let connected = 0;
    let missed = 0;
    let failed = 0;
    let answeredDuration = 0;
    let answeredCount = 0;
    let today = 0;

    const startOfToday = new Date();
    startOfToday.setHours(0, 0, 0, 0);

    for (const call of rows) {
      const outcome = call.outcome ?? 'unknown';
      byOutcome[outcome] = (byOutcome[outcome] || 0) + 1;

      if (outcome === 'completed') {
        connected++;
        // Average over connected calls only: including no-answers (duration 0)
        // dragged the displayed average down to a fraction of real talk time.
        answeredCount++;
        answeredDuration += call.duration || 0;
      } else if (outcome === 'no_answer' || outcome === 'busy' || outcome === 'voicemail') {
        missed++;
      } else if (outcome === 'failed' || outcome === 'cancelled') {
        failed++;
      }

      if (call.called_at && new Date(call.called_at) >= startOfToday) today++;
    }

    const total = rows.length;
    const avgDuration = answeredCount > 0 ? Math.round(answeredDuration / answeredCount) : 0;

    return NextResponse.json({
      total,
      connected,
      missed,
      failed,
      today,
      // snake_case is what the dashboard and the CallStats type use; the camelCase
      // key is kept so nothing that already reads it breaks.
      avg_duration: avgDuration,
      avgDuration,
      total_talk_time: answeredDuration,
      connect_rate: total > 0 ? Math.round((connected / total) * 100) : 0,
      by_outcome: byOutcome,
    });
  } catch (error: any) {
    console.error('[calls/stats] unexpected', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
