import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { getCallBehaviorSettings } from '@/lib/call-behavior';
import { applyLeadSegmentFilter, LEAD_SEGMENTS } from '@/lib/lead-segments';
import { applyVerticalFilter, verticalFromParam } from '@/lib/verticals';

/**
 * Live lead counts per launchable segment, for the Start Campaign dialog.
 *
 * Uses the exact same filter the launch route claims leads with
 * (lib/lead-segments.ts), so the number shown here is never out of step with
 * what a launch actually finds.
 */
export async function GET(request: Request) {
  try {
    const supabase = createServerClient();
    const { callGapMinutes, maxRetries } = await getCallBehaviorSettings(supabase);
    // Same business line the launch will use, so the dialog cannot offer 200
    // "New" leads and then dial only the 40 that belong to this vertical.
    const vertical = verticalFromParam(new URL(request.url).searchParams.get('vertical'));

    const counts = await Promise.all(
      LEAD_SEGMENTS.map(async (segment) => {
        const { count, error } = await applyLeadSegmentFilter(
          supabase.from('leads').select('id', { count: 'exact', head: true }).not('phone', 'is', null),
          segment.value,
          callGapMinutes,
          vertical,
          // Same ceiling the launch applies, so the dialog can never offer 40
          // "Retry pending" leads and then dial only the 12 with quota left.
          maxRetries,
        );
        if (error) {
          console.error('[campaigns/segments] count failed for', segment.value, error);
          return { ...segment, count: 0 };
        }

        /*
         * For the segments that ignore Max retries, say HOW MANY people that
         * actually affects.
         *
         * "May exceed your retry cap" is a sentence nobody weighs. On this
         * database the Line-was-busy segment holds 108 leads against a cap of
         * 2, and 59 of them have already had two or more calls — six have had
         * six. Launching it rings those people a seventh time. That is a
         * legitimate thing to choose, and an indefensible thing to choose by
         * accident, so the number is put in front of the decision.
         */
        let overCap: number | undefined;
        if (segment.bypassesRetryCap && Number.isFinite(maxRetries)) {
          const { count: over } = await applyLeadSegmentFilter(
            supabase
              .from('leads')
              .select('id', { count: 'exact', head: true })
              .not('phone', 'is', null)
              .gte('retry_count', Math.max(0, maxRetries)),
            segment.value,
            callGapMinutes,
            vertical,
            maxRetries,
          );
          overCap = over ?? 0;
        }

        return { ...segment, count: count ?? 0, overCapCount: overCap };
      }),
    );

    // "Follow-ups due" counts callbacks whose date has ARRIVED, while the
    // sidebar's Follow-ups badge counts every scheduled callback. Both are
    // right, and seeing "1" in the sidebar next to "Follow-ups due (0)" in this
    // dialog reads as a bug. So the dialog is told how many are scheduled for
    // later and when the earliest one falls due, and can say so out loud
    // instead of showing a bare zero.
    const nowIso = new Date().toISOString();
    const { count: upcomingCount } = await applyVerticalFilter(
      supabase
        .from('leads')
        .select('id', { count: 'exact', head: true })
        .not('follow_up_date', 'is', null)
        .gt('follow_up_date', nowIso),
      vertical,
    );

    const { data: nextDue } = await applyVerticalFilter(
      supabase
        .from('leads')
        .select('follow_up_date')
        .not('follow_up_date', 'is', null)
        .gt('follow_up_date', nowIso),
      vertical,
    )
      .order('follow_up_date', { ascending: true })
      .limit(1);

    return NextResponse.json({
      segments: counts,
      follow_up_upcoming: upcomingCount ?? 0,
      follow_up_next_due: nextDue?.[0]?.follow_up_date ?? null,
    });
  } catch (error: any) {
    console.error('[campaigns/segments] unexpected', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
