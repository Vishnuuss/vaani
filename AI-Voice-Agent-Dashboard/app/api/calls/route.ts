import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { verticalFromParam } from '@/lib/verticals';

/**
 * Call history, newest first.
 *
 * The previous version filtered on a `direction` column that does not exist in
 * call_logs, so any non-"all" filter returned a 500. Every call this system places
 * is outbound; the meaningful axis is the outcome, so that is what we filter on.
 */

/** UI filter name -> the call_logs.outcome values it covers. */
const FILTER_GROUPS: Record<string, string[]> = {
  connected: ['completed'],
  missed: ['no_answer', 'busy', 'voicemail'],
  failed: ['failed', 'cancelled'],
};

const MAX_LIMIT = 200;

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url);
    const filter = searchParams.get('filter');
    const outcome = searchParams.get('outcome');
    const search = searchParams.get('search');
    const startDate = searchParams.get('startDate');
    const endDate = searchParams.get('endDate');
    const page = Math.max(parseInt(searchParams.get('page') || '1', 10) || 1, 1);
    const limit = Math.min(Math.max(parseInt(searchParams.get('limit') || '20', 10) || 20, 1), MAX_LIMIT);

    const supabase = createServerClient();

    // Joined so the table can show who was called without an N+1 per row.
    //
    // A call has no vertical of its own - it inherits the business line of the
    // lead it was placed to, so the header switch filters through the join.
    // `!inner` is required for that: with a plain join, filtering on a joined
    // column returns every call row and merely nulls out the non-matching lead,
    // which would show the right count but the wrong rows.
    const vertical = verticalFromParam(searchParams.get('vertical'));
    let query = vertical
      ? supabase
          .from('call_logs')
          .select('*, leads!inner (id, name, phone, city, vertical)', { count: 'exact' })
          .eq('leads.vertical', vertical)
      : supabase.from('call_logs').select('*, leads (id, name, phone, city, vertical)', { count: 'exact' });

    if (outcome) {
      query = query.eq('outcome', outcome);
    } else if (filter && filter !== 'all') {
      const group = FILTER_GROUPS[filter];
      if (group) query = query.in('outcome', group);
    }

    if (startDate) query = query.gte('called_at', startDate);
    if (endDate) query = query.lte('called_at', endDate);

    const from = (page - 1) * limit;
    const to = from + limit - 1;

    // Order by called_at: created_at is when WE recorded the row, which for
    // reconciled calls can be hours after the call actually happened.
    query = query.order('called_at', { ascending: false }).range(from, to);

    const { data: calls, count, error } = await query;

    if (error) {
      console.error('[calls:GET] query failed', error);
      return NextResponse.json({ error: 'Failed to load calls.' }, { status: 500 });
    }

    // Lead name/phone live on the joined row, so filtering has to happen after the
    // fetch. Scoped to the current page, which is what the search box implies.
    const term = search?.trim().toLowerCase();
    const rows = term
      ? (calls ?? []).filter((call: any) =>
          `${call.leads?.name ?? ''} ${call.leads?.phone ?? ''} ${call.outcome ?? ''}`
            .toLowerCase()
            .includes(term),
        )
      : (calls ?? []);

    const total = count || 0;

    return NextResponse.json({
      calls: rows,
      // Both shapes are returned because the dashboard hooks read the flat keys.
      totalCount: total,
      totalPages: total ? Math.ceil(total / limit) : 0,
      pagination: {
        total,
        page,
        limit,
        totalPages: total ? Math.ceil(total / limit) : 0,
      },
    });
  } catch (error: any) {
    console.error('[calls:GET] unexpected', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
