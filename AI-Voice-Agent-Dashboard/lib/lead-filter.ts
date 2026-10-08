/**
 * The filters the Leads page can apply, in ONE place.
 *
 * This exists for the same reason lib/lead-segments.ts does: the list route and
 * the bulk-delete route must agree exactly. The delete dialog tells the client
 * "this will delete 4,318 leads", and that number comes from the list route's
 * count. If the two routes each built their own filter they would eventually
 * drift, and the drift would show up as the client deleting a different set of
 * leads than the one they were shown — which, unlike a wrong count on a report,
 * is not something they can check afterwards.
 *
 * So: one parser, one applier, used by both.
 */

import { applyVerticalFilter, verticalFromParam, VERTICAL_LABELS, type Vertical } from '@/lib/verticals';

export interface LeadFilters {
  search: string;
  /** Lead state: new, queued, called, retry_pending, no_answer, unreachable. */
  statuses: string[];
  qualification: string;
  /** How the LAST call ended - a different axis from status. See the list route. */
  outcomes: string[];
  /** True = only leads with a booked callback. */
  followUp: boolean;
  vertical: Vertical | null;
  /** ISO timestamps. `createdBefore` is the one the client actually wants:
   *  "delete everything older than X". */
  createdBefore: string | null;
  createdAfter: string | null;
  /** Restrict to the leads dialled by one campaign run. */
  campaignRunId: string | null;
  source: string;
}

/**
 * PostgREST's `.or()` takes a filter expression as a STRING, so commas,
 * parentheses and dots in user input change its meaning. Strip them rather than
 * trusting the search box. (Same rule as the list route, which this replaces.)
 */
function sanitiseSearch(raw: string): string {
  return raw.replace(/[,()*%\\"']/g, ' ').trim().slice(0, 80);
}

function csv(raw: string | null): string[] {
  return (raw || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

function isoOrNull(raw: string | null): string | null {
  if (!raw) return null;
  const parsed = new Date(raw);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

/** Reads every supported filter off a query string or a JSON body's fields. */
export function parseLeadFilters(params: URLSearchParams): LeadFilters {
  return {
    search: sanitiseSearch(params.get('search') || ''),
    statuses: csv(params.get('status')),
    qualification: (params.get('qualification') || '').trim(),
    outcomes: csv(params.get('outcome')),
    followUp: params.get('followUp') === 'true',
    vertical: verticalFromParam(params.get('vertical')),
    createdBefore: isoOrNull(params.get('createdBefore')),
    createdAfter: isoOrNull(params.get('createdAfter')),
    campaignRunId: params.get('campaignRunId') || null,
    source: (params.get('source') || '').trim(),
  };
}

/** True when no filter at all is set - i.e. this selects EVERY lead. */
export function isUnfilteredLeadQuery(f: LeadFilters): boolean {
  return (
    !f.search &&
    f.statuses.length === 0 &&
    !f.qualification &&
    f.outcomes.length === 0 &&
    !f.followUp &&
    !f.vertical &&
    !f.createdBefore &&
    !f.createdAfter &&
    !f.campaignRunId &&
    !f.source
  );
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function applyLeadFilters(query: any, f: LeadFilters) {
  query = applyVerticalFilter(query, f.vertical);

  if (f.search) {
    query = query.or(
      `name.ilike.%${f.search}%,phone.ilike.%${f.search}%,city.ilike.%${f.search}%,email.ilike.%${f.search}%`,
    );
  }

  if (f.statuses.length === 1) query = query.eq('status', f.statuses[0]);
  else if (f.statuses.length > 1) query = query.in('status', f.statuses);

  if (f.qualification) query = query.eq('qualification', f.qualification);

  if (f.outcomes.length === 1) query = query.eq('call_outcome', f.outcomes[0]);
  else if (f.outcomes.length > 1) query = query.in('call_outcome', f.outcomes);

  if (f.followUp) query = query.not('follow_up_date', 'is', null);

  if (f.createdBefore) query = query.lt('created_at', f.createdBefore);
  if (f.createdAfter) query = query.gte('created_at', f.createdAfter);

  if (f.campaignRunId) query = query.eq('campaign_run_id', f.campaignRunId);
  if (f.source) query = query.eq('source', f.source);

  return query;
}

/**
 * The value the UI's filter menu actually sends is `not_qualified`, not
 * `unqualified` — the dashboard's LEAD_FILTERS list is the authority. Getting
 * this wrong does not break the delete (the filter is passed through verbatim),
 * but it puts a raw database value in the confirmation dialog, which is the one
 * sentence the client reads before destroying thousands of rows.
 */
const QUALIFICATION_LABELS: Record<string, string> = {
  qualified: 'Qualified',
  not_qualified: 'Not qualified',
  unqualified: 'Not qualified',
  hot: 'Hot',
  warm: 'Warm',
  cold: 'Cold',
};

const STATUS_LABELS: Record<string, string> = {
  new: 'Never called',
  queued: 'Being dialled',
  called: 'Called',
  retry_pending: 'Retry pending',
  no_answer: 'No answer',
  unreachable: 'Unreachable',
};

function labelDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });
}

/**
 * The filter as an English sentence, for the confirmation dialog and for the
 * permanent record in the Recycle Bin.
 *
 * The client is about to destroy thousands of rows on the strength of this
 * sentence, so it describes what WILL be deleted rather than echoing parameter
 * names — "Unqualified leads, Solar, added before 01 Jul 2026", not
 * "qualification=unqualified&vertical=solar".
 */
export function describeLeadFilters(f: LeadFilters): string {
  const parts: string[] = [];

  if (f.qualification) parts.push(QUALIFICATION_LABELS[f.qualification] ?? f.qualification);
  if (f.statuses.length) parts.push(f.statuses.map((s) => STATUS_LABELS[s] ?? s).join(' or '));
  if (f.outcomes.length) parts.push(`last call ${f.outcomes.join(' or ')}`);
  if (f.followUp) parts.push('with a booked callback');
  if (f.vertical) parts.push(VERTICAL_LABELS[f.vertical]);
  if (f.source) parts.push(`from ${f.source}`);
  if (f.campaignRunId) parts.push('from one campaign');
  if (f.search) parts.push(`matching "${f.search}"`);
  if (f.createdAfter) parts.push(`added on or after ${labelDate(f.createdAfter)}`);
  if (f.createdBefore) parts.push(`added before ${labelDate(f.createdBefore)}`);

  return parts.length ? `Leads: ${parts.join(', ')}` : 'All leads';
}
