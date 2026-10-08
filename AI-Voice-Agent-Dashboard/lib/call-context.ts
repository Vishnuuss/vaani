/**
 * Maps whatever Dograh sends into the small set of signals we score and store.
 *
 * Two things made this necessary:
 *  1. The live workflow ("BS Financial Services — Telugu Loan Agent") extracts
 *     loan_required / loan_type / loan_amount / profession / qualified / summary,
 *     while the webhook handler only understood the older real-estate field names
 *     (interested / budget / visit_date). Every answered call therefore scored a
 *     flat 10 and was written off as not_qualified with an empty budget.
 *  2. Depending on the delivery path the values arrive either flat on the payload,
 *     or nested under gathered_context / extracted_variables / variables / context.
 *
 * As of the two-question agent the workflow also sends `lead_score` (its own
 * 100/50/0 verdict) and `vertical` (which business line the call was for).
 *
 * Everything here is defensive: unknown shapes yield nulls, never throws.
 */

import { DEFAULT_VERTICAL, parseVertical } from '@/lib/verticals';

/** Values a Jinja template produces for a variable that did not resolve. */
const PLACEHOLDER_VALUES = new Set(['', 'none', 'null', 'undefined', 'nan', 'n/a', '{}', '[]', '-']);

/** "" / "None" / "Undefined" -> null; otherwise the trimmed string. */
export function cleanString(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'object') {
    try {
      const s = JSON.stringify(value);
      return s === '{}' || s === '[]' ? null : s;
    } catch {
      return null;
    }
  }
  const s = String(value).trim();
  return PLACEHOLDER_VALUES.has(s.toLowerCase()) ? null : s;
}

/** Accepts 6, "6" or "" and returns a number or null - never NaN. */
export function cleanNumber(value: unknown): number | null {
  const s = cleanString(value);
  if (s === null) return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

/**
 * Jinja renders Python's True/False capitalised, and an unset variable as "".
 * Anything unrecognised stays null so scoreCall() treats it as "no signal"
 * rather than a definite "not interested".
 */
export function cleanBoolean(value: unknown): boolean | null {
  if (typeof value === 'boolean') return value;
  const s = cleanString(value);
  if (s === null) return null;
  const v = s.toLowerCase();
  if (['true', 'yes', '1', 'interested', 'y', 'కావాలి', 'అవును'].includes(v)) return true;
  if (['false', 'no', '0', 'not_interested', 'n', 'వద్దు', 'లేదు'].includes(v)) return false;
  return null;
}

/** Nested places providers stash extracted variables. Later sources never win over earlier ones. */
const CONTEXT_KEYS = [
  'gathered_context',
  'extracted_variables',
  'extracted_context',
  'variables',
  'context',
  'collected_data',
  'gathered_data',
  'metadata',
  'initial_context',
];

/**
 * Flattens the payload and every nested context bag into one lookup table.
 * Top-level keys win; nested bags fill the gaps in the order listed above.
 */
export function flattenPayload(raw: Record<string, any>): Record<string, any> {
  const flat: Record<string, any> = {};

  const absorb = (obj: unknown) => {
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return;
    for (const [key, value] of Object.entries(obj as Record<string, any>)) {
      if (flat[key] === undefined) flat[key] = value;
    }
  };

  absorb(raw);
  for (const key of CONTEXT_KEYS) absorb(raw?.[key]);
  // One extra level: gathered_context.extracted_variables etc.
  for (const key of CONTEXT_KEYS) {
    const bag = raw?.[key];
    if (bag && typeof bag === 'object') for (const inner of CONTEXT_KEYS) absorb(bag[inner]);
  }

  return flat;
}

/** First non-null alias, in priority order. */
function pick(flat: Record<string, any>, keys: string[]): unknown {
  for (const key of keys) {
    if (flat[key] !== undefined && cleanString(flat[key]) !== null) return flat[key];
  }
  return null;
}

/** What the solar agent asks first: is the house theirs, or rented. */
export type HouseOwnership = 'own' | 'rent';

/**
 * Turns whatever the solar agent extracted into 'own' / 'rent' / null.
 *
 * The value arrives as free text from an LLM listening to spoken Telugu, so it
 * can be "own", "own house", "rented", "tenant", "సొంత ఇల్లు" or "అద్దె ఇల్లు".
 * Anything it cannot place stays null - "no opinion" - rather than being guessed
 * into one of the two, because the two carry opposite scores (50 vs 0).
 */
export function parseHouseOwnership(value: unknown): HouseOwnership | null {
  const s = cleanString(value)?.toLowerCase();
  if (!s) return null;
  // Rent is checked first: "not own house" and "own kaadu, rent" both contain "own".
  if (/rent|tenant|lease|అద్దె|కిరాయి|అద్దెకి/.test(s)) return 'rent';
  if (/\bown\b|owned|self|sonta|independent|సొంత|సొంతం|మా\s*ఇల్లు/.test(s)) return 'own';
  if (/apartment|flat/.test(s)) return null; // says nothing about who owns it
  return null;
}

export interface CallSignals {
  interested: boolean | null;
  budget: string | null;
  loan_type: string | null;
  /** Solar: does the customer own the house. Rent is an instant disqualification. */
  house_ownership: HouseOwnership | null;
  /** Solar: are they planning / thinking about putting solar on it. */
  solar_planning: boolean | null;
  /**
   * Investing: does the caller already put money away every month - SIP, mutual
   * fund, FD, savings, insurance, chit, gold. The investing agent (Dograh
   * workflow 6) asks exactly this one question, so it is the whole qualification.
   */
  currently_investing: boolean | null;
  /** Investing: what they said they invest in, in plain English ("SIP", "FD"). */
  investment_type: string | null;
  profession: string | null;
  monthly_income: string | null;
  existing_emi: string | null;
  visit_date: string | null;
  customer_intent: string | null;
  do_not_call: boolean | null;
  summary: string | null;
  /** Real estate: plot, independent house, flat, villa, commercial, as stated. */
  realestate_property_type: string | null;
  /** Real estate: buy, sell, rent, or unclear. */
  realestate_deal_type: string | null;
  /** Real estate: area or locality mentioned. */
  realestate_location: string | null;
  /** Real estate: how soon they said they want to proceed. */
  realestate_timeline: string | null;
  notes: string | null;
  customer_name: string | null;
  /** The agent's own qualified flag, used as a fallback interest signal. */
  qualified_flag: boolean | null;
  /** Score the agent decided during the call, 0-100. Overrides the additive rules. */
  lead_score: string | null;
  /**
   * Which business line the call was for: loan, solar, realestate, investing.
   * Each vertical gets its own Dograh workflow, and each names itself here, so a
   * single lead list can hold all four and a score is never ambiguous.
   */
  vertical: string | null;
}

/**
 * The live Dograh webhook packs four extracted variables into one `notes` string:
 *   "type: Home loan | occupation: Salaried | income: 50000 | existing EMI: 12000"
 * Recovering them here means the dashboard shows structured values even before
 * the Dograh payload template is updated to send them as separate fields.
 */
function parsePackedNotes(notes: string | null): Record<string, string> {
  if (!notes || !notes.includes(':')) return {};
  const found: Record<string, string> = {};
  const aliases: Record<string, string> = {
    type: 'loan_type',
    'loan type': 'loan_type',
    occupation: 'occupation',
    profession: 'occupation',
    income: 'monthly_income',
    'monthly income': 'monthly_income',
    'existing emi': 'existing_emi',
    emi: 'existing_emi',
    amount: 'loan_amount',
  };

  for (const part of notes.split('|')) {
    const idx = part.indexOf(':');
    if (idx === -1) continue;
    const label = part.slice(0, idx).trim().toLowerCase();
    const value = cleanString(part.slice(idx + 1));
    const key = aliases[label];
    if (key && value) found[key] = value;
  }
  return found;
}

/**
 * True for "type: | occupation: | income: | existing EMI:" - every segment is a
 * label with nothing after the colon, which is what the template renders when a
 * call was never answered and no variable resolved.
 */
function isEmptyLabelledList(text: string): boolean {
  const segments = text.split('|');
  if (segments.length < 2) return false;
  return segments.every((segment) => {
    const idx = segment.indexOf(':');
    return idx > 0 && segment.slice(idx + 1).trim() === '';
  });
}

export function extractCallSignals(raw: Record<string, any>): CallSignals {
  const flat = flattenPayload(raw ?? {});

  // Fill only the gaps: a real field always beats one recovered from the string.
  const rawNotes = cleanString(flat.notes);
  const packed = parsePackedNotes(rawNotes);
  for (const [key, value] of Object.entries(packed)) {
    if (flat[key] === undefined || cleanString(flat[key]) === null) flat[key] = value;
  }

  // The template always renders its notes line, so an unanswered call produces
  // "type:  | occupation:  | income:  | existing EMI:" - pure noise that was
  // being written into call_logs and appended to the lead's notes.
  if (rawNotes && Object.keys(packed).length === 0 && isEmptyLabelledList(rawNotes)) {
    flat.notes = null;
  }

  const interested = cleanBoolean(
    pick(flat, ['interested', 'loan_required', 'is_interested', 'lead_interested', 'solar_interest']),
  );
  const qualifiedFlag = cleanBoolean(pick(flat, ['qualified', 'is_qualified']));

  const vertical = parseVertical(pick(flat, ['vertical', 'business_line', 'product_line', 'agent_type']));

  // `property_type` is the solar agent's word for the HOUSE, and the loan
  // agent's column for the loan type. Reading it as a loan type on a solar call
  // is what put "Own house loan" in the dashboard's Loan type column, so on a
  // solar call it is deliberately not one of the loan aliases.
  const loanTypeAliases =
    vertical === 'solar'
      ? ['loan_type', 'loan_category', 'product']
      : ['loan_type', 'loan_category', 'product', 'property_type'];

  return {
    // Fall back to the agent's own "qualified" verdict when it never set an
    // explicit interest flag - otherwise a good call reads as "no signal".
    interested: interested ?? qualifiedFlag,
    budget: cleanString(pick(flat, ['budget', 'loan_amount', 'amount', 'requested_amount', 'loan_budget'])),
    loan_type: cleanString(pick(flat, loanTypeAliases)),
    house_ownership: parseHouseOwnership(
      pick(flat, ['house_ownership', 'own_house', 'house_type', 'home_ownership', 'ownership', 'property_type']),
    ),
    solar_planning: cleanBoolean(
      pick(flat, ['solar_planning', 'planning_solar', 'solar_plan', 'planning_for_solar', 'solar_interest']),
    ),
    currently_investing: cleanBoolean(
      pick(flat, ['currently_investing', 'is_investing', 'already_investing', 'investing', 'invests']),
    ),
    // Deliberately NOT one of the loan_type aliases: an investing call's "SIP"
    // is not a loan type, and reading it as one would put "SIP loan" on screen -
    // the same mistake `property_type` made on solar leads.
    investment_type: cleanString(
      pick(flat, ['investment_type', 'investing_type', 'investment_product', 'invests_in', 'investment']),
    ),
    profession: cleanString(pick(flat, ['profession', 'occupation', 'income_type', 'employment_type'])),
    monthly_income: cleanString(pick(flat, ['monthly_income', 'income', 'salary'])),
    existing_emi: cleanString(pick(flat, ['existing_emi', 'current_emi', 'emi'])),
    visit_date: cleanString(pick(flat, ['visit_date', 'preferred_visit_date', 'callback_date', 'follow_up_date'])),
    customer_intent: cleanString(pick(flat, ['customer_intent', 'intent', 'disposition'])),
    do_not_call: cleanBoolean(pick(flat, ['do_not_call', 'dnc', 'opt_out'])),
    summary: cleanString(pick(flat, ['summary', 'call_summary', 'crm_summary'])),
    // `property_type` is accepted ONLY on a real-estate call.
    //
    // The webhook sends this as `property_kind` precisely because the bare name
    // is already claimed by the loan agent's loan-type alias and by
    // parseHouseOwnership's solar check -- on those verticals "plot" under that
    // name would be filed as a loan type or misread as a house-ownership answer.
    //
    // But the reconcile sweep does not read the webhook payload; it reads the
    // run's own gathered_context, where the field is named `property_type`
    // because that is what the extraction schema calls it. So a real-estate call
    // recovered by the sweep left this null while the webhook path filled it,
    // and the lead card's Property column was empty for exactly those calls.
    // Seen on run 579 (2026-09-04): "plot" reached the note as "loan: plot".
    //
    // Gating on the vertical keeps the collision impossible: a loan or solar
    // call never reaches this alias.
    realestate_property_type: cleanString(
      pick(flat, vertical === 'realestate'
        ? ['property_kind', 'realestate_property_type', 'property_type']
        : ['property_kind', 'realestate_property_type']),
    ),
    realestate_deal_type: cleanString(pick(flat, ['deal_type', 'realestate_deal_type'])),
    realestate_location: cleanString(pick(flat, ['location', 'realestate_location', 'area'])),
    realestate_timeline: cleanString(pick(flat, ['timeline', 'realestate_timeline'])),
    notes: cleanString(pick(flat, ['notes', 'note', 'remarks', 'comments'])),
    customer_name: cleanString(pick(flat, ['customer_name', 'name', 'lead_name'])),
    qualified_flag: qualifiedFlag,
    lead_score: cleanString(pick(flat, ['lead_score', 'score', 'call_score'])),
    // Left NULL when the agent did not name a business line. It used to default
    // to 'loan' here, which meant buildGatheredContext's lead fallback could
    // never fire: an investing agent that does not send `vertical` (workflow 6
    // does not) had every one of its calls stored as "from Loan".
    vertical,
  };
}

/**
 * True when the call actually told us something about the lead.
 *
 * A call can be "answered" and still carry no information - the customer picks up
 * and hangs up after three seconds, so nothing is extracted. Without this check a
 * throwaway second call overwrote a lead that an earlier real conversation had
 * qualified (observed live: score 75 "qualified" replaced by 20 "not_qualified").
 */
export function hasQualificationSignal(signals: CallSignals): boolean {
  return Boolean(
    signals.interested !== null ||
      signals.budget ||
      signals.loan_type ||
      // Solar: "rented house" is the whole answer to a solar call. Without it a
      // rent call carried no signal, so its 0 was never written to the lead.
      signals.house_ownership ||
      signals.solar_planning !== null ||
      // Investing: "I already do a SIP" is the entire answer to an investing
      // call. Without it such a call carries no signal and its score is never
      // written to the lead.
      signals.currently_investing !== null ||
      signals.investment_type ||
      signals.profession ||
      signals.monthly_income ||
      signals.existing_emi ||
      signals.visit_date ||
      signals.do_not_call !== null ||
      // Real estate: any one of these is a real answer, same as budget already is.
      signals.realestate_property_type ||
      signals.realestate_deal_type ||
      signals.realestate_location ||
      signals.realestate_timeline,
  );
}

export interface QaVerdict {
  tags: { tag: string; reason?: string }[];
  sentiment: string | null;
  score: number | null;
  summary: string | null;
}

/**
 * Dograh's QA node reviews each call against a rubric and returns tags such as
 * DEAD_AIR, HEARING_ISSUES, READ_OPTION_LIST or GUESSED_MISHEARD. The verdict
 * arrives either as the webhook's `qa` field or as `annotations` on the run
 * record, nested one level per QA node id, so flatten it into one shape.
 */
export function extractQaVerdict(raw: unknown): QaVerdict | null {
  if (!raw) return null;

  let payload: any = raw;
  if (typeof payload === 'string') {
    try {
      payload = JSON.parse(payload);
    } catch {
      return null;
    }
  }
  if (typeof payload !== 'object') return null;

  // Results are keyed by node ("qa_6": {...}); merge every node's verdict.
  const blocks: any[] = [];
  if (Array.isArray(payload.tags) || payload.call_quality_score !== undefined) blocks.push(payload);
  for (const [key, value] of Object.entries(payload)) {
    if (key.startsWith('qa_') && value && typeof value === 'object') blocks.push(value);
    if (key === 'node_results' && value && typeof value === 'object') {
      for (const inner of Object.values(value as Record<string, any>)) blocks.push(inner);
    }
  }
  if (blocks.length === 0) return null;

  // Dograh repeats each tag at the top level as an aggregate AND inside its node
  // block, so collapse by name and keep whichever copy carries the reason.
  const byTag = new Map<string, { tag: string; reason?: string }>();
  let sentiment: string | null = null;
  let score: number | null = null;
  let summary: string | null = null;

  const addTag = (tag: string, reason?: string) => {
    const existing = byTag.get(tag);
    if (!existing) byTag.set(tag, reason ? { tag, reason } : { tag });
    else if (!existing.reason && reason) existing.reason = reason;
  };

  for (const block of blocks) {
    for (const entry of block?.tags ?? []) {
      if (typeof entry === 'string') addTag(entry);
      else if (entry?.tag) addTag(String(entry.tag), cleanString(entry.reason) ?? undefined);
    }
    sentiment = sentiment ?? cleanString(block?.overall_sentiment);
    const parsed = cleanNumber(block?.call_quality_score);
    if (score === null && parsed !== null) score = parsed;
    summary = summary ?? cleanString(block?.summary);
  }

  const tags = [...byTag.values()];
  if (tags.length === 0 && score === null && !summary) return null;
  return { tags, sentiment, score, summary };
}

/**
 * The jsonb blob we persist on call_logs.gathered_context and leads.qual_data.
 *
 * `scoring` is included so the dashboard can show WHO decided the score and why,
 * rather than presenting a bare number nobody can audit.
 */
export function buildGatheredContext(
  signals: CallSignals,
  outcome: string,
  scoring?: { score: number; scoredBy: string; reason: string },
  /**
   * The business line of the LEAD this call belongs to, used when the agent
   * did not name one itself - which is most agents: workflows 5 and 6 never
   * send `vertical`. Without it every call was stored as `loan`, so a solar or
   * investing call's history read "100 from Loan".
   */
  leadVertical?: unknown,
): Record<string, any> {
  const context: Record<string, any> = { call_outcome: outcome };
  if (scoring) {
    context.scoring = { score: scoring.score, scored_by: scoring.scoredBy, reason: scoring.reason };
  }
  if (signals.interested !== null) context.interested = signals.interested;
  if (signals.budget) context.loan_amount = signals.budget;
  if (signals.budget) context.budget_confirmed = signals.budget;
  if (signals.loan_type) context.loan_type = signals.loan_type;
  // Solar's two answers. Stored under their own names so the dashboard can show
  // "Own house / planning solar" instead of borrowing the loan agent's fields.
  if (signals.house_ownership) context.house_ownership = signals.house_ownership;
  if (signals.solar_planning !== null) context.solar_planning = signals.solar_planning;
  // Investing's two answers, under their own names for the same reason: the
  // dashboard shows "Already investing · SIP", not a loan type and a budget.
  if (signals.currently_investing !== null) context.currently_investing = signals.currently_investing;
  if (signals.investment_type) context.investment_type = signals.investment_type;
  if (signals.profession) context.profession = signals.profession;
  if (signals.monthly_income) context.monthly_income = signals.monthly_income;
  if (signals.existing_emi) context.existing_emi = signals.existing_emi;
  if (signals.visit_date) context.preferred_visit_date = signals.visit_date;
  if (signals.customer_intent) context.customer_intent = signals.customer_intent;
  // Real estate's answers, under their own names for the same reason solar and
  // investing get their own: the dashboard shows "Plot · Gachibowli", not a loan
  // type and a budget.
  if (signals.realestate_property_type) context.property_type = signals.realestate_property_type;
  if (signals.realestate_deal_type) context.deal_type = signals.realestate_deal_type;
  if (signals.realestate_location) context.location = signals.realestate_location;
  if (signals.realestate_timeline) context.timeline = signals.realestate_timeline;
  if (signals.do_not_call !== null) context.do_not_call = signals.do_not_call;
  if (signals.summary) context.summary = signals.summary;
  if (signals.notes) context.call_notes = signals.notes;
  if (signals.qualified_flag !== null) context.agent_qualified = signals.qualified_flag;
  // Always stored: the dashboard shows "100 from loan", so the business line has
  // to travel with the score even when every other field came back empty.
  //
  // Order matters. The agent's own claim wins (it knows which script it ran),
  // then the lead's column, and only then the default - so a solar call is
  // never filed under loan just because the agent stayed quiet about it.
  context.vertical = parseVertical(signals.vertical) ?? parseVertical(leadVertical) ?? DEFAULT_VERTICAL;
  if (signals.lead_score) context.agent_lead_score = signals.lead_score;
  return context;
}

/** Human-readable one-liner appended to leads.notes for each attempt. */
export function buildNoteLine(
  signals: CallSignals,
  parts: { prefix: string; attempt: number; outcome: string; score: number | null },
): string {
  return [
    parts.prefix,
    `attempt ${parts.attempt}`,
    `outcome: ${parts.outcome}`,
    parts.score !== null ? `score: ${parts.score}` : null,
    signals.loan_type ? `loan: ${signals.loan_type}` : null,
    signals.house_ownership ? `house: ${signals.house_ownership === 'own' ? 'own house' : 'rented'}` : null,
    signals.solar_planning !== null ? `solar plan: ${signals.solar_planning ? 'yes' : 'no'}` : null,
    signals.currently_investing !== null ? `investing: ${signals.currently_investing ? 'yes' : 'no'}` : null,
    signals.investment_type ? `invests in: ${signals.investment_type}` : null,
    signals.budget ? `amount: ${signals.budget}` : null,
    signals.profession ? `profession: ${signals.profession}` : null,
    signals.visit_date ? `follow-up: ${signals.visit_date}` : null,
    signals.realestate_property_type ? `property: ${signals.realestate_property_type}` : null,
    signals.realestate_location ? `location: ${signals.realestate_location}` : null,
    signals.realestate_deal_type ? `deal: ${signals.realestate_deal_type}` : null,
    signals.summary ?? signals.notes ?? null,
  ]
    .filter(Boolean)
    .join(' | ');
}

/**
 * Only absolute http(s) links are usable by the dashboard.
 *
 * Dograh stores raw storage keys like "transcripts/23.txt" / "recordings/23.wav"
 * on the run record, and only exposes a fetchable link once a public access token
 * exists. Storing a bare key produced an audio player pointed at nothing and a
 * transcript link that 404'd, so treat anything non-absolute as "not available".
 */
export function usableMediaUrl(value: unknown): string | null {
  const s = cleanString(value);
  if (!s) return null;
  return /^https?:\/\//i.test(s) ? s : null;
}

/**
 * A visit/callback date only becomes leads.follow_up_date if it resolves to a
 * real date.
 *
 * This used to be `new Date(value)` and nothing else, which meant it only ever
 * accepted a machine-readable date — and a person on a phone call never gives
 * one. "tomorrow", "next week", "Monday", "రేపు", "2 days" all produced Invalid
 * Date, so follow_up_date stayed null and the Follow-ups page stayed empty no
 * matter what the customer agreed to.
 *
 * Two further traps handled here:
 *  - "10/05/2026" is 10 May in India and 5 October to Date(). Day-first is
 *    parsed explicitly rather than left to the US default.
 *  - A resolved relative date lands at 10am local, not midnight. Midnight IST
 *    stored as UTC reads as the PREVIOUS day for anyone looking at UTC, and a
 *    callback is a working-hours thing anyway.
 */

/** IST. Every caller and every operator on this system is in one timezone. */
const LOCAL_OFFSET_MINUTES = 330;
const CALLBACK_HOUR_LOCAL = 10;

/** N days from today, at 10am local, as an ISO instant. */
function daysFromToday(days: number, now = new Date()): string {
  const local = new Date(now.getTime() + LOCAL_OFFSET_MINUTES * 60_000);
  const target = Date.UTC(
    local.getUTCFullYear(),
    local.getUTCMonth(),
    local.getUTCDate() + days,
    CALLBACK_HOUR_LOCAL,
    0,
    0,
  );
  return new Date(target - LOCAL_OFFSET_MINUTES * 60_000).toISOString();
}

/** Whole-word relative phrases, English and Telugu, in days from today. */
const RELATIVE_DAYS: [RegExp, number][] = [
  [/day\s*after\s*tomorrow|overmorrow|ఎల్లుండి|ఎల్లుండ/, 2],
  // Deliberately not matching "కల్" — it collides with "కాల్", the Telugu
  // rendering of the English word "call", which appears in almost every summary.
  [/tomorrow|tommorow|tmrw|రేపు|రేపటి/, 1],
  [/today|tonight|this\s*evening|ఈరోజు|ఈ\s*రోజు|సాయంత్రం/, 0],
  [/next\s*week|వచ్చే\s*వారం|వారం\s*తర్వాత/, 7],
  [/next\s*month|వచ్చే\s*నెల|నెల\s*తర్వాత/, 30],
  [/weekend|వీకెండ్/, 6],
];

/** Weekday names -> JS day index. The next occurrence is taken. */
const WEEKDAYS: [RegExp, number][] = [
  [/sunday|ఆదివారం/, 0],
  [/monday|సోమవారం/, 1],
  [/tuesday|మంగళవారం/, 2],
  [/wednesday|బుధవారం/, 3],
  [/thursday|గురువారం/, 4],
  [/friday|శుక్రవారం/, 5],
  [/saturday|శనివారం/, 6],
];

export function parseFollowUpDate(value: string | null, now = new Date()): string | null {
  if (!value) return null;
  const text = String(value).trim().toLowerCase();
  if (!text) return null;

  // ── Day-first numeric dates: 10/05/2026, 10-05-26, 10.5.2026 ───────────────
  const dmy = text.match(/^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})$/);
  if (dmy) {
    const day = Number(dmy[1]);
    const month = Number(dmy[2]);
    const year = Number(dmy[3].length === 2 ? `20${dmy[3]}` : dmy[3]);
    // Only treat it as day-first when it can BE day-first; 2026/05/10 stays for
    // the ISO path below.
    if (day >= 1 && day <= 31 && month >= 1 && month <= 12 && year >= 2020 && year <= 2100) {
      const at = Date.UTC(year, month - 1, day, CALLBACK_HOUR_LOCAL, 0, 0);
      const iso = new Date(at - LOCAL_OFFSET_MINUTES * 60_000);
      if (!Number.isNaN(iso.getTime())) return iso.toISOString();
    }
  }

  // ── Relative phrases ───────────────────────────────────────────────────────
  for (const [pattern, days] of RELATIVE_DAYS) {
    if (pattern.test(text)) return daysFromToday(days, now);
  }

  // "in 3 days", "3 days later", "after 2 weeks", "2 రోజుల తర్వాత"
  const inN = text.match(/(\d{1,3})\s*(day|days|రోజు|రోజుల|week|weeks|వారం|వారాల|month|months|నెల|నెలల)/);
  if (inN) {
    const n = Number(inN[1]);
    const unit = inN[2];
    const multiplier = /week|వార/.test(unit) ? 7 : /month|నెల/.test(unit) ? 30 : 1;
    const days = n * multiplier;
    // A callback more than a year out is a mis-extraction, not a plan.
    if (n > 0 && days <= 365) return daysFromToday(days, now);
  }

  // ── Weekday names: the NEXT one, never today ───────────────────────────────
  for (const [pattern, weekday] of WEEKDAYS) {
    if (pattern.test(text)) {
      const local = new Date(now.getTime() + LOCAL_OFFSET_MINUTES * 60_000);
      const delta = ((weekday - local.getUTCDay() + 7) % 7) || 7;
      return daysFromToday(delta, now);
    }
  }

  // ── Anything a Date can read on its own (ISO, "12 May 2026", …) ────────────
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  // Ignore anything absurd (a mis-extracted "2" becoming year 2001, etc).
  const year = parsed.getUTCFullYear();
  if (year < 2020 || year > 2100) return null;
  return parsed.toISOString();
}
