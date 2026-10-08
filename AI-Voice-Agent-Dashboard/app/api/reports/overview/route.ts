import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { verticalFromParam } from '@/lib/verticals';

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url);
    const range = parseInt(searchParams.get('range') || '7', 10);
    
    const supabase = createServerClient();
    const startDate = new Date();
    startDate.setDate(startDate.getDate() - range);
    
    // Scoped to the header switch. `!inner` is required when filtering on the
    // joined lead: a plain join would keep every call row and merely null the
    // lead, so the chart would show other business lines' calls.
    const vertical = verticalFromParam(searchParams.get('vertical'));
    const base = vertical
      ? supabase
          .from('call_logs')
          .select('created_at, outcome, leads!inner (qualification, vertical)')
          .eq('leads.vertical', vertical)
      : supabase.from('call_logs').select('created_at, outcome, leads (qualification)');

    const { data: calls, error } = await base.gte('created_at', startDate.toISOString());

    if (error) {
      return NextResponse.json({ error: error.message }, { status: 500 });
    }

    const dailyStats: Record<string, { total: number; qualified: number }> = {};
    
    // Initialize dates
    for (let i = 0; i < range; i++) {
      const d = new Date();
      d.setDate(d.getDate() - i);
      const dateStr = d.toISOString().split('T')[0];
      dailyStats[dateStr] = { total: 0, qualified: 0 };
    }

    for (const call of calls) {
      const dateStr = new Date(call.created_at).toISOString().split('T')[0];
      if (dailyStats[dateStr]) {
        dailyStats[dateStr].total++;
        // If lead is qualified from this call
        if (call.leads && (call.leads as any).qualification === 'qualified') {
           dailyStats[dateStr].qualified++;
        }
      }
    }

    const result = Object.entries(dailyStats)
      .map(([date, stats]) => ({ date, ...stats }))
      .sort((a, b) => a.date.localeCompare(b.date));

    return NextResponse.json({ data: result });
  } catch (error: any) {
    console.error('Error GET /api/reports/overview:', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
