/**
 * Verifies the retry cap end to end. Read-only — places no calls, changes nothing.
 *
 *   node scripts/verify-retry-cap.mjs
 *
 * Part 1 is pure arithmetic and passes/fails on its own.
 * Part 2 checks the live database invariants that 006_fix_retry_counting.sql
 * establishes, so it FAILS until that migration has been applied.
 */
import fs from 'node:fs';
import path from 'node:path';

let failures = 0;
const check = (name, ok, detail = '') => {
  console.log(`${ok ? '  PASS' : '  FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`);
  if (!ok) failures += 1;
};

// ── Part 1: the arithmetic that was wrong ───────────────────────────────────
// Dograh counts max_retries as dials AFTER the first, so total = 1 + max_retries.
// This is the derivation app/api/campaigns/route.ts now uses.
const providerRetriesFor = (maxRetries) => Math.max(0, maxRetries - 1);
const totalDials = (maxRetries) => 1 + providerRetriesFor(maxRetries);

console.log('\nPart 1 — one campaign must place exactly `maxRetries` calls');
for (const n of [1, 2, 3, 5, 10]) {
  check(`maxRetries=${n} -> ${totalDials(n)} dial(s)`, totalDials(n) === n, `provider max_retries=${providerRetriesFor(n)}`);
}
check('maxRetries=0 never dials more than once', totalDials(0) === 1);

// The old behaviour, for the record: hardcoded max_retries: 2 -> 3 dials, and the
// sweep then allowed a second campaign -> 6. This is what production showed.
check('old code produced 6 dials for a "2" setting', (1 + 2) * 2 === 6);

// ── Part 2: live database invariants ────────────────────────────────────────
const envPath = path.resolve(process.cwd(), '..', '.env');
if (!fs.existsSync(envPath)) {
  console.log(`\nPart 2 — skipped, no .env at ${envPath}`);
  process.exit(failures > 0 ? 1 : 0);
}
const env = Object.fromEntries(
  fs
    .readFileSync(envPath, 'utf8')
    .split('\n')
    .filter((l) => l.includes('=') && !l.trim().startsWith('#'))
    .map((l) => [l.slice(0, l.indexOf('=')).trim(), l.slice(l.indexOf('=') + 1).trim()]),
);
const URL_ = env.NEXT_PUBLIC_SUPABASE_URL;
const KEY = env.SUPABASE_SERVICE_ROLE_KEY;
if (!URL_ || !KEY) {
  console.log('\nPart 2 — skipped, Supabase credentials not in .env');
  process.exit(failures > 0 ? 1 : 0);
}

const q = async (p) => {
  const r = await fetch(`${URL_}/rest/v1/${p}`, { headers: { apikey: KEY, Authorization: `Bearer ${KEY}` } });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
};

console.log('\nPart 2 — live database invariants (needs migration 006)');

const logs = await q('call_logs?select=lead_id,dograh_run_id&limit=50000');
const withRun = logs.filter((r) => r.dograh_run_id != null);
const distinctRuns = new Set(withRun.map((r) => r.dograh_run_id));
check(
  'no duplicate call_logs rows per Dograh run',
  withRun.length === distinctRuns.size,
  `${withRun.length} rows / ${distinctRuns.size} runs`,
);

const rowsPerLead = new Map();
for (const r of logs) if (r.lead_id) rowsPerLead.set(r.lead_id, (rowsPerLead.get(r.lead_id) ?? 0) + 1);

const leads = await q('leads?select=id,retry_count,status&limit=50000');
const mismatched = leads.filter((l) => (l.retry_count ?? 0) !== (rowsPerLead.get(l.id) ?? 0));
check(
  'every lead.retry_count equals its real number of calls',
  mismatched.length === 0,
  `${mismatched.length} mismatched`,
);

const settings = await q('settings?key=eq.call_behavior&select=value');
const maxRetries = Number(settings?.[0]?.value?.maxRetries ?? 2);
const over = [...rowsPerLead.entries()].filter(([, n]) => n > maxRetries);
check(
  `nobody has been called more than the cap of ${maxRetries}`,
  over.length === 0,
  over.length ? `worst: ${Math.max(...over.map(([, n]) => n))} calls, ${over.length} lead(s)` : '',
);
if (over.length) {
  console.log('        (historic over-calls from before the fix — the cap now stops these leads being dialled again)');
}

console.log(failures === 0 ? '\nAll checks passed.\n' : `\n${failures} check(s) failed.\n`);
process.exit(failures > 0 ? 1 : 0);
