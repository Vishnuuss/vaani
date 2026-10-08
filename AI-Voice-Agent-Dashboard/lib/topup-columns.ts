import { gstOn, GST_RATE_PERCENT, type GstBreakdown } from '@/lib/billing';

/**
 * Reading and writing `topup_requests` when the GST columns may not exist yet.
 *
 * scripts/004_gst_on_topups.sql adds `base_amount_inr`, `gst_rate_percent` and
 * `gst_amount_inr` to the BILLING database. It was written on 2026-08-06 and, as
 * of 2026-08-20, had never been run — so every read of those columns came back
 * 42703 and BOTH halves of the top-up route 500'd:
 *
 *   GET  — the dialog could not load, and its 30s poll filled the log with errors.
 *   POST — the insert names the same three columns, so filing a request failed too.
 *
 * That second one is the serious half. This is the only route through which money
 * enters the system, so a missing migration silently stopped the client from
 * being able to buy credits at all.
 *
 * The same argument as lib/lead-update.ts applies, only with money instead of a
 * score: "the migration is always run first" is not an assumption worth betting
 * the revenue path on. This degrades to the pre-004 shape instead of failing, and
 * warns once per request so the gap stays visible rather than becoming permanent.
 *
 * RUN scripts/004_gst_on_topups.sql. This is a safety net, not the fix — until it
 * is run, the breakdown is reconstructed on read rather than stored, so a future
 * change to GST_RATE_PERCENT would silently restate old requests.
 */

/** Columns added by 004_gst_on_topups.sql. */
export const GST_COLUMNS = ['base_amount_inr', 'gst_rate_percent', 'gst_amount_inr'] as const;

/** Postgres: undefined_column. PostgREST passes the SQLSTATE through as `code`. */
const UNDEFINED_COLUMN = '42703';

/** Every column the top-up UI needs, in the shape it expects. */
export const TOPUP_SELECT_FULL =
  'id, credits_requested, base_amount_inr, gst_rate_percent, gst_amount_inr, amount_inr, status, reference_note, requested_at, decided_at, decision_note';

/** The same list minus the three columns 004 adds. */
export const TOPUP_SELECT_LEGACY =
  'id, credits_requested, amount_inr, status, reference_note, requested_at, decided_at, decision_note';

/** Does this PostgREST error mean one of the GST columns is missing? */
export function isMissingGstColumn(error: { code?: string; message?: string } | null): boolean {
  if (!error) return false;
  const message = String(error.message ?? '');
  return error.code === UNDEFINED_COLUMN || GST_COLUMNS.some((c) => message.includes(c));
}

interface LegacyRow {
  credits_requested?: number | string | null;
  amount_inr?: number | string | null;
  [key: string]: unknown;
}

/** The three columns 004 adds, in the snake_case shape the UI reads. */
export interface TopupGstFields {
  base_amount_inr: number;
  gst_rate_percent: number;
  gst_amount_inr: number;
}

/**
 * Reconstruct the GST breakdown of a row stored before 004 was applied.
 *
 * `amount_inr` has always meant "what the client actually pays", so it is the
 * one number that is trustworthy either way, and the breakdown is inferred from
 * how it relates to the credits:
 *
 *   amount == credits          A request quoted before GST existed. It was paid
 *                              tax-free, so it is reported tax-free. This is
 *                              exactly what 004's backfill does, and it is why
 *                              tax must never be retro-added to a settled payment.
 *   amount == gstOn(credits)   Filed after GST existed but while the columns were
 *                              missing, so amount_inr already holds the gross.
 *                              gstOn() reproduces the split it was charged on.
 *   anything else              Unknown provenance. Report it as tax-free rather
 *                              than inventing a tax figure that was never charged.
 */
export function withDerivedGst<T extends LegacyRow>(row: T): T & TopupGstFields {
  const credits = Number(row.credits_requested) || 0;
  const amount = Number(row.amount_inr) || 0;
  const quoted = gstOn(credits);

  if (amount === quoted.totalInr && amount !== credits) {
    return {
      ...row,
      base_amount_inr: quoted.baseInr,
      gst_rate_percent: quoted.gstRatePercent,
      gst_amount_inr: quoted.gstInr,
    };
  }

  return {
    ...row,
    base_amount_inr: amount,
    gst_rate_percent: 0,
    gst_amount_inr: 0,
  };
}

/**
 * The row to insert for a new request, given whether the GST columns exist.
 *
 * `amount_inr` is the GROSS in both shapes. It has to be: the QR route computes
 * the payable figure from gstOn() independently, so storing the net here would
 * put a different number on the request than on the QR the client actually pays.
 */
export function topupInsert(
  base: Record<string, unknown>,
  gst: GstBreakdown,
  includeGstColumns: boolean,
): Record<string, unknown> {
  const row = { ...base, amount_inr: gst.totalInr };
  if (!includeGstColumns) return row;
  return {
    ...row,
    base_amount_inr: gst.baseInr,
    gst_rate_percent: gst.gstRatePercent,
    gst_amount_inr: gst.gstInr,
  };
}

/** One-line warning, worded so the reader knows what to run. */
export function warnMissingGstColumns(where: string): void {
  console.warn(
    `[${where}] topup_requests is missing ${GST_COLUMNS.join(', ')}; served the pre-GST shape. ` +
      'Run scripts/004_gst_on_topups.sql against the BILLING database to store the breakdown.',
  );
}

export { GST_RATE_PERCENT };
