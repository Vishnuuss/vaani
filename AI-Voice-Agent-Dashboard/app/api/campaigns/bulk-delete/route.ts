import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { currentUserEmail } from '@/lib/session';
import { deleteCampaigns, describeCampaignFilters, parseCampaignFilters, MAX_ROWS_PER_DELETE } from '@/lib/trash';
import {
  BulkDeleteRequestError,
  parseBulkDeleteBody,
  requireConfirmation,
} from '@/lib/bulk-delete-request';

/**
 * Bulk delete for campaign history, into the Recycle Bin.
 *
 * Only completed, failed and cancelled runs are ever eligible — the restriction
 * is enforced in lib/trash.ts deleteCampaigns(), on the query itself, so it
 * cannot be bypassed from here. A live campaign's row is the only handle this
 * system has on the Dograh campaign it started; deleting it mid-flight leaves
 * the provider dialling and billing with nothing on our side able to see or stop
 * it. See the note on deleteCampaigns for the full reasoning.
 *
 * Even "Delete all" respects that, which is why the count below is restricted to
 * the deletable statuses: the dialog must never promise to remove a campaign
 * that the delete will then refuse to touch.
 */

export const maxDuration = 60;

const DELETABLE = ['completed', 'failed', 'cancelled'];

export async function POST(request: Request) {
  try {
    const body = parseBulkDeleteBody(await request.json().catch(() => null));
    const filters = parseCampaignFilters(body.filters);
    const supabase = createServerClient();

    const eligible = filters.statuses.length
      ? filters.statuses.filter((s) => DELETABLE.includes(s))
      : DELETABLE;

    let countQuery = supabase.from('campaign_runs').select('id', { count: 'exact', head: true });
    countQuery = eligible.length
      ? countQuery.in('status', eligible)
      : // Asked only for live campaigns: nothing is eligible. `.in` with an empty
        // list is a PostgREST error, so express "match nothing" explicitly.
        countQuery.in('status', ['__none__']);
    if (filters.vertical) countQuery = countQuery.eq('vertical', filters.vertical);
    if (filters.createdBefore) countQuery = countQuery.lt('created_at', filters.createdBefore);
    if (filters.createdAfter) countQuery = countQuery.gte('created_at', filters.createdAfter);
    if (body.mode === 'ids') countQuery = countQuery.in('id', body.ids);

    const { count, error: countError } = await countQuery;
    if (countError) {
      console.error('[campaigns:bulk-delete] count failed', countError);
      return NextResponse.json({ error: 'Could not work out how many campaigns match.' }, { status: 500 });
    }

    const total = count || 0;
    const filterLabel =
      body.mode === 'ids'
        ? `${total.toLocaleString('en-IN')} hand-picked campaign${total === 1 ? '' : 's'}`
        : describeCampaignFilters(filters);

    if (body.preview) {
      return NextResponse.json({
        count: total,
        filterLabel,
        capped: total > MAX_ROWS_PER_DELETE,
        maxPerDelete: MAX_ROWS_PER_DELETE,
        note: 'Running, paused and queued campaigns are never deleted.',
      });
    }

    if (total === 0) {
      return NextResponse.json({
        deleted: 0,
        cascaded: 0,
        skipped: 0,
        filterLabel,
        message: 'No finished campaigns matched — nothing was deleted.',
      });
    }

    requireConfirmation(body.mode, total, body.confirmCount);

    const result = await deleteCampaigns(supabase, {
      filters,
      ids: body.mode === 'ids' ? body.ids : undefined,
      filterLabel,
      deletedBy: await currentUserEmail(),
    });

    return NextResponse.json({ ...result, success: true });
  } catch (error: any) {
    if (error instanceof BulkDeleteRequestError) {
      return NextResponse.json({ error: error.message }, { status: 400 });
    }
    console.error('[campaigns:bulk-delete] unexpected', error);
    return NextResponse.json({ error: error?.message || 'Internal Server Error' }, { status: 500 });
  }
}
