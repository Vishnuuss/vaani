/**
 * The four business lines the voice agents sell for.
 *
 * Each vertical gets its own Dograh workflow, and each workflow extracts a
 * `vertical` variable naming itself. That is what lets the dashboard say
 * "score 100 from loan" rather than just "score 100" - once solar, real estate
 * and investing agents exist, a single lead list holds all four and the score
 * alone would be ambiguous.
 *
 * Only `loan` has a built agent today (Dograh workflow 3). The other three are
 * defined here so the dashboard already shows the categories, and so adding an
 * agent later is a workflow change rather than a code change.
 */

export const VERTICALS = ['loan', 'solar', 'realestate', 'investing'] as const;

export type Vertical = (typeof VERTICALS)[number];

export const DEFAULT_VERTICAL: Vertical = 'loan';

export const VERTICAL_LABELS: Record<Vertical, string> = {
  loan: 'Loan',
  solar: 'Solar',
  realestate: 'Real Estate',
  investing: 'Investing',
};

/** Tailwind classes per vertical, so a lead's business line is readable at a glance. */
export const VERTICAL_STYLES: Record<Vertical, { color: string; bg: string }> = {
  loan: { color: 'text-emerald-500', bg: 'bg-emerald-500/10' },
  solar: { color: 'text-amber-500', bg: 'bg-amber-500/10' },
  realestate: { color: 'text-sky-500', bg: 'bg-sky-500/10' },
  investing: { color: 'text-violet-500', bg: 'bg-violet-500/10' },
};

/**
 * Accepts whatever the agent, the CRM or a human typed and returns a known
 * vertical. Agents are configured by hand in the Dograh dashboard, so the value
 * arrives as free text - "Real Estate", "real-estate" and "property" all mean
 * the same thing. Returns null rather than guessing when nothing matches, so the
 * caller decides whether to fall back to the default.
 */
export function parseVertical(value: unknown): Vertical | null {
  const s = String(value ?? '')
    .trim()
    .toLowerCase()
    .replace(/[\s_-]+/g, '');
  if (!s) return null;

  if (s === 'loan' || s === 'loans' || s === 'finance' || s === 'lending') return 'loan';
  if (s === 'solar' || s === 'solarpanel' || s === 'solarpanels' || s === 'energy') return 'solar';
  if (s === 'realestate' || s === 'property' || s === 'realty' || s === 'housing') return 'realestate';
  if (s === 'investing' || s === 'investment' || s === 'investments' || s === 'wealth' || s === 'mutualfunds') return 'investing';
  return null;
}

export function verticalLabel(value: unknown): string {
  const v = parseVertical(value);
  return v ? VERTICAL_LABELS[v] : VERTICAL_LABELS[DEFAULT_VERTICAL];
}

/**
 * The dashboard's header switch sends `?vertical=` on every list request, with
 * "all" meaning no filter. Returns null for "all", for a missing value and for
 * anything unrecognised - so a bad query string shows everything rather than
 * silently showing one business line's leads under another's heading.
 *
 * Deliberately different from parseVertical: this one is for query strings,
 * where "all" is a legitimate answer.
 */
export function verticalFromParam(value: unknown): Vertical | null {
  const s = String(value ?? '').trim().toLowerCase();
  if (!s || s === 'all') return null;
  return parseVertical(s);
}

/**
 * Applies the header switch to any `leads` query. Null means "All", which is
 * no filter at all - not a four-way `in`, so leads written before the column
 * existed still appear.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function applyVerticalFilter(query: any, vertical: Vertical | null) {
  return vertical ? query.eq('vertical', vertical) : query;
}

/**
 * Guards a lead that was matched to a call BY PHONE NUMBER rather than by id.
 *
 * `leads.phone` used to be globally unique, so a phone lookup could only ever
 * return the one right lead. Once the same person can be a lead in several
 * business lines, that lookup becomes ambiguous, and attaching a solar call's
 * score, recording and transcript to the same person's loan lead is silent
 * corruption of data the client reads and bills from.
 *
 * Returns true when the match should be REJECTED. Deliberately conservative:
 * it only rejects when both sides are actually known to differ.
 *
 * - `leadVertical` undefined -> the column does not exist yet (pre-migration) or
 *   was not selected. No opinion, allow. This is what lets the fix ship BEFORE
 *   the migration without changing today's behaviour.
 * - `expectedVertical` unknown -> the agent did not say which business line it
 *   was calling for (every agent today). No opinion, allow.
 *
 * So this is inert until there is real vertical data on both sides, and from
 * then on it blocks exactly the cross-business-line mismatches.
 */
export function isWrongVerticalMatch(leadVertical: unknown, expectedVertical: unknown): boolean {
  const lead = parseVertical(leadVertical);
  const expected = parseVertical(expectedVertical);
  if (!lead || !expected) return false;
  return lead !== expected;
}
