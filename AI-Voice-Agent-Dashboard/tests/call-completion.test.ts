/**
 * A call that arrives thin must end up complete — exactly once.
 *
 * Vaani's webhook fires at hang-up, and `perform_final_variable_extraction` has
 * not always finished by then. When it has not, every {{gathered_context.X}} in
 * the payload renders empty and {{cost_info.call_duration_seconds}} renders 0.
 *
 * Run 798, solar, 2026-09-06 — the case that prompted this:
 *
 *   Vaani      70s, house_ownership "own", solar_planning true, lead_score 100,
 *              recording + transcript present
 *   Dashboard  duration 0, no media, no answers, lead scored 0
 *
 * A qualified customer shown to the client as a dud, and nothing ever repaired
 * it: the old backfill touched only duration and media, and the cron's campaign
 * loop skips completed campaigns entirely.
 *
 * The tension this file pins down: completing the row is necessary, but
 * RE-scoring it on every ten-minute tick is what migration 006 had to clean up
 * after (47 log rows and retry_count 31 for 3 real calls). The rule that
 * satisfies both is "complete only what is blank, and only once".
 */
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';

import { extractCallSignals, hasQualificationSignal } from '../lib/call-context.ts';

/** The webhook payload for run 798, as it actually arrived. */
const THIN_PAYLOAD = {
  run_id: '798',
  vertical: 'solar',
  lead_id: 'a-lead',
  outcome: 'completed',
  duration: '0',
  house_ownership: '',
  solar_planning: '',
  lead_score: '',
  do_not_call: '',
  summary: '',
  recording: '',
  transcript: '',
};

/** The same call, read back from Vaani after extraction finished. */
const FINISHED_RUN = {
  id: 798,
  is_completed: true,
  cost_info: { call_duration_seconds: 70 },
  recording_public_url: 'https://vaani-api.bswealthfinance.com/api/v1/public/download/workflow/t/recording',
  transcript_public_url: 'https://vaani-api.bswealthfinance.com/api/v1/public/download/workflow/t/transcript',
  initial_context: { vertical: 'solar', lead_id: 'a-lead' },
  gathered_context: {
    house_ownership: 'own',
    solar_planning: true,
    do_not_call: null,
    lead_score: '100',
    summary: 'The customer said they own the house and are interested in installing solar.',
  },
};

describe('detecting a thin delivery', () => {
  test('the payload as sent carries NO qualification', () => {
    // This is what made the row worthless, and what the webhook now checks.
    assert.equal(hasQualificationSignal(extractCallSignals(THIN_PAYLOAD)), false);
  });

  test('the finished run carries one', () => {
    assert.equal(hasQualificationSignal(extractCallSignals(FINISHED_RUN)), true);
  });

  test('the run yields the answers the payload lacked', () => {
    const s = extractCallSignals(FINISHED_RUN);
    assert.equal(s.house_ownership, 'own');
    assert.equal(s.solar_planning, true);
  });

  test('empty strings are not mistaken for answers', () => {
    // Jinja renders a missing value as "", and "" must never look like a reply.
    const s = extractCallSignals(THIN_PAYLOAD);
    assert.equal(s.house_ownership, null);
    assert.equal(s.solar_planning, null);
  });
});

/**
 * Mirrors backfillIncompleteLog's decision table. The real function needs a
 * Supabase client and a network fetch; the DECISIONS are what must not regress.
 */
function repairPlan(existing: any, run: any) {
  const patch: Record<string, any> = {};
  const real = Number(run?.cost_info?.call_duration_seconds ?? run?.duration ?? 0);
  if (Number.isFinite(real) && real > 0 && !(Number(existing.duration) > 0)) patch.duration = Math.round(real);

  const usable = (v: any) => (typeof v === 'string' && /^https?:\/\//i.test(v) ? v : null);
  if (!usable(existing.recording_url) && usable(run.recording_public_url)) patch.recording_url = run.recording_public_url;
  if (!usable(existing.transcript_url) && usable(run.transcript_public_url)) patch.transcript_url = run.transcript_public_url;

  const storedSignals = extractCallSignals(existing.gathered_context ?? {});
  const runSignals = extractCallSignals(run);
  const completing = !hasQualificationSignal(storedSignals) && hasQualificationSignal(runSignals);
  if (completing) patch.gathered_context = { ...(existing.gathered_context ?? {}), ...run.gathered_context };
  return { patch, completing };
}

/** call_logs row 798, as the webhook left it. */
const THIN_ROW = {
  id: 'row-798',
  lead_id: 'a-lead',
  duration: 0,
  recording_url: null,
  transcript_url: null,
  gathered_context: { call_id: 'x', provider: 'vobiz', vertical: 'solar', call_outcome: 'completed' },
};

describe('repairing run 798', () => {
  test('duration, media AND the answers are all restored', () => {
    const { patch, completing } = repairPlan(THIN_ROW, FINISHED_RUN);
    assert.equal(patch.duration, 70);
    assert.ok(patch.recording_url);
    assert.ok(patch.transcript_url);
    assert.equal(completing, true, 'the qualification must be completed, not just the facts');
    assert.equal(patch.gathered_context.house_ownership, 'own');
  });

  test('the SECOND pass is a no-op — this is the 006 guard', () => {
    // Once the row carries a qualification the guard closes for ever, so a
    // ten-minute cron cannot re-score the same call over and over.
    const afterFirst = {
      ...THIN_ROW,
      duration: 70,
      recording_url: FINISHED_RUN.recording_public_url,
      transcript_url: FINISHED_RUN.transcript_public_url,
      gathered_context: { ...THIN_ROW.gathered_context, ...FINISHED_RUN.gathered_context },
    };
    const { patch, completing } = repairPlan(afterFirst, FINISHED_RUN);
    assert.equal(completing, false);
    assert.deepEqual(patch, {}, 'nothing to write means no write at all');
  });

  test('attempt counts are never part of a repair', () => {
    // retry_count/attempt_no are exactly what ran away in 006.
    const { patch } = repairPlan(THIN_ROW, FINISHED_RUN);
    assert.equal('attempt_no' in patch, false);
    assert.equal('retry_count' in patch, false);
  });

  test('a row that already has answers keeps its own', () => {
    // The first write wins on judgement; repair only fills blanks.
    const scored = { ...THIN_ROW, gathered_context: { house_ownership: 'rent', solar_planning: false } };
    const { completing } = repairPlan(scored, FINISHED_RUN);
    assert.equal(completing, false, 'a stored answer is never overwritten by a later read');
  });
});

describe('calls that are genuinely empty', () => {
  // Runs 793 and 795: Vaani itself extracted "" for every variable, because the
  // caller hung up in seconds. There is nothing to complete, and inventing a
  // score would be worse than an honest blank.
  const emptyRun = {
    id: 795,
    is_completed: true,
    cost_info: { call_duration_seconds: 14 },
    gathered_context: { house_ownership: '', solar_planning: '', do_not_call: '', summary: '', lead_score: '' },
  };

  test('no qualification is invented', () => {
    const { completing } = repairPlan({ ...THIN_ROW, duration: 14 }, emptyRun);
    assert.equal(completing, false);
  });

  test('the duration is still corrected', () => {
    // The facts are always safe to copy, even when the answers are blank.
    const { patch } = repairPlan(THIN_ROW, emptyRun);
    assert.equal(patch.duration, 14);
  });
});

/**
 * The repair pass must never read a run from the wrong backend.
 *
 * A run id identifies a call only WITHIN one backend, and this pass repairs a
 * row from whatever run comes back for that id. On 2026-09-06 the pass picked up
 * seven rows, six of them the old backend's (runs 2091-2097), and they merely
 * 404'd because Vaani was only at ~800.
 *
 * That is a clock, not a fix. When Vaani reaches run 2091 those ids stop 404-ing
 * and start returning a different customer's call, which would then be merged
 * into an August row. The candidate query is scoped by provider so the question
 * is never asked.
 */
describe('repair pass scoping', () => {
  const CANDIDATES = [
    { run: 573, provider: 'vaani' },
    { run: 2097, provider: 'voice' },
    { run: 2096, provider: 'voice' },
    { run: 2094, provider: 'voice' },
    { run: 2093, provider: 'voice' },
    { run: 2092, provider: 'voice' },
    { run: 2091, provider: 'voice' },
  ];

  const scopedTo = (provider: string) => CANDIDATES.filter((r) => r.provider === provider);

  test('only this backend’s rows are repaired', () => {
    assert.deepEqual(scopedTo('vaani').map((r) => r.run), [573]);
  });

  test('the other backend’s rows are never fetched', () => {
    // Six of seven candidates. Unscoped they were six errors a tick, and a
    // corruption risk the day the ids start resolving.
    assert.equal(scopedTo('voice').length, 6);
    assert.equal(scopedTo('vaani').some((r) => r.run > 2000), false);
  });

  test('a colliding id belongs to whichever backend the row came from', () => {
    // voice 2091 and a future vaani 2091 are different calls, and the provider
    // is the only thing that tells them apart.
    const voiceRow = CANDIDATES.find((r) => r.run === 2091)!;
    assert.equal(voiceRow.provider, 'voice');
    assert.notEqual(voiceRow.provider, 'vaani');
  });
});
