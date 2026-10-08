/**
 * A call must not be billed until it has finished.
 *
 * Rows in call_usage are insert-if-absent, so the FIRST write is what the client
 * is charged on, permanently. Read a run mid-call and it arrives with no
 * duration and no disposition; postDebit then finds zero talk time, stamps
 * billed_at to stop reconsidering it, and a real conversation is settled at zero
 * for ever.
 *
 * This is not hypothetical. Run 571, 2026-09-04:
 *
 *   12:59:40  a loan call is placed and connects
 *   13:00:09  it ends, 29 seconds of talk time
 *   13:00:10  the billing sweep reads it -- still in flight a moment earlier --
 *             and writes duration_seconds 0, outcome 'unknown'
 *             -> billed_at stamped, ~4 credits never charged
 *
 * meterSingleRun already refused this case and its comment explains exactly this
 * damage. meterCampaign, the ten-minute cron path, never got the same guard.
 */
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';

import { isMeterable, isBillableCall, billableSecondsFor } from '../lib/billing.ts';

describe('isMeterable', () => {
  test('a finished call is metered', () => {
    assert.equal(isMeterable({ id: 571, is_completed: true, cost_info: { call_duration_seconds: 29 } }), true);
  });

  test('a call still in progress is NOT metered', () => {
    // The exact shape run 571 had at 13:00:10.
    assert.equal(isMeterable({ id: 571, is_completed: false, cost_info: null }), false);
  });

  test('a run with no completion flag at all is not metered', () => {
    assert.equal(isMeterable({ id: 571 }), false);
    assert.equal(isMeterable({ id: 571, is_completed: null }), false);
  });

  test('a FINISHED call with no talk time still is metered', () => {
    // A real no-answer. It must be recorded once and marked unbillable, or the
    // sweep reconsiders it on every tick for ever.
    assert.equal(isMeterable({ id: 600, is_completed: true, cost_info: { call_duration_seconds: 0 } }), true);
  });

  test('nothing blows up on a malformed run', () => {
    assert.equal(isMeterable(null), false);
    assert.equal(isMeterable(undefined), false);
    assert.equal(isMeterable({}), false);
  });
});

describe('what the old behaviour cost — run 571', () => {
  const config: any = { minimumBillableSeconds: 60, billingIncrementSeconds: 60, rateMilliPerMinute: 4000 };

  test('metered mid-call, a 29s conversation is worth nothing', () => {
    const asSweptTooEarly = {
      dograh_run_id: 571,
      duration_seconds: 0,
      call_mode: 'vobiz',
      outcome: 'unknown',
      usage_info: null,
    };
    assert.equal(isBillableCall(asSweptTooEarly).billable, false);
    assert.equal(billableSecondsFor(asSweptTooEarly, config), 0);
  });

  test('metered after it finished, the same call is worth one minute', () => {
    const asFinished = {
      dograh_run_id: 571,
      duration_seconds: 29,
      call_mode: 'vobiz',
      outcome: 'completed',
      usage_info: null,
    };
    assert.equal(isBillableCall(asFinished).billable, true);
    assert.equal(billableSecondsFor(asFinished, config), 60);
  });
});

/**
 * The same premature write also lands in the client's call_logs, via the webhook
 * at hang-up. There the row is not immutable, so the sweep repairs it -- but
 * ONLY the facts. Re-running the scoring on every tick is the runaway that
 * migration 006 had to clean up after.
 */
describe('backfilling an incomplete call log', () => {
  // Mirrors backfillIncompleteLog's decision table.
  function patchFor(existing: any, run: any) {
    const patch: Record<string, any> = {};
    const real = Number(run?.cost_info?.call_duration_seconds ?? run?.duration ?? 0);
    if (Number.isFinite(real) && real > 0 && !(Number(existing.duration) > 0)) patch.duration = Math.round(real);
    const usable = (v: any) => (typeof v === 'string' && /^https?:\/\//i.test(v) ? v : null);
    if (!usable(existing.recording_url) && usable(run.recording_public_url)) {
      patch.recording_url = run.recording_public_url;
    }
    if (!usable(existing.transcript_url) && usable(run.transcript_public_url)) {
      patch.transcript_url = run.transcript_public_url;
    }
    return patch;
  }

  const finishedRun = {
    cost_info: { call_duration_seconds: 29 },
    recording_public_url: 'https://vaani-api.bswealthfinance.com/api/v1/public/download/workflow/t/recording',
    transcript_public_url: 'https://vaani-api.bswealthfinance.com/api/v1/public/download/workflow/t/transcript',
  };

  test('a zero duration written by the webhook is corrected', () => {
    const patch = patchFor({ duration: 0, recording_url: null, transcript_url: null }, finishedRun);
    assert.equal(patch.duration, 29);
    assert.ok(patch.recording_url);
    assert.ok(patch.transcript_url);
  });

  test('a duration that is already right is never overwritten', () => {
    const patch = patchFor(
      { duration: 29, recording_url: finishedRun.recording_public_url, transcript_url: finishedRun.transcript_public_url },
      finishedRun,
    );
    assert.deepEqual(patch, {}, 'nothing to do means no write, so the sweep stays idle');
  });

  test('a genuine zero-second call is left at zero', () => {
    // No talk time on the run either: there is nothing to correct TO.
    const patch = patchFor({ duration: 0, recording_url: null, transcript_url: null },
                           { cost_info: { call_duration_seconds: 0 } });
    assert.equal(patch.duration, undefined);
  });

  test('only the missing media link is filled, not the one already stored', () => {
    const patch = patchFor(
      { duration: 29, recording_url: 'https://kept.example/rec.wav', transcript_url: null },
      finishedRun,
    );
    assert.equal(patch.recording_url, undefined, 'an existing link is authoritative');
    assert.equal(patch.transcript_url, finishedRun.transcript_public_url);
  });
});
