/**
 * Shared shape of a bulk-delete request, for the three delete routes.
 *
 * The three modes exist because they carry different amounts of risk and so
 * deserve different guards, not because they are three ways of saying the same
 * thing:
 *
 *   'ids'    - the rows the client ticked. Bounded and visible; they can see
 *              exactly what they picked.
 *   'filter' - everything matching the filters currently on screen, including
 *              the pages they have not scrolled to. They are shown a count.
 *   'all'    - every row in the table. No filter at all.
 *
 * `mode` is required and never inferred. An earlier design let an empty filter
 * object mean "everything", which turns one forgotten parameter on the client
 * into a wiped table. Deleting everything now takes deliberately saying so.
 */

export type BulkDeleteMode = 'ids' | 'filter' | 'all';

export interface BulkDeleteBody {
  mode: BulkDeleteMode;
  ids: string[];
  filters: URLSearchParams;
  /** When true, return the count and the description without deleting. */
  preview: boolean;
  /** The row count the client typed to confirm a large delete. */
  confirmCount: number | null;
}

/** Deletes at or above this size must be confirmed by typing the number. */
export const TYPE_TO_CONFIRM_THRESHOLD = 500;

export class BulkDeleteRequestError extends Error {}

export function parseBulkDeleteBody(raw: unknown): BulkDeleteBody {
  if (!raw || typeof raw !== 'object') {
    throw new BulkDeleteRequestError('Invalid request body.');
  }
  const body = raw as Record<string, unknown>;

  const mode = body.mode;
  if (mode !== 'ids' && mode !== 'filter' && mode !== 'all') {
    throw new BulkDeleteRequestError('mode must be one of "ids", "filter" or "all".');
  }

  const ids = Array.isArray(body.ids) ? body.ids.filter((v): v is string => typeof v === 'string' && !!v) : [];
  if (mode === 'ids' && ids.length === 0) {
    throw new BulkDeleteRequestError('No rows were selected.');
  }
  // One request should not carry an unbounded id list; the client pages instead.
  if (ids.length > 2000) {
    throw new BulkDeleteRequestError('Too many rows selected at once. Use the filter instead.');
  }

  // 'all' means no filter, whatever was sent alongside it.
  const filters = new URLSearchParams();
  if (mode !== 'all' && body.filters && typeof body.filters === 'object') {
    for (const [key, value] of Object.entries(body.filters as Record<string, unknown>)) {
      if (value === null || value === undefined || value === '') continue;
      filters.set(key, String(value));
    }
  }

  const confirmCount =
    typeof body.confirmCount === 'number' && Number.isFinite(body.confirmCount)
      ? body.confirmCount
      : null;

  return { mode, ids, filters, preview: body.preview === true, confirmCount };
}

/**
 * Enforces the type-the-number confirmation.
 *
 * Checked on the SERVER against the count the server just measured, not against
 * whatever the dialog believed. A stale dialog showing "12 rows" must not be
 * able to authorise deleting 40,000 because the filter matched more by the time
 * the request arrived.
 */
export function requireConfirmation(mode: BulkDeleteMode, count: number, confirmCount: number | null) {
  const needsConfirmation = mode === 'all' || count >= TYPE_TO_CONFIRM_THRESHOLD;
  if (!needsConfirmation) return;

  if (confirmCount !== count) {
    throw new BulkDeleteRequestError(
      `This will delete ${count.toLocaleString('en-IN')} rows. Type ${count} to confirm.`,
    );
  }
}
