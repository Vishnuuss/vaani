/**
 * The feedback questionnaire, defined once.
 *
 * The form component renders from these lists and the API validates against
 * them, so a new option cannot appear in the UI without the server accepting it
 * — the usual way a survey ends up silently discarding answers.
 *
 * Scope today is deliberately the two business lines that actually have a built
 * agent: solar and loan, plus "both". Real estate and investing are in
 * lib/verticals.ts as categories the dashboard can display, but nobody has been
 * called by one, so asking a client to rate them would only produce noise.
 */

export const FEEDBACK_VERTICALS = [
  { value: 'solar', label: 'Solar agent' },
  { value: 'loan', label: 'Loan agent' },
  { value: 'both', label: 'Both' },
] as const;

export const QUALIFICATION_CHANGE_OPTIONS = [
  { value: 'much_better', label: 'Improved a lot' },
  { value: 'better', label: 'Improved a little' },
  { value: 'same', label: 'About the same' },
  { value: 'worse', label: 'Got worse' },
  { value: 'too_early', label: 'Too early to say' },
] as const;

export const HOURS_SAVED_OPTIONS = [
  { value: 'none', label: 'None yet' },
  { value: 'under_5', label: 'Under 5 hours' },
  { value: '5_to_15', label: '5 to 15 hours' },
  { value: 'over_15', label: 'More than 15 hours' },
] as const;

export const RATING_QUESTIONS = [
  { key: 'dashboard_rating', label: 'How easy is the dashboard to use?' },
  { key: 'voice_rating', label: 'How natural does the AI voice sound on calls?' },
  { key: 'understanding_rating', label: 'Does the agent understand your customers correctly?' },
] as const;

export type RatingKey = (typeof RATING_QUESTIONS)[number]['key'];

const VERTICAL_VALUES = new Set(FEEDBACK_VERTICALS.map((v) => v.value as string));
const CHANGE_VALUES = new Set(QUALIFICATION_CHANGE_OPTIONS.map((v) => v.value as string));
const HOURS_VALUES = new Set(HOURS_SAVED_OPTIONS.map((v) => v.value as string));

export interface FeedbackSubmission {
  vertical: string;
  dashboard_rating: number | null;
  voice_rating: number | null;
  understanding_rating: number | null;
  qualification_change: string | null;
  qualified_before_week: number | null;
  qualified_after_week: number | null;
  hours_saved: string | null;
  improvements: string | null;
  recommend_score: number | null;
}

export class FeedbackValidationError extends Error {}

function optionalRating(value: unknown, min: number, max: number, field: string): number | null {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  if (!Number.isInteger(n) || n < min || n > max) {
    throw new FeedbackValidationError(`${field} must be a whole number between ${min} and ${max}.`);
  }
  return n;
}

function optionalCount(value: unknown, field: string): number | null {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  // Capped rather than unbounded: this is a per-week lead count typed by hand,
  // and a slipped keystroke should be rejected, not stored and later averaged.
  if (!Number.isInteger(n) || n < 0 || n > 100_000) {
    throw new FeedbackValidationError(`${field} must be a whole number of leads.`);
  }
  return n;
}

function optionalChoice(value: unknown, allowed: Set<string>, field: string): string | null {
  if (value === null || value === undefined || value === '') return null;
  const s = String(value);
  if (!allowed.has(s)) throw new FeedbackValidationError(`${field} is not one of the offered answers.`);
  return s;
}

/**
 * Validates a submitted form.
 *
 * Only `vertical` is required. Everything else is optional on purpose: a client
 * who answers three questions and closes the dialog has told us something, and
 * a form that refuses to save unless it is complete is a form that mostly does
 * not get submitted at all.
 */
export function validateFeedback(raw: unknown): FeedbackSubmission {
  if (!raw || typeof raw !== 'object') throw new FeedbackValidationError('Invalid form data.');
  const body = raw as Record<string, unknown>;

  const vertical = String(body.vertical ?? '');
  if (!VERTICAL_VALUES.has(vertical)) {
    throw new FeedbackValidationError('Please choose which agent you are rating.');
  }

  return {
    vertical,
    dashboard_rating: optionalRating(body.dashboard_rating, 1, 5, 'Dashboard rating'),
    voice_rating: optionalRating(body.voice_rating, 1, 5, 'Voice rating'),
    understanding_rating: optionalRating(body.understanding_rating, 1, 5, 'Understanding rating'),
    qualification_change: optionalChoice(body.qualification_change, CHANGE_VALUES, 'Qualification change'),
    qualified_before_week: optionalCount(body.qualified_before_week, 'Qualified leads before'),
    qualified_after_week: optionalCount(body.qualified_after_week, 'Qualified leads now'),
    hours_saved: optionalChoice(body.hours_saved, HOURS_VALUES, 'Time saved'),
    improvements: body.improvements ? String(body.improvements).slice(0, 4000) : null,
    recommend_score: optionalRating(body.recommend_score, 0, 10, 'Recommendation score'),
  };
}
