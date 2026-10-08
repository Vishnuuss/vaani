import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { applyVerticalFilter, verticalFromParam } from '@/lib/verticals';

export async function GET(request: Request) {
  try {
    const supabase = createServerClient();

    // Scoped by the dashboard's header switch. Every count below MUST use the
    // same filter as /api/leads, or the sidebar badge says one number while the
    // table it links to shows another.
    const vertical = verticalFromParam(new URL(request.url).searchParams.get('vertical'));
    const countQuery = () =>
      applyVerticalFilter(supabase.from('leads').select('*', { count: 'exact', head: true }), vertical);

    const [
      totalRes,
      qualifiedRes,
      notQualifiedRes,
      newRes,
      calledRes,
      queuedRes,
      followUpRes,
      retryRes,
      unreachableRes,
    ] = await Promise.all([
        countQuery(),
        countQuery().eq('qualification', 'qualified'),
        countQuery().eq('qualification', 'not_qualified'),
        countQuery().eq('status', 'new'),
        countQuery().eq('status', 'called'),
        countQuery().eq('status', 'queued'),
        countQuery().not('follow_up_date', 'is', null),
        // Unanswered leads still eligible for another attempt.
        countQuery().eq('status', 'retry_pending'),
        countQuery().in('status', ['no_answer', 'unreachable']),
      ]);

    // Get source breakdown
    const { data: allLeads } = await applyVerticalFilter(
      supabase.from('leads').select('source'),
      vertical,
    );

    const sourceBreakdown: Record<string, number> = {};
    if (allLeads) {
      for (const lead of allLeads) {
        const src = lead.source || 'Unknown';
        sourceBreakdown[src] = (sourceBreakdown[src] || 0) + 1;
      }
    }

    return NextResponse.json({
      total: totalRes.count || 0,
      qualified: qualifiedRes.count || 0,
      not_qualified: notQualifiedRes.count || 0,
      new_leads: newRes.count || 0,
      called: calledRes.count || 0,
      queued: queuedRes.count || 0,
      follow_ups: followUpRes.count || 0,
      retry_pending: retryRes.count || 0,
      unreachable: unreachableRes.count || 0,
      not_interested: notQualifiedRes.count || 0,
      site_visits: 0, // No site_visit tracking in current schema
      by_source: sourceBreakdown,
    });
  } catch (error: any) {
    console.error('Unexpected error in GET /api/leads/stats:', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
