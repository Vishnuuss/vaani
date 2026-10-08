import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { applyLeadFilters, parseLeadFilters } from '@/lib/lead-filter';

/** Columns a client is allowed to sort by - anything else is a PostgREST error. */
const SORTABLE = new Set([
  'created_at',
  'updated_at',
  'last_attempt_at',
  'follow_up_date',
  'score',
  'name',
  'status',
]);

const MAX_LIMIT = 200;

/**
 * The lead list.
 *
 * Filter parsing and application moved to lib/lead-filter.ts so that the
 * bulk-delete route runs the EXACT same filter. The delete dialog quotes a count
 * that comes from this endpoint's `totalCount`, and the two must never be able
 * to disagree — see the note at the top of that file.
 */
export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url);
    const filters = parseLeadFilters(searchParams);

    const page = Math.max(parseInt(searchParams.get('page') || '1', 10) || 1, 1);
    const limit = Math.min(Math.max(parseInt(searchParams.get('limit') || '20', 10) || 20, 1), MAX_LIMIT);
    const requestedSort = searchParams.get('sort') || 'created_at';
    const sort = SORTABLE.has(requestedSort) ? requestedSort : 'created_at';
    const order = searchParams.get('order') === 'asc' ? 'asc' : 'desc';

    const supabase = createServerClient();

    let query = applyLeadFilters(supabase.from('leads').select('*', { count: 'exact' }), filters);

    const from = (page - 1) * limit;
    const to = from + limit - 1;

    query = query
      .order(sort, { ascending: order === 'asc', nullsFirst: false })
      .range(from, to);

    const { data: leads, error, count } = await query;

    if (error) {
      console.error('[leads:GET] query failed', error);
      return NextResponse.json({ error: 'Failed to load leads.' }, { status: 500 });
    }

    const total = count || 0;
    const totalPages = total ? Math.ceil(total / limit) : 0;

    return NextResponse.json({
      leads: leads ?? [],
      // The dashboard hooks read these flat keys; `pagination` is kept for any
      // other consumer that already depends on it.
      totalCount: total,
      totalPages,
      pagination: { total, page, limit, totalPages },
    });
  } catch (error: any) {
    console.error('[leads:GET] unexpected', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
