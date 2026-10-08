import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { currentUserEmail } from '@/lib/session';
import { deleteCallLogs, describeCallLogFilters, parseCallLogFilters, MAX_ROWS_PER_DELETE } from '@/lib/trash';
import {
  BulkDeleteRequestError,
  parseBulkDeleteBody,
  requireConfirmation,
} from '@/lib/bulk-delete-request';

/**
 * Bulk delete for call history, into the Recycle Bin.
 *
 * This deletes the CALL RECORDS ONLY. The leads they belong to are untouched,
 * which is the whole point of having it separate from the leads delete: the
 * client's complaint is call-log volume, and they should be able to clear years
 * of "no answer" rows without losing a single contact.
 *
 * Note what stays behind on the lead itself: `retry_count`, `status`,
 * `last_attempt_at`, `score` and `recording_url` all live on `leads` and are not
 * recomputed from call history. So clearing call logs does not resurrect a lead
 * into the dialling rotation or reset its retry quota — deleting history cannot
 * cause anyone to be phoned again.
 */

export const maxDuration = 60;

export async function POST(request: Request) {
  try {
    const body = parseBulkDeleteBody(await request.json().catch(() => null));
    const filters = parseCallLogFilters(body.filters);
    const supabase = createServerClient();

    // `!inner` on the vertical filter for the same reason the calls list route
    // uses it: a plain join would count every call row and merely null out the
    // non-matching lead, so a "Solar only" delete would report — and then
    // delete — every call in the system.
    let countQuery = filters.vertical
      ? supabase
          .from('call_logs')
          .select('id, leads!inner (vertical)', { count: 'exact', head: true })
          .eq('leads.vertical', filters.vertical)
      : supabase.from('call_logs').select('id', { count: 'exact', head: true });

    if (filters.outcomes.length === 1) countQuery = countQuery.eq('outcome', filters.outcomes[0]);
    else if (filters.outcomes.length > 1) countQuery = countQuery.in('outcome', filters.outcomes);
    if (filters.calledBefore) countQuery = countQuery.lt('called_at', filters.calledBefore);
    if (filters.calledAfter) countQuery = countQuery.gte('called_at', filters.calledAfter);
    if (filters.campaignRunId) countQuery = countQuery.eq('campaign_run_id', filters.campaignRunId);
    if (body.mode === 'ids') countQuery = countQuery.in('id', body.ids);

    const { count, error: countError } = await countQuery;
    if (countError) {
      console.error('[calls:bulk-delete] count failed', countError);
      return NextResponse.json({ error: 'Could not work out how many call logs match.' }, { status: 500 });
    }

    const total = count || 0;
    const filterLabel =
      body.mode === 'ids'
        ? `${total.toLocaleString('en-IN')} hand-picked call log${total === 1 ? '' : 's'}`
        : describeCallLogFilters(filters);

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

    const result = await deleteCallLogs(supabase, {
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
    console.error('[calls:bulk-delete] unexpected', error);
    return NextResponse.json({ error: error?.message || 'Internal Server Error' }, { status: 500 });
  }
}
