/**
 * The ledger's idempotency key, across the backend cutover.
 *
 * Two opposite failures are possible here and this file pins both:
 *
 *  1. Key the same way for both backends and a Vaani call whose run id an old
 *     voice call already used is waved through as "already charged". The call
 *     happens and the client is never billed for it. 183 such ids already exist.
 *
 *  2. Change the key format for EVERY row and a thousand calls that were charged
 *     in August stop matching their ledger entry, so the next sweep bills the
 *     client for all of them a second time.
 *
 * The shape below is the only one that avoids both: historical format preserved
 * for the old backend, namespaced format for anything new.
 */
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';

import { debitIdempotencyKey, isBillableCall, billableSecondsFor, creditsForSeconds } from '../lib/billing.ts';

describe('debitIdempotencyKey', () => {
  test('keeps the historical format for the old backend', () => {
    // Every entry posted before 2026-09-03 carries exactly this.
    assert.equal(debitIdempotencyKey(1234, 'voice'), 'call:run:1234');
  });

  test('keeps the historical format when no provider is known', () => {
    // Rows read back before migration 010 have no provider; they are all old.
    assert.equal(debitIdempotencyKey(1234), 'call:run:1234');
    assert.equal(debitIdempotencyKey(1234, undefined), 'call:run:1234');
  });

  test('namespaces the new backend so a colliding id is still charged', () => {
    assert.equal(debitIdempotencyKey(464, 'vaani'), 'call:vaani:run:464');
  });

  test('the same run id on two backends produces two different keys', () => {
    // This is the whole point: run 464 exists on both.
    assert.notEqual(debitIdempotencyKey(464, 'voice'), debitIdempotencyKey(464, 'vaani'));
  });

  test('is stable — the same inputs always give the same key', () => {
    assert.equal(debitIdempotencyKey(464, 'vaani'), debitIdempotencyKey(464, 'vaani'));
  });
});

describe('isBillableCall — a test call must never be charged for', () => {
  const base = {
    dograh_run_id: 1,
    duration_seconds: 30,
    call_mode: 'vobiz',
    outcome: 'completed',
    usage_info: null,
  };

  test('a connected phone call is billable', () => {
    assert.equal(isBillableCall(base).billable, true);
  });

  test('the text simulator is not', () => {
    assert.equal(isBillableCall({ ...base, call_mode: 'textchat' }).billable, false);
  });

  test('the browser tester is not', () => {
    assert.equal(isBillableCall({ ...base, call_mode: 'smallwebrtc' }).billable, false);
  });

  test('a call that never connected is not', () => {
    assert.equal(isBillableCall({ ...base, outcome: 'no_answer' }).billable, false);
  });

  test('zero talk time is not', () => {
    assert.equal(isBillableCall({ ...base, duration_seconds: 0 }).billable, false);
  });

  test('an unknown mode is not — a missed charge is recoverable, a wrong one is not', () => {
    assert.equal(isBillableCall({ ...base, call_mode: null }).billable, false);
  });
});

describe('billing maths — whole-minute pulse', () => {
  const config: any = {
    minimumBillableSeconds: 60,
    billingIncrementSeconds: 60,
    rateMilliPerMinute: 4000,
  };
  const call = (seconds: number) => ({
    dograh_run_id: 1,
    duration_seconds: seconds,
    call_mode: 'vobiz',
    outcome: 'completed',
    usage_info: null,
  });

  test('a 20s call is charged one minute', () => {
    assert.equal(billableSecondsFor(call(20), config), 60);
    assert.equal(creditsForSeconds(60, config), 4000);
  });

  test('a 38s call is charged one minute', () => {
    // Run 400, the Tata Solar campaign call.
    assert.equal(billableSecondsFor(call(38), config), 60);
  });

  test('a second past the minute costs a full second minute', () => {
    assert.equal(billableSecondsFor(call(61), config), 120);
    assert.equal(creditsForSeconds(120, config), 8000);
  });

  test('an unbillable call is worth zero seconds, not a minimum charge', () => {
    assert.equal(billableSecondsFor({ ...call(30), call_mode: 'textchat' }, config), 0);
  });
});
