/**
 * Writing a lead update that may contain columns the database does not have yet.
 *
 * scripts/007_solar_fields.sql adds `house_ownership` and `solar_planning`. Until
 * someone runs it by hand in the Supabase SQL editor, those columns do not exist,
 * and PostgREST rejects the WHOLE update with 42703 ("column does not exist") -
 * so a solar call would lose its score, its status, its retry count and its
 * recording, not just the two new fields. Migration 006 sat unapplied for a day,
 * so "the migration is always run first" is not an assumption worth betting a
 * call result on.
 *
 * This retries once without the optional columns and logs a one-line warning. The
 * values are still safe in qual_data either way, so nothing is actually lost -
 * the columns are a convenience for querying, not the source of truth.
 */

/** Columns added by a migration that may not have been applied yet. */
const OPTIONAL_COLUMNS = ['house_ownership', 'solar_planning'] as const;

/** Postgres: undefined_column. PostgREST passes the SQLSTATE through as `code`. */
const UNDEFINED_COLUMN = '42703';

export async function updateLead(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  supabase: any,
  leadId: string,
  update: Record<string, any>,
): Promise<{ error: any | null; droppedColumns: string[] }> {
  const { error } = await supabase.from('leads').update(update).eq('id', leadId);
  if (!error) return { error: null, droppedColumns: [] };

  const present = OPTIONAL_COLUMNS.filter((c) => c in update);
  const looksLikeMissingColumn =
    error.code === UNDEFINED_COLUMN ||
    present.some((c) => String(error.message ?? '').includes(c));

  if (present.length === 0 || !looksLikeMissingColumn) return { error, droppedColumns: [] };

  const retry = { ...update };
  for (const c of present) delete retry[c];

  console.warn(
    `[lead-update] ${present.join(', ')} missing from the leads table; wrote the rest. ` +
      'Run scripts/007_solar_fields.sql in the Supabase SQL editor to store them as columns. ' +
      'The values are still in qual_data.',
  );

  const { error: retryError } = await supabase.from('leads').update(retry).eq('id', leadId);
  return { error: retryError ?? null, droppedColumns: [...present] };
}
