import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { applyVerticalFilter, verticalFromParam } from '@/lib/verticals';

export async function GET(request: Request) {
  try {
    const supabase = createServerClient();

    // These four tiles sit on the Campaigns page, under the same header switch
    // as everything else. Unscoped, "Solar" would show the loan campaigns'
    // numbers - the exact confusion the switch exists to prevent.
    const vertical = verticalFromParam(new URL(request.url).searchParams.get('vertical'));

    const [activeRes, targetedRes, calledRes, qualifiedRes] = await Promise.all([
      applyVerticalFilter(
        supabase.from('campaign_runs').select('*', { count: 'exact', head: true }),
        vertical,
      ).eq('status', 'running'),
      applyVerticalFilter(supabase.from('campaign_runs').select('requested_count'), vertical),
      applyVerticalFilter(
        supabase.from('leads').select('*', { count: 'exact', head: true }),
        vertical,
      ).eq('status', 'called'),
      applyVerticalFilter(
        supabase.from('leads').select('*', { count: 'exact', head: true }),
        vertical,
      ).eq('qualification', 'qualified')
    ]);

    const activeCount = activeRes.count || 0;
    const totalTargeted = (targetedRes.data || []).reduce((sum: number, c: any) => sum + (c.requested_count || 0), 0);
    const totalCalled = calledRes.count || 0;
    const totalQualified = qualifiedRes.count || 0;

    return NextResponse.json({
      activeCampaigns: activeCount,
      totalTargeted,
      totalCalled,
      totalQualified
    });
  } catch (error: any) {
    console.error('Error GET /api/campaigns/stats:', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
