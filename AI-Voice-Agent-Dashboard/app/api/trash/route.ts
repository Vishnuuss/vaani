import { NextResponse } from 'next/server';
import { createServerClient, isMissingTableError } from '@/lib/supabase-server';
import { outstandingRows, purgeAll, RETENTION_DAYS } from '@/lib/trash';

/**
 * The Recycle Bin listing.
 *
 * Lists delete ACTIONS, not deleted rows. "4,318 unqualified leads, deleted 2
 * hours ago" is one line the client can restore in one click; 4,318 lines would
 * be the same unusable volume the delete feature exists to reduce.
 */

const MAX_LIMIT = 100;

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url);
    const entity = searchParams.get('entity');
    const page = Math.max(parseInt(searchParams.get('page') || '1', 10) || 1, 1);
    const limit = Math.min(Math.max(parseInt(searchParams.get('limit') || '25', 10) || 25, 1), MAX_LIMIT);

    const supabase = createServerClient();

    let query = supabase.from('trash_batches').select('*', { count: 'exact' });
    if (entity && ['lead', 'call_log', 'campaign'].includes(entity)) {
      query = query.eq('entity', entity);
    }

    const from = (page - 1) * limit;
    const { data, count, error } = await query
      .order('created_at', { ascending: false })
      .range(from, from + limit - 1);

    if (error) {
      // The bin is the one screen a client opens when they are already worried
      // about lost data, so say plainly when it is not there yet rather than
      // showing an empty bin, which would read as "your data is gone".
      if (isMissingTableError(error)) {
        return NextResponse.json(
          { error: 'The Recycle Bin tables are missing. Run scripts/008_trash_and_feedback.sql in Supabase.', batches: [] },
          { status: 503 },
        );
      }
      console.error('[trash:GET] query failed', error);
      return NextResponse.json({ error: 'Failed to load the Recycle Bin.' }, { status: 500 });
    }

    const batches = data ?? [];

    /*
     * Attach the names of the people in each lead batch.
     *
     * Without this the bin's only description of a delete is its filter_label,
     * which for the commonest case — ticking rows by hand — reads "1 hand-picked
     * lead". That is a description of HOW it was deleted, not WHAT was deleted,
     * and it is unanswerable: the client cannot tell whether the thing they are
     * about to restore or wipe is the customer they care about.
     *
     * One extra query for the whole page, not one per row. Only lead batches:
     * call_logs_trash stores no name, and a campaign batch's label is already
     * the campaign's own name.
     */
    const leadBatchIds = batches.filter((b) => b.entity === 'lead').map((b) => b.id);
    const samples = new Map<string, { names: string[]; total: number }>();

    if (leadBatchIds.length) {
      const { data: rows } = await supabase
        .from('leads_trash')
        .select('batch_id, name, phone')
        .in('batch_id', leadBatchIds)
        // Bounded: enough to name a few per batch without reading a 20,000-row
        // delete back out of the archive just to render one line.
        .limit(400);

      for (const row of rows ?? []) {
        const entry = samples.get(row.batch_id) ?? { names: [], total: 0 };
        entry.total += 1;
        if (entry.names.length < 3) entry.names.push(row.name || row.phone || 'Unnamed');
        samples.set(row.batch_id, entry);
      }
    }

    /*
     * How many rows in each RESTORED batch never actually made it back.
     *
     * `restored_at` only means "at least one row returned". A restore is per-row
     * and may partially fail, and for the rows that failed the archive is the
     * only copy left. Without this number the bin labels such a batch "Put back
     * / Already in your list" and offers to throw it away — which is precisely
     * how a Recycle Bin ends up destroying the record it was protecting.
     *
     * Only for restored batches: an un-restored batch is outstanding by
     * definition, and this costs a query per batch.
     */
    const outstanding = new Map<string, number>();
    for (const batch of batches) {
      if (!batch.restored_at) continue;
      outstanding.set(batch.id, await outstandingRows(supabase, batch.id));
    }

    const total = count || 0;
    return NextResponse.json({
      batches: batches.map((b) => ({
        ...b,
        sample: samples.get(b.id)?.names ?? null,
        outstanding: b.restored_at ? (outstanding.get(b.id) ?? 0) : b.row_count + b.cascaded_count,
      })),
      retentionDays: RETENTION_DAYS,
      totalCount: total,
      totalPages: total ? Math.ceil(total / limit) : 0,
    });
  } catch (error: any) {
    console.error('[trash:GET] unexpected', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}

/**
 * Empty the bin — permanent, for everything currently in it.
 *
 * Requires `?confirm=empty` so that a stray DELETE to the collection URL cannot
 * destroy the one copy of data the client deleted by mistake.
 */
export async function DELETE(request: Request) {
  try {
    const { searchParams } = new URL(request.url);
    if (searchParams.get('confirm') !== 'empty') {
      return NextResponse.json({ error: 'Add ?confirm=empty to permanently empty the Recycle Bin.' }, { status: 400 });
    }

    const supabase = createServerClient();
    const force = searchParams.get('force') === '1';
    const { purged, outstanding } = await purgeAll(supabase, { force });

    // Nothing was deleted: the bin still holds records that are not in the live
    // tables, so emptying it would destroy the only copy. Report the number and
    // make the client ask again with ?force=1.
    if (outstanding > 0) {
      return NextResponse.json(
        {
          error:
            `${outstanding.toLocaleString('en-IN')} record${outstanding === 1 ? '' : 's'} in the bin ` +
            `${outstanding === 1 ? 'is' : 'are'} not in your live data — this is the only copy.`,
          outstanding,
          needsForce: true,
        },
        { status: 409 },
      );
    }

    return NextResponse.json({ purged, success: true });
  } catch (error: any) {
    console.error('[trash:DELETE] unexpected', error);
    return NextResponse.json({ error: error?.message || 'Internal Server Error' }, { status: 500 });
  }
}
