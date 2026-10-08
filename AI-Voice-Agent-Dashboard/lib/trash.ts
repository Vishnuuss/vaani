/**
 * The Recycle Bin: archive, restore and purge.
 *
 * Deleting does not remove a row so much as MOVE it. The row is copied whole
 * into a `*_trash` table and then deleted from the live table, which is what
 * makes the delete safe: see the long note at the top of
 * scripts/008_trash_and_feedback.sql for why a `deleted_at` flag was rejected.
 *
 * Everything here is chunked. The client's whole complaint is that there are
 * too many rows, so "delete all unqualified leads" is expected to be tens of
 * thousands of records — well past what PostgREST will return or accept in one
 * request.
 */

import type { SupabaseClient } from '@supabase/supabase-js';
import { applyLeadFilters, type LeadFilters } from '@/lib/lead-filter';
import { isTerminal } from '@/lib/campaign-state';
import { verticalFromParam, VERTICAL_LABELS, type Vertical } from '@/lib/verticals';

/** How long a deleted batch stays restorable. */
export const RETENTION_DAYS = 7;

/** Rows per round trip. Comfortably inside PostgREST limits, few enough that a
 *  failure halfway through a huge delete leaves a small, restorable mess. */
const CHUNK = 500;

/** Hard ceiling on one delete action, so a runaway filter cannot lock the table
 *  for minutes. The routes report the cap and the client repeats the action. */
export const MAX_ROWS_PER_DELETE = 20_000;

/** Belt-and-braces against a filter that somehow never empties. */
const MAX_ITERATIONS = Math.ceil(MAX_ROWS_PER_DELETE / CHUNK) + 5;

export type TrashEntity = 'lead' | 'call_log' | 'campaign';

export interface DeleteResult {
  batchId: string | null;
  deleted: number;
  /** Rows swept along with a deleted parent (call logs of a deleted lead). */
  cascaded: number;
  /** Rows that matched the filter but were protected - see `skippedReason`. */
  skipped: number;
  skippedReason: string | null;
  /** True when MAX_ROWS_PER_DELETE stopped the run before the filter emptied. */
  capped: boolean;
  filterLabel: string;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Db = SupabaseClient<any, 'public', any>;

async function openBatch(
  supabase: Db,
  entity: TrashEntity,
  filterLabel: string,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  filterJson: Record<string, any>,
  deletedBy: string | null,
): Promise<string> {
  const purgeAfter = new Date(Date.now() + RETENTION_DAYS * 86_400_000).toISOString();
  const { data, error } = await supabase
    .from('trash_batches')
    .insert({
      entity,
      filter_label: filterLabel,
      filter_json: filterJson,
      deleted_by: deletedBy,
      purge_after: purgeAfter,
    })
    .select('id')
    .single();

  if (error || !data) throw new Error(`Could not open a Recycle Bin batch: ${error?.message}`);
  return data.id as string;
}

/**
 * Closes a batch, or removes it if nothing was actually deleted.
 *
 * An empty batch left behind would show in the Recycle Bin as "0 leads deleted",
 * which reads like a bug to the client and is noise in the purge sweep.
 */
async function closeBatch(supabase: Db, batchId: string, deleted: number, cascaded: number) {
  if (deleted === 0) {
    await supabase.from('trash_batches').delete().eq('id', batchId);
    return;
  }
  await supabase
    .from('trash_batches')
    .update({ row_count: deleted, cascaded_count: cascaded })
    .eq('id', batchId);
}

// ─── Leads ───────────────────────────────────────────────────────────────────

/**
 * Deletes leads by filter, by explicit id list, or both.
 *
 * `status = 'queued'` leads are EXCLUDED at the query level, not filtered out
 * afterwards. A queued lead is one the campaign runner has already claimed and
 * is dialling right now; deleting it mid-call would strand the webhook that is
 * about to report the result, so the row must survive until the call lands.
 * Doing the exclusion in the query rather than in JavaScript also guarantees the
 * paging loop below terminates - a protected row that still matched the filter
 * would be re-fetched forever.
 */
export async function deleteLeads(
  supabase: Db,
  opts: { filters: LeadFilters; ids?: string[]; filterLabel: string; deletedBy?: string | null },
): Promise<DeleteResult> {
  const { filters, ids, filterLabel, deletedBy = null } = opts;

  // Count what the filter protects, so the client is told rather than left to
  // wonder why 12 of their 15 selected leads disappeared.
  let queuedQuery = applyLeadFilters(
    supabase.from('leads').select('id', { count: 'exact', head: true }),
    filters,
  ).eq('status', 'queued');
  if (ids?.length) queuedQuery = queuedQuery.in('id', ids);
  const { count: queuedCount } = await queuedQuery;

  const batchId = await openBatch(supabase, 'lead', filterLabel, { ...filters, ids: ids ?? null }, deletedBy);

  let deleted = 0;
  let cascaded = 0;
  let capped = false;

  try {
    for (let i = 0; i < MAX_ITERATIONS; i++) {
      const remaining = MAX_ROWS_PER_DELETE - deleted;
      if (remaining <= 0) {
        capped = true;
        break;
      }

      let query = applyLeadFilters(supabase.from('leads').select('*'), filters).neq('status', 'queued');
      if (ids?.length) query = query.in('id', ids);

      const { data: rows, error } = await query.limit(Math.min(CHUNK, remaining));
      if (error) throw new Error(`Could not read the leads to delete: ${error.message}`);
      if (!rows?.length) break;

      const chunkIds = rows.map((r: any) => r.id as string);

      // Archive the call logs FIRST, then delete them explicitly below.
      //
      // This used to archive them and stop there, on the stated grounds that
      // `call_logs.lead_id` is ON DELETE CASCADE and the database would clear
      // them out. 001_init_schema.sql does declare the cascade — but the LIVE
      // database does not have it, and deleting a lead failed outright:
      //
      //   update or delete on table "leads" violates foreign key constraint
      //   "call_logs_lead_id_fkey" on table "call_logs"
      //
      // So deleting any lead that had ever been called returned a 500, which is
      // every lead the client actually works with. It escaped notice because a
      // lead with NO call history has nothing pointing at it and deletes fine —
      // which is exactly what a freshly-created throwaway test row is.
      //
      // Clearing the children here rather than re-declaring the constraint is
      // deliberate: it works whether or not the cascade is present, it matches
      // what deleteCampaigns already does for its own dependants, and it needs
      // no migration to be run before deletes start working again.
      const { data: calls, error: callsError } = await supabase
        .from('call_logs')
        .select('*')
        .in('lead_id', chunkIds);
      if (callsError) throw new Error(`Could not read the call history to archive: ${callsError.message}`);

      if (calls?.length) {
        const { error: callTrashError } = await supabase.from('call_logs_trash').insert(
          calls.map((c: any) => ({
            id: c.id,
            batch_id: batchId,
            row_data: c,
            lead_id: c.lead_id,
            dograh_run_id: c.dograh_run_id,
            outcome: c.outcome,
            called_at: c.called_at,
            cascaded: true,
          })),
        );
        if (callTrashError) throw new Error(`Could not archive the call history: ${callTrashError.message}`);
        cascaded += calls.length;
      }

      const { error: trashError } = await supabase.from('leads_trash').insert(
        rows.map((r: any) => ({
          id: r.id,
          batch_id: batchId,
          row_data: r,
          phone: r.phone,
          name: r.name,
          vertical: r.vertical,
          status: r.status,
          qualification: r.qualification,
        })),
      );
      if (trashError) throw new Error(`Could not archive the leads: ${trashError.message}`);

      // Clear the children before the parent. Keyed on lead_id rather than on
      // the ids just archived, because anything that arrived in between is
      // still a row pointing at this lead and would block the delete all over
      // again. A call landing inside that window is deleted without an archive
      // copy — a narrow race, and the alternative is the delete failing.
      const { error: callDelError } = await supabase
        .from('call_logs')
        .delete()
        .in('lead_id', chunkIds);
      if (callDelError) {
        throw new Error(`Could not clear the call history before deleting: ${callDelError.message}`);
      }

      const { error: delError } = await supabase.from('leads').delete().in('id', chunkIds);
      if (delError) throw new Error(`Could not delete the leads: ${delError.message}`);

      deleted += rows.length;
      if (rows.length < CHUNK) break;
    }
  } finally {
    // Runs even when a chunk threw, so whatever WAS archived stays restorable
    // instead of being stranded under a batch that claims to hold nothing.
    await closeBatch(supabase, batchId, deleted, cascaded);
  }

  return {
    batchId: deleted > 0 ? batchId : null,
    deleted,
    cascaded,
    skipped: queuedCount || 0,
    skippedReason: queuedCount ? 'currently being dialled' : null,
    capped,
    filterLabel,
  };
}

// ─── Call logs ───────────────────────────────────────────────────────────────

export interface CallLogFilters {
  /** call_logs.outcome values. */
  outcomes: string[];
  calledBefore: string | null;
  calledAfter: string | null;
  campaignRunId: string | null;
  vertical: Vertical | null;
}

export function parseCallLogFilters(params: URLSearchParams): CallLogFilters {
  const iso = (raw: string | null) => {
    if (!raw) return null;
    const d = new Date(raw);
    return Number.isNaN(d.getTime()) ? null : d.toISOString();
  };
  return {
    outcomes: (params.get('outcome') || '')
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean),
    calledBefore: iso(params.get('calledBefore')),
    calledAfter: iso(params.get('calledAfter')),
    campaignRunId: params.get('campaignRunId') || null,
    vertical: verticalFromParam(params.get('vertical')),
  };
}

export function isUnfilteredCallLogQuery(f: CallLogFilters): boolean {
  return (
    f.outcomes.length === 0 && !f.calledBefore && !f.calledAfter && !f.campaignRunId && !f.vertical
  );
}

/** Same wording the Call History table uses, so the dialog and the table agree. */
const OUTCOME_LABELS: Record<string, string> = {
  completed: 'connected',
  no_answer: 'no answer',
  busy: 'busy',
  voicemail: 'voicemail',
  failed: 'failed',
  cancelled: 'cancelled',
};

export function describeCallLogFilters(f: CallLogFilters): string {
  const parts: string[] = [];
  if (f.outcomes.length) parts.push(f.outcomes.map((o) => OUTCOME_LABELS[o] ?? o).join(' or '));
  if (f.vertical) parts.push(VERTICAL_LABELS[f.vertical]);
  if (f.campaignRunId) parts.push('from one campaign');
  if (f.calledAfter) parts.push(`called on or after ${new Date(f.calledAfter).toLocaleDateString('en-IN')}`);
  if (f.calledBefore) parts.push(`called before ${new Date(f.calledBefore).toLocaleDateString('en-IN')}`);
  return parts.length ? `Call logs: ${parts.join(', ')}` : 'All call logs';
}

/**
 * Deletes call history WITHOUT touching the leads.
 *
 * The vertical filter goes through `leads!inner`, because a call has no business
 * line of its own - it inherits the one on the lead it was placed to. `!inner`
 * is required: with a plain join, filtering on a joined column returns every row
 * and merely nulls the lead, so an unfiltered wipe would masquerade as a
 * filtered one. Same trap the calls list route documents.
 */
export async function deleteCallLogs(
  supabase: Db,
  opts: { filters: CallLogFilters; ids?: string[]; filterLabel: string; deletedBy?: string | null },
): Promise<DeleteResult> {
  const { filters, ids, filterLabel, deletedBy = null } = opts;

  const batchId = await openBatch(supabase, 'call_log', filterLabel, { ...filters, ids: ids ?? null }, deletedBy);

  let deleted = 0;
  let capped = false;

  try {
    for (let i = 0; i < MAX_ITERATIONS; i++) {
      const remaining = MAX_ROWS_PER_DELETE - deleted;
      if (remaining <= 0) {
        capped = true;
        break;
      }

      let query = filters.vertical
        ? supabase.from('call_logs').select('*, leads!inner (vertical)').eq('leads.vertical', filters.vertical)
        : supabase.from('call_logs').select('*');

      if (filters.outcomes.length === 1) query = query.eq('outcome', filters.outcomes[0]);
      else if (filters.outcomes.length > 1) query = query.in('outcome', filters.outcomes);
      if (filters.calledBefore) query = query.lt('called_at', filters.calledBefore);
      if (filters.calledAfter) query = query.gte('called_at', filters.calledAfter);
      if (filters.campaignRunId) query = query.eq('campaign_run_id', filters.campaignRunId);
      if (ids?.length) query = query.in('id', ids);

      const { data: rows, error } = await query.limit(Math.min(CHUNK, remaining));
      if (error) throw new Error(`Could not read the call logs to delete: ${error.message}`);
      if (!rows?.length) break;

      const { error: trashError } = await supabase.from('call_logs_trash').insert(
        rows.map((c: any) => {
          // The join adds a `leads` key that is not a call_logs column. Storing
          // it would make a restore fail with "column leads does not exist".
          const { leads: _joined, ...row } = c;
          return {
            id: row.id,
            batch_id: batchId,
            row_data: row,
            lead_id: row.lead_id,
            dograh_run_id: row.dograh_run_id,
            outcome: row.outcome,
            called_at: row.called_at,
            cascaded: false,
          };
        }),
      );
      if (trashError) throw new Error(`Could not archive the call logs: ${trashError.message}`);

      const { error: delError } = await supabase
        .from('call_logs')
        .delete()
        .in('id', rows.map((r: any) => r.id));
      if (delError) throw new Error(`Could not delete the call logs: ${delError.message}`);

      deleted += rows.length;
      if (rows.length < CHUNK) break;
    }
  } finally {
    await closeBatch(supabase, batchId, deleted, 0);
  }

  return { batchId: deleted > 0 ? batchId : null, deleted, cascaded: 0, skipped: 0, skippedReason: null, capped, filterLabel };
}

// ─── Campaigns ───────────────────────────────────────────────────────────────

export interface CampaignFilters {
  statuses: string[];
  createdBefore: string | null;
  createdAfter: string | null;
  vertical: Vertical | null;
}

export function parseCampaignFilters(params: URLSearchParams): CampaignFilters {
  const iso = (raw: string | null) => {
    if (!raw) return null;
    const d = new Date(raw);
    return Number.isNaN(d.getTime()) ? null : d.toISOString();
  };
  return {
    statuses: (params.get('status') || '')
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean),
    createdBefore: iso(params.get('createdBefore')),
    createdAfter: iso(params.get('createdAfter')),
    vertical: verticalFromParam(params.get('vertical')),
  };
}

export function isUnfilteredCampaignQuery(f: CampaignFilters): boolean {
  return f.statuses.length === 0 && !f.createdBefore && !f.createdAfter && !f.vertical;
}

export function describeCampaignFilters(f: CampaignFilters): string {
  const parts: string[] = [];
  if (f.statuses.length) parts.push(f.statuses.join(' or '));
  if (f.vertical) parts.push(VERTICAL_LABELS[f.vertical]);
  if (f.createdAfter) parts.push(`started on or after ${new Date(f.createdAfter).toLocaleDateString('en-IN')}`);
  if (f.createdBefore) parts.push(`started before ${new Date(f.createdBefore).toLocaleDateString('en-IN')}`);
  return parts.length ? `Campaigns: ${parts.join(', ')}` : 'All finished campaigns';
}

/** Only these can ever be deleted - see the note in deleteCampaigns. */
const DELETABLE_CAMPAIGN_STATUSES = ['completed', 'failed', 'cancelled'];

/**
 * Deletes finished campaign runs.
 *
 * A running, queued, pending or paused campaign is NEVER deleted, whatever the
 * filter says. Its row is the only handle this system has on the Dograh campaign
 * it started: the reconcile sweep finds live calls through
 * `campaign_runs.dograh_campaign_id`, and the billing meter charges through the
 * same row. Delete it mid-flight and the provider keeps dialling and keeps
 * charging while the dashboard has lost the ability to see it, stop it, or bill
 * it. The restriction is applied inside the query, so a protected campaign is
 * never re-fetched and the paging loop terminates.
 *
 * `leads.campaign_run_id` and `call_logs.campaign_run_id` are declared ON DELETE
 * SET NULL in scripts/001_init_schema.sql, but the LIVE database has them as
 * RESTRICT — so this does NOT rely on the constraint. Both are nulled out
 * explicitly before the campaign row goes (see the note at the delete itself).
 * The detached ids are recorded on the trash row so a restore can reattach them.
 */
export async function deleteCampaigns(
  supabase: Db,
  opts: { filters: CampaignFilters; ids?: string[]; filterLabel: string; deletedBy?: string | null },
): Promise<DeleteResult> {
  const { filters, ids, filterLabel, deletedBy = null } = opts;

  // The statuses the caller asked for, narrowed to the ones we allow. An empty
  // intersection means they asked only for live campaigns - delete nothing.
  const requested = filters.statuses.length
    ? filters.statuses.filter((s) => DELETABLE_CAMPAIGN_STATUSES.includes(s))
    : DELETABLE_CAMPAIGN_STATUSES;

  let liveQuery = supabase.from('campaign_runs').select('id', { count: 'exact', head: true }).not('status', 'in', `(${DELETABLE_CAMPAIGN_STATUSES.join(',')})`);
  if (ids?.length) liveQuery = liveQuery.in('id', ids);
  const { count: liveCount } = await liveQuery;

  if (requested.length === 0) {
    return {
      batchId: null,
      deleted: 0,
      cascaded: 0,
      skipped: liveCount || 0,
      skippedReason: 'still running, paused or queued',
      capped: false,
      filterLabel,
    };
  }

  const batchId = await openBatch(supabase, 'campaign', filterLabel, { ...filters, ids: ids ?? null }, deletedBy);

  let deleted = 0;
  let capped = false;

  try {
    for (let i = 0; i < MAX_ITERATIONS; i++) {
      const remaining = MAX_ROWS_PER_DELETE - deleted;
      if (remaining <= 0) {
        capped = true;
        break;
      }

      let query = supabase.from('campaign_runs').select('*').in('status', requested);
      if (filters.vertical) query = query.eq('vertical', filters.vertical);
      if (filters.createdBefore) query = query.lt('created_at', filters.createdBefore);
      if (filters.createdAfter) query = query.gte('created_at', filters.createdAfter);
      if (ids?.length) query = query.in('id', ids);

      const { data: rows, error } = await query.limit(Math.min(CHUNK, remaining));
      if (error) throw new Error(`Could not read the campaigns to delete: ${error.message}`);
      if (!rows?.length) break;

      const chunkIds = rows.map((r: any) => r.id as string);

      const [{ data: linkedLeads }, { data: linkedCalls }] = await Promise.all([
        supabase.from('leads').select('id, campaign_run_id').in('campaign_run_id', chunkIds),
        supabase.from('call_logs').select('id, campaign_run_id').in('campaign_run_id', chunkIds),
      ]);

      const { error: trashError } = await supabase.from('campaign_runs_trash').insert(
        rows.map((r: any) => ({
          id: r.id,
          batch_id: batchId,
          row_data: r,
          campaign_name: r.campaign_name,
          status: r.status,
          vertical: r.vertical,
          relink_lead_ids: (linkedLeads || []).filter((l: any) => l.campaign_run_id === r.id).map((l: any) => l.id),
          relink_call_ids: (linkedCalls || []).filter((c: any) => c.campaign_run_id === r.id).map((c: any) => c.id),
        })),
      );
      if (trashError) throw new Error(`Could not archive the campaigns: ${trashError.message}`);

      /*
       * Detach the children BEFORE deleting the parent.
       *
       * This code was written expecting `leads.campaign_run_id` and
       * `call_logs.campaign_run_id` to be ON DELETE SET NULL — which is exactly
       * what scripts/001_init_schema.sql declares. The LIVE database does not
       * match that file: both constraints are RESTRICT, so Postgres refused
       * every delete with
       *   23503 ... violates foreign key constraint "leads_campaign_run_id_fkey"
       * and campaign deletion could never work for any campaign that had ever
       * dialled anyone — which is all of them.
       *
       * Nulling the references here rather than fixing the constraint keeps
       * this working under EITHER schema, needs no hand-run migration on a live
       * database, and makes the archive/restore pair explicit: the ids were
       * captured into relink_lead_ids/relink_call_ids just above, and
       * restoreBatch puts them back.
       *
       * Leads first: a lead is what the client sees, and if the sweep dies
       * halfway a detached call log is invisible while a detached lead is not.
       */
      for (const [table, column] of [
        ['leads', 'campaign_run_id'],
        ['call_logs', 'campaign_run_id'],
      ] as const) {
        const { error: detachError } = await supabase
          .from(table)
          .update({ [column]: null })
          .in(column, chunkIds);
        if (detachError) {
          throw new Error(`Could not detach ${table} from the campaigns: ${detachError.message}`);
        }
      }

      const { error: delError } = await supabase.from('campaign_runs').delete().in('id', chunkIds);
      if (delError) throw new Error(`Could not delete the campaigns: ${delError.message}`);

      deleted += rows.length;
      if (rows.length < CHUNK) break;
    }
  } finally {
    await closeBatch(supabase, batchId, deleted, 0);
  }

  return {
    batchId: deleted > 0 ? batchId : null,
    deleted,
    cascaded: 0,
    skipped: liveCount || 0,
    skippedReason: liveCount ? 'still running, paused or queued' : null,
    capped,
    filterLabel,
  };
}

// ─── Restore ─────────────────────────────────────────────────────────────────

export interface RestoreResult {
  restored: number;
  restoredCalls: number;
  /** Rows that could not come back, with the reason, e.g. a phone number that
   *  has since been re-uploaded as a new lead. */
  skipped: number;
  skippedReason: string | null;
}

/**
 * Reinserts rows one chunk at a time, falling back to row-by-row on conflict.
 *
 * A bulk insert is all-or-nothing: one lead whose (phone, vertical) has been
 * re-uploaded since the delete would fail the whole chunk and the other 499
 * would stay in the bin. Retrying individually costs a round trip per row but
 * only on the chunks that actually conflict, and it means a restore always
 * brings back everything it possibly can.
 */
/**
 * Columns the DATABASE computes, which therefore must never be written back.
 *
 * `leads.lead_temperature` is `GENERATED ALWAYS`. The archive stores the whole
 * row as jsonb — correctly, so a restore survives the table gaining columns —
 * but that whole row includes the generated one, and Postgres rejects any
 * insert that names it: 428C9 "cannot insert a non-DEFAULT value".
 *
 * This made EVERY lead restore fail, every time, and the failure was invisible:
 * insertTolerant counted the error and threw the message away, and restoreBatch
 * then stamped the batch as restored regardless. The client pressed Restore, saw
 * "Restored", and the lead did not come back. purgeExpired deletes restored
 * batches, so the archived copy was next in line to be swept — the lead would
 * have been destroyed by the feature whose whole purpose is not destroying it.
 *
 * Detected against the live schema: `leads.lead_temperature` is the only one.
 * `stripGenerated` also self-heals if a future migration adds another, because
 * insertTolerant retries on 428C9 with the offending column removed.
 */
const GENERATED_COLUMNS: Record<string, string[]> = {
  leads: ['lead_temperature'],
};

function stripGenerated(table: string, row: any): any {
  const generated = GENERATED_COLUMNS[table];
  if (!generated?.length) return row;
  const copy = { ...row };
  for (const column of generated) delete copy[column];
  return copy;
}

/** The column named in a Postgres 428C9 "is a generated column" error. */
function generatedColumnFromError(message: string | undefined): string | null {
  const match = message?.match(/column "([^"]+)" is a generated column/i)
    ?? message?.match(/cannot insert a non-DEFAULT value into column "([^"]+)"/i);
  return match?.[1] ?? null;
}

async function insertTolerant(
  supabase: Db,
  table: string,
  rows: any[],
): Promise<{ ok: number; failed: number; error: string | null }> {
  if (!rows.length) return { ok: 0, failed: 0, error: null };

  const prepared = rows.map((r) => stripGenerated(table, r));

  const { error } = await supabase.from(table).insert(prepared);
  if (!error) return { ok: prepared.length, failed: 0, error: null };

  // A generated column added since this constant was written: drop it and retry
  // the whole chunk once, rather than losing every row to a schema change.
  const generated = generatedColumnFromError(error.message);
  if (generated) {
    const retried = prepared.map((r) => {
      const copy = { ...r };
      delete copy[generated];
      return copy;
    });
    const { error: retryError } = await supabase.from(table).insert(retried);
    if (!retryError) return { ok: retried.length, failed: 0, error: null };
  }

  // Row by row, so one bad row does not cost the rest of the chunk. The first
  // real error is KEPT and reported — swallowing it is what hid this bug.
  let ok = 0;
  let failed = 0;
  let firstError: string | null = null;
  for (const row of prepared) {
    const { error: rowError } = await supabase.from(table).insert(row);
    if (rowError) {
      failed += 1;
      firstError = firstError ?? rowError.message;
    } else {
      ok += 1;
    }
  }
  return { ok, failed, error: firstError };
}

/** True if a row with this id currently exists - used to avoid restoring a
 *  foreign key that points at something since deleted. */
async function existingIds(supabase: Db, table: string, ids: string[]): Promise<Set<string>> {
  const found = new Set<string>();
  for (let i = 0; i < ids.length; i += CHUNK) {
    const { data } = await supabase.from(table).select('id').in('id', ids.slice(i, i + CHUNK));
    for (const row of data || []) found.add(row.id as string);
  }
  return found;
}

export async function restoreBatch(supabase: Db, batchId: string): Promise<RestoreResult> {
  const { data: batch, error: batchError } = await supabase
    .from('trash_batches')
    .select('*')
    .eq('id', batchId)
    .single();

  if (batchError || !batch) throw new Error('That Recycle Bin entry no longer exists.');
  if (batch.restored_at) throw new Error('That entry has already been restored.');

  let restored = 0;
  let restoredCalls = 0;
  let skipped = 0;
  let skippedReason: string | null = null;
  /** The first real database error, so a failure can say what went wrong. */
  let insertError: string | null = null;

  if (batch.entity === 'campaign') {
    const { data: rows } = await supabase.from('campaign_runs_trash').select('*').eq('batch_id', batchId);
    for (let i = 0; i < (rows?.length || 0); i += CHUNK) {
      const chunk = (rows || []).slice(i, i + CHUNK);
      const res = await insertTolerant(supabase, 'campaign_runs', chunk.map((r: any) => r.row_data));
      restored += res.ok;
      skipped += res.failed;
      if (res.error) insertError = insertError ?? res.error;

      // Reattach the leads and calls that deleteCampaigns detached. It nulls
      // those references explicitly (the live FK is RESTRICT, not SET NULL), so
      // this is the other half of that pair.
      for (const r of chunk) {
        const leadIds = (r.relink_lead_ids || []) as string[];
        const callIds = (r.relink_call_ids || []) as string[];
        for (let j = 0; j < leadIds.length; j += CHUNK) {
          await supabase.from('leads').update({ campaign_run_id: r.id }).in('id', leadIds.slice(j, j + CHUNK));
        }
        for (let j = 0; j < callIds.length; j += CHUNK) {
          await supabase.from('call_logs').update({ campaign_run_id: r.id }).in('id', callIds.slice(j, j + CHUNK));
        }
      }
    }
    if (skipped) skippedReason = 'a campaign with the same id already exists';
  }

  if (batch.entity === 'lead') {
    const { data: rows } = await supabase.from('leads_trash').select('*').eq('batch_id', batchId);

    // A lead's campaign_run_id may point at a campaign deleted separately since.
    // Restoring it as-is would fail the foreign key and lose the whole lead, so
    // the link is dropped and the lead comes back unattached - which is the
    // lesser loss by a wide margin.
    const campaignIds = [...new Set((rows || []).map((r: any) => r.row_data?.campaign_run_id).filter(Boolean))] as string[];
    const liveCampaigns = campaignIds.length ? await existingIds(supabase, 'campaign_runs', campaignIds) : new Set<string>();

    const batchIds = [...new Set((rows || []).map((r: any) => r.row_data?.batch_id).filter(Boolean))] as string[];
    const liveBatches = batchIds.length ? await existingIds(supabase, 'upload_batches', batchIds) : new Set<string>();

    for (let i = 0; i < (rows?.length || 0); i += CHUNK) {
      const chunk = (rows || []).slice(i, i + CHUNK).map((r: any) => {
        const row = { ...r.row_data };
        if (row.campaign_run_id && !liveCampaigns.has(row.campaign_run_id)) row.campaign_run_id = null;
        if (row.batch_id && !liveBatches.has(row.batch_id)) row.batch_id = null;
        return row;
      });
      const res = await insertTolerant(supabase, 'leads', chunk);
      restored += res.ok;
      skipped += res.failed;
      if (res.error) insertError = insertError ?? res.error;
    }
    // The old wording asserted a duplicate phone number as the cause without
    // checking. It was wrong for the failure that actually happened for months
    // — a generated column — and sent the reader looking in the wrong place.
    // Report a duplicate only when the database says duplicate.
    if (skipped) {
      skippedReason = insertError?.includes('duplicate key')
        ? 'the same phone number has been re-uploaded on that business line since'
        : insertError ?? 'the database refused the row';
    }
  }

  // Call logs come last in every case: `call_logs.lead_id` points at `leads`, so
  // a cascaded call can only be reinserted once its lead is back.
  const { data: callRows } = await supabase.from('call_logs_trash').select('*').eq('batch_id', batchId);
  if (callRows?.length) {
    const leadIds = [...new Set(callRows.map((r: any) => r.row_data?.lead_id).filter(Boolean))] as string[];
    const liveLeads = leadIds.length ? await existingIds(supabase, 'leads', leadIds) : new Set<string>();

    const campaignIds = [...new Set(callRows.map((r: any) => r.row_data?.campaign_run_id).filter(Boolean))] as string[];
    const liveCampaigns = campaignIds.length ? await existingIds(supabase, 'campaign_runs', campaignIds) : new Set<string>();

    // A call log whose lead is gone for good would be an orphan row nothing can
    // display, so it is left in the bin rather than restored into limbo.
    const restorable = callRows.filter((r: any) => !r.row_data?.lead_id || liveLeads.has(r.row_data.lead_id));
    const orphaned = callRows.length - restorable.length;

    for (let i = 0; i < restorable.length; i += CHUNK) {
      const chunk = restorable.slice(i, i + CHUNK).map((r: any) => {
        const row = { ...r.row_data };
        if (row.campaign_run_id && !liveCampaigns.has(row.campaign_run_id)) row.campaign_run_id = null;
        return row;
      });
      const res = await insertTolerant(supabase, 'call_logs', chunk);
      restoredCalls += res.ok;
      skipped += res.failed;
      if (res.error) insertError = insertError ?? res.error;
    }

    if (orphaned) {
      skipped += orphaned;
      skippedReason = skippedReason ?? 'their lead was permanently deleted';
    }
    if (batch.entity === 'call_log') restored = restoredCalls;
  }

  // ONLY stamp the batch restored if something actually came back.
  //
  // This used to run unconditionally. Combined with insertTolerant swallowing
  // its errors it produced the worst failure this feature can have: the bin
  // said "Restored", the rows were still archived and not live, and
  // purgeExpired — which deletes restored batches — was then free to wipe the
  // only remaining copy. A restore that restored nothing is a FAILURE, and it
  // now says so and leaves the batch exactly where it is, still recoverable.
  if (restored === 0 && restoredCalls === 0) {
    throw new Error(
      skippedReason
        ? `Nothing could be restored: ${skippedReason}. It is still in the Recycle bin.`
        : 'Nothing could be restored. It is still in the Recycle bin, so nothing has been lost.',
    );
  }

  await supabase.from('trash_batches').update({ restored_at: new Date().toISOString() }).eq('id', batchId);

  return { restored, restoredCalls: batch.entity === 'call_log' ? 0 : restoredCalls, skipped, skippedReason };
}

// ─── Purge ───────────────────────────────────────────────────────────────────

/**
 * How many archived rows in this batch are NOT currently live.
 *
 * This is the only trustworthy answer to "is it safe to throw the archive
 * away?", and `restored_at` is not that answer. A restore is per-row and is
 * allowed to partially succeed: restore 4 leads, have 1 rejected because its
 * phone number was re-uploaded on that business line since, and the batch is
 * still stamped restored because 3 did come back. For that 1 lead the archived
 * copy is then the ONLY copy in existence.
 *
 * Measured against the live tables rather than stored on the batch, so it stays
 * correct when a restored row is deleted again afterwards.
 */
export async function outstandingRows(supabase: Db, batchId: string): Promise<number> {
  let outstanding = 0;

  for (const [trashTable, liveTable] of [
    ['leads_trash', 'leads'],
    ['call_logs_trash', 'call_logs'],
    ['campaign_runs_trash', 'campaign_runs'],
  ] as const) {
    const { data: archived } = await supabase.from(trashTable).select('id').eq('batch_id', batchId);
    const ids = (archived ?? []).map((r: any) => r.id as string);
    if (!ids.length) continue;

    const live = await existingIds(supabase, liveTable, ids);
    outstanding += ids.filter((id) => !live.has(id)).length;
  }

  return outstanding;
}

/**
 * Permanent deletion. Removing the batch row is enough: all three `*_trash`
 * tables reference it ON DELETE CASCADE, so the archived rows go with it.
 *
 * REFUSES by default while the batch still holds rows that are not live, even
 * when the batch is stamped restored — see outstandingRows. Destroying the last
 * copy of a record is exactly the outcome this whole feature exists to prevent,
 * and the caller that asks for it is usually acting on a stale "already put
 * back" label. `force` is for a caller that has been told the real number and
 * still means it.
 */
export async function purgeBatch(
  supabase: Db,
  batchId: string,
  opts: { force?: boolean } = {},
): Promise<void> {
  if (!opts.force) {
    const outstanding = await outstandingRows(supabase, batchId);
    if (outstanding > 0) {
      throw new PurgeWouldLoseDataError(outstanding);
    }
  }

  const { error } = await supabase.from('trash_batches').delete().eq('id', batchId);
  if (error) throw new Error(`Could not empty that Recycle Bin entry: ${error.message}`);
}

/** Thrown when a purge would destroy the last copy of a record. */
export class PurgeWouldLoseDataError extends Error {
  constructor(public readonly outstanding: number) {
    super(
      `${outstanding.toLocaleString('en-IN')} record${outstanding === 1 ? '' : 's'} in this entry ` +
        `${outstanding === 1 ? 'is' : 'are'} not in your live data — this is the only copy. ` +
        `Deleting it cannot be undone.`,
    );
    this.name = 'PurgeWouldLoseDataError';
  }
}

/**
 * "Empty bin" — every batch. The widest-reaching destructive action in the app.
 *
 * Returns what it destroyed AND what it would destroy, so the route can refuse
 * a first attempt that would take the last copy of live-missing records with it.
 * Without `force` this counts first and deletes nothing if anything is owing.
 */
export async function purgeAll(
  supabase: Db,
  opts: { force?: boolean } = {},
): Promise<{ purged: number; outstanding: number }> {
  if (!opts.force) {
    const { data: all } = await supabase.from('trash_batches').select('id').limit(500);
    let outstanding = 0;
    for (const batch of all ?? []) outstanding += await outstandingRows(supabase, batch.id);
    if (outstanding > 0) return { purged: 0, outstanding };
  }

  const { data, error } = await supabase.from('trash_batches').delete().not('id', 'is', null).select('id');
  if (error) throw new Error(`Could not empty the Recycle Bin: ${error.message}`);
  return { purged: data?.length || 0, outstanding: 0 };
}

/**
 * The automatic sweep. Called from the reconcile cron, which already runs every
 * ten minutes, so the retention window needs no scheduler of its own.
 *
 * Restored batches are swept too: once the rows are back in the live tables,
 * their archived copies are dead weight and keeping them would defeat the point
 * of the feature, which is that the database stops growing.
 */
export async function purgeExpired(supabase: Db): Promise<number> {
  const now = new Date().toISOString();

  // Batches past their retention window go unconditionally: seven days is the
  // promise the bin makes, and a row that has run out its clock has had its
  // chance. This is the one deletion the client has actually been told about.
  const { data: expired, error: expiredError } = await supabase
    .from('trash_batches')
    .delete()
    .lte('purge_after', now)
    .select('id');

  if (expiredError) console.error('[trash:purgeExpired] expiry sweep failed', expiredError);

  /*
   * Restored batches are swept only once NOTHING is left owing.
   *
   * This used to delete every batch with a `restored_at`, on the reasoning that
   * archived copies of live rows are dead weight. True — but a restore is
   * per-row and may partially succeed, and the batch is stamped restored if even
   * one row came back. Any row that did NOT come back exists only in the
   * archive, so the old sweep permanently destroyed it within ten minutes, with
   * no human involved and nothing reported. That is the worst possible failure
   * for a Recycle Bin, and it ran on a cron.
   *
   * Checked per batch against the live tables. The candidate list is small — a
   * batch appears here at most once, because a clean one is deleted on the spot.
   */
  const { data: restored, error: restoredError } = await supabase
    .from('trash_batches')
    .select('id')
    .not('restored_at', 'is', null)
    .gt('purge_after', now)
    .limit(200);

  if (restoredError) console.error('[trash:purgeExpired] restored sweep failed', restoredError);

  let sweptRestored = 0;
  for (const batch of restored ?? []) {
    const outstanding = await outstandingRows(supabase, batch.id);
    if (outstanding > 0) {
      // Left in the bin on purpose, and left restorable: these rows are still
      // only in the archive, so the client can try the restore again once the
      // collision that blocked them is cleared.
      console.warn(
        `[trash:purgeExpired] keeping batch ${batch.id}: ${outstanding} row(s) never made it back`,
      );
      continue;
    }
    const { error } = await supabase.from('trash_batches').delete().eq('id', batch.id);
    if (!error) sweptRestored += 1;
  }

  return (expired?.length || 0) + sweptRestored;
}
