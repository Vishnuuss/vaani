import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { applyVerticalFilter, verticalFromParam } from '@/lib/verticals';

export async function GET(request: Request) {
  try {
    const supabase = createServerClient();

    const vertical = verticalFromParam(new URL(request.url).searchParams.get('vertical'));
    const { data: leads, error } = await applyVerticalFilter(
      supabase.from('leads').select('source'),
      vertical,
    );

    if (error) {
      return NextResponse.json({ error: error.message }, { status: 500 });
    }

    const sourceCount: Record<string, number> = {};
    for (const lead of leads) {
      const src = lead.source || 'unknown';
      sourceCount[src] = (sourceCount[src] || 0) + 1;
    }

    const result = Object.entries(sourceCount)
      .map(([source, count]) => ({ source, count }))
      .sort((a, b) => b.count - a.count);

    return NextResponse.json({ data: result });
  } catch (error: any) {
    console.error('Error GET /api/reports/sources:', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
