import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { currentUserEmail } from '@/lib/session';
import { applyLeadFilters, describeLeadFilters, parseLeadFilters } from '@/lib/lead-filter';
import { deleteLeads, MAX_ROWS_PER_DELETE } from '@/lib/trash';
import {
  BulkDeleteRequestError,
  parseBulkDeleteBody,
  requireConfirmation,
} from '@/lib/bulk-delete-request';

/**
 * Bulk delete for leads, into the Recycle Bin.
 *
 * POST rather than DELETE because the request carries a body — a filter object
 * and up to 2,000 ids. Bodies on DELETE are legal but poorly supported by
 * intermediaries, and a delete request silently losing its filter and being read
 * as "delete everything" is the single worst failure this endpoint could have.
 *
 * `preview: true` returns the count and the English description of the filter
 * without touching anything, so the confirmation dialog and the delete are
 * measured by the same code path.
 */

export const maxDuration = 60;

export async function POST(request: Request) {
  try {
    const body = parseBulkDeleteBody(await request.json().catch(() => null));
    const filters = parseLeadFilters(body.filters);
    const supabase = createServerClient();

    // The count the dialog shows and the confirmation is checked against.
    // `queued` leads are excluded here exactly as the delete excludes them, so
    // the promised number is the number that actually goes.
    let countQuery = applyLeadFilters(
      supabase.from('leads').select('id', { count: 'exact', head: true }),
      filters,
    ).neq('status', 'queued');
    if (body.mode === 'ids') countQuery = countQuery.in('id', body.ids);

    const { count, error: countError } = await countQuery;
    if (countError) {
      console.error('[leads:bulk-delete] count failed', countError);
      return NextResponse.json({ error: 'Could not work out how many leads match.' }, { status: 500 });
    }

    const total = count || 0;
    const filterLabel =
      body.mode === 'ids'
        ? `${total.toLocaleString('en-IN')} hand-picked lead${total === 1 ? '' : 's'}`
        : describeLeadFilters(filters);

    if (body.preview) {
      return NextResponse.json({
        count: total,
        filterLabel,
        capped: total > MAX_ROWS_PER_DELETE,
        maxPerDelete: MAX_ROWS_PER_DELETE,
      });
    }

    if (total === 0) {
      return NextResponse.json({ deleted: 0, cascaded: 0, skipped: 0, filterLabel, message: 'Nothing matched — nothing was deleted.' });
    }

    requireConfirmation(body.mode, total, body.confirmCount);

    const result = await deleteLeads(supabase, {
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
    console.error('[leads:bulk-delete] unexpected', error);
    return NextResponse.json({ error: error?.message || 'Internal Server Error' }, { status: 500 });
  }
}
