/**
 * The backend-identity guard.
 *
 * Regression cover for the 2026-09-03 cutover bug: run ids are unique only
 * within one calling backend, and Vaani restarted numbering at 1 while
 * call_logs already held a thousand ids from voice.bswealthfinance.com. Without
 * a provider in the identity, a real Vaani call is treated as a redelivery of a
 * call from August and dropped with a 200 OK.
 *
 * Run with:  npm test
 */
import { test, describe, beforeEach } from 'node:test';
import assert from 'node:assert/strict';

import {
  callProvider,
  findExistingCallLog,
  hasProviderColumn,
  resetProviderCapabilityCache,
  withProvider,
  LEGACY_PROVIDER,
} from '../lib/call-provider.ts';

/**
 * The slice of the Supabase client these functions touch.
 *
 * `columnExists` decides whether a `select('provider')` succeeds, which is how
 * the code detects whether migration 009/010 has been applied.
 */
function fakeDb(opts: { columnExists: boolean; rows?: any[] }) {
  const calls: any = { selects: [], filters: [] };
  const builder: any = {
    select(cols: string) {
      calls.selects.push(cols);
      if (cols === 'provider' && !opts.columnExists) {
        // PostgREST's shape for "column does not exist".
        return { ...builder, error: { code: '42703' }, limit: () => ({ error: { code: '42703' } }) };
      }
      return builder;
    },
    eq(col: string, val: any) {
      calls.filters.push([col, val]);
      return builder;
    },
    limit() {
      return Promise.resolve({ data: opts.rows ?? [], error: null });
    },
  };
  return {
    client: { from: () => builder } as any,
    calls,
  };
}

/** Each suite starts from a known env; describe bodies run before any test. */
function freshEnv() {
  delete process.env.CALL_PROVIDER;
  delete process.env.DOGRAH_API_URL;
  resetProviderCapabilityCache();
}

describe('callProvider', () => {
  beforeEach(freshEnv);

  test('a Vaani base url is the vaani provider', () => {
    process.env.DOGRAH_API_URL = 'https://vaani-api.bswealthfinance.com';
    assert.equal(callProvider(), 'vaani');
  });

  test('the bare vaani hostname counts too', () => {
    process.env.DOGRAH_API_URL = 'https://vaani.bswealthfinance.com';
    assert.equal(callProvider(), 'vaani');
  });

  test('the old backend keeps the legacy identity', () => {
    process.env.DOGRAH_API_URL = 'https://voice.bswealthfinance.com';
    assert.equal(callProvider(), LEGACY_PROVIDER);
  });

  test('an unset url is treated as the old backend, never as a new one', () => {
    // Guessing "vaani" here would re-key every historical row and make a
    // thousand billed calls look unbilled.
    assert.equal(callProvider(), LEGACY_PROVIDER);
  });

  test('an explicit override wins over the url', () => {
    process.env.DOGRAH_API_URL = 'https://voice.bswealthfinance.com';
    process.env.CALL_PROVIDER = 'staging';
    assert.equal(callProvider(), 'staging');
  });
});

describe('hasProviderColumn', () => {
  beforeEach(freshEnv);

  test('true when the migration has been applied', async () => {
    const { client } = fakeDb({ columnExists: true });
    assert.equal(await hasProviderColumn(client, 'call_logs'), true);
  });

  test('false when it has not, so the caller can degrade instead of failing', async () => {
    const { client } = fakeDb({ columnExists: false });
    assert.equal(await hasProviderColumn(client, 'call_logs'), false);
  });

  test('probes once and caches, rather than on every call', async () => {
    const { client, calls } = fakeDb({ columnExists: true });
    await hasProviderColumn(client, 'call_logs');
    await hasProviderColumn(client, 'call_logs');
    await hasProviderColumn(client, 'call_logs');
    assert.equal(calls.selects.filter((c: string) => c === 'provider').length, 1);
  });
});

describe('findExistingCallLog', () => {
  beforeEach(() => {
    freshEnv();
    process.env.DOGRAH_API_URL = 'https://vaani-api.bswealthfinance.com';
  });

  test('scopes the lookup by provider once the column exists', async () => {
    // THE BUG: unscoped, voice run 464 makes Vaani run 464 look already-handled.
    const { client, calls } = fakeDb({ columnExists: true, rows: [] });
    await findExistingCallLog(client, 464);
    assert.deepEqual(calls.filters, [
      ['dograh_run_id', 464],
      ['provider', 'vaani'],
    ]);
  });

  test('falls back to the run id alone when the column is absent', async () => {
    const { client, calls } = fakeDb({ columnExists: false, rows: [] });
    await findExistingCallLog(client, 464);
    assert.deepEqual(calls.filters, [['dograh_run_id', 464]]);
  });

  test('reports a genuine duplicate as found', async () => {
    const { client } = fakeDb({ columnExists: true, rows: [{ id: 'x' }] });
    assert.equal((await findExistingCallLog(client, 398)).found, true);
  });

  test('reports an unseen run as not found', async () => {
    const { client } = fakeDb({ columnExists: true, rows: [] });
    assert.equal((await findExistingCallLog(client, 401)).found, false);
  });
});

describe('withProvider', () => {
  beforeEach(() => {
    freshEnv();
    process.env.DOGRAH_API_URL = 'https://vaani-api.bswealthfinance.com';
  });

  test('stamps the row when the column exists', async () => {
    const { client } = fakeDb({ columnExists: true });
    const row = await withProvider(client, { dograh_run_id: 401 });
    assert.deepEqual(row, { dograh_run_id: 401, provider: 'vaani' });
  });

  test('leaves the row untouched when it does not, so the insert still succeeds', async () => {
    // Sending an unknown column fails the whole insert. Losing the collision
    // guard is bad; losing the call log entirely is worse.
    const { client } = fakeDb({ columnExists: false });
    const row = await withProvider(client, { dograh_run_id: 401 });
    assert.deepEqual(row, { dograh_run_id: 401 });
  });
});
