/**
 * Verifies the solar ladder end to end: the raw webhook payload Dograh posts ->
 * extracted signals -> score and reason. Read-only. Places no calls, touches no
 * database, spends nothing.
 *
 *   node scripts/verify-solar-scoring.mjs
 *
 * The lib files are TypeScript with `@/lib/...` imports, which Node cannot
 * resolve on its own, so they are copied to .tmp with the alias rewritten and
 * run there under Node's type stripping. Nothing in lib/ is modified.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const stage = path.join(root, '.tmp', 'solar-verify');
fs.mkdirSync(stage, { recursive: true });
for (const f of ['call-scoring.ts', 'call-context.ts', 'verticals.ts', 'lead-update.ts']) {
  const src = fs
    .readFileSync(path.join(root, 'lib', f), 'utf8')
    .replace(/'@\/lib\/([a-z-]+)'/g, "'./$1.ts'");
  fs.writeFileSync(path.join(stage, f), src);
}

// pathToFileURL, not the bare path: a Windows "c:\..." path is read as a URL
// scheme by the ESM loader and fails with ERR_UNSUPPORTED_ESM_URL_SCHEME.
const { scoreCall } = await import(pathToFileURL(path.join(stage, 'call-scoring.ts')).href);
const { extractCallSignals } = await import(pathToFileURL(path.join(stage, 'call-context.ts')).href);

let failures = 0;
const check = (name, ok, detail = '') => {
  console.log(`${ok ? '  PASS' : '  FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`);
  if (!ok) failures += 1;
};

/** Exactly the shape Dograh's Jinja template posts: every value is a string. */
const payload = (gathered, outcome = 'user_hangup', duration = '45') => ({
  run_id: '9001',
  lead_id: 'lead-1',
  phone: '+919999999999',
  vertical: 'solar',
  outcome,
  duration,
  ...gathered,
});

const run = (gathered, outcome, duration) => {
  const raw = payload(gathered, outcome, duration);
  const s = extractCallSignals(raw);
  return scoreCall({
    vertical: 'solar',
    house_ownership: s.house_ownership,
    solar_planning: s.solar_planning,
    interested: s.interested,
    budget: s.budget,
    loan_type: s.loan_type,
    profession: s.profession,
    do_not_call: s.do_not_call,
    lead_score: s.lead_score,
    outcome: raw.outcome,
    duration: raw.duration,
  });
};

console.log('\nSolar ladder');
let r = run({ house_ownership: 'rent' });
check('rented house -> 0', r.score === 0, r.reason);

r = run({ house_ownership: 'own', solar_planning: 'True' });
check('own house + planning solar -> 100', r.score === 100 && r.qualification === 'qualified', r.reason);

r = run({ house_ownership: 'own', solar_planning: 'False' });
check('own house + not planning -> 50', r.score === 50, r.reason);

r = run({ house_ownership: 'own', solar_planning: '' });
check('own house, second question never answered -> 50', r.score === 50, r.reason);

r = run({});
check('talked 45s, answered nothing -> 25 (below 50)', r.score === 25, r.reason);

r = run({}, 'no_answer', '0');
check('never answered -> unscored and retryable', r.score === 0 && r.qualification === null, r.reason);

r = run({ house_ownership: 'own', solar_planning: 'True', do_not_call: 'True' });
check('do-not-call beats everything -> 0', r.score === 0, r.reason);

console.log('\nSpellings the extraction LLM may return');
for (const [value, expect] of [
  ['own house', 'own'], ['Own', 'own'], ['సొంత ఇల్లు', 'own'], ['independent house', 'own'],
  ['rent', 'rent'], ['rented house', 'rent'], ['అద్దె ఇల్లు', 'rent'], ['tenant', 'rent'],
  ['', null], ['None', null], ['apartment', null],
]) {
  const s = extractCallSignals(payload({ house_ownership: value }));
  check(`house_ownership "${value || '(empty)'}" -> ${expect}`, s.house_ownership === expect, String(s.house_ownership));
}

console.log('\nSolar detail must not leak into the loan fields');
const solar = extractCallSignals(payload({ property_type: 'own house', house_ownership: 'own' }));
check('property_type on a solar call is not read as a loan type', solar.loan_type === null, String(solar.loan_type));
check('property_type is read as the house instead', solar.house_ownership === 'own');

console.log('\nLoan calls are unchanged');
const loan = extractCallSignals({ vertical: 'loan', property_type: 'Home loan', loan_required: 'True' });
check('loan call still reads property_type as the loan type', loan.loan_type === 'Home loan');
check('loan: named a type -> 100', scoreCall({ vertical: 'loan', loan_type: 'Home loan', outcome: 'user_hangup', duration: 60 }).score === 100);
check('loan: talked, gave nothing -> 25', scoreCall({ vertical: 'loan', outcome: 'user_hangup', duration: 60 }).score === 25);

console.log('\nThe lead write survives 007_solar_fields.sql not having been run');
const { updateLead } = await import(pathToFileURL(path.join(stage, 'lead-update.ts')).href);

/** Fake Supabase that rejects the two new columns, exactly as PostgREST would. */
const fakeDb = (missing) => {
  const writes = [];
  return {
    writes,
    from: () => ({
      update(payload) {
        writes.push(payload);
        const bad = missing.find((c) => c in payload);
        return {
          eq: async () =>
            bad
              ? { error: { code: '42703', message: `column leads.${bad} does not exist` } }
              : { error: null },
        };
      },
    }),
  };
};

const noColumns = fakeDb(['house_ownership', 'solar_planning']);
const before = console.warn;
console.warn = () => {};
const r1 = await updateLead(noColumns, 'lead-1', { score: 100, status: 'called', house_ownership: 'own', solar_planning: true });
console.warn = before;
check('missing columns: the score and status are still written', r1.error === null && noColumns.writes[1].score === 100);
check('missing columns: only the two new fields are dropped', !('house_ownership' in noColumns.writes[1]) && !('solar_planning' in noColumns.writes[1]));
check('missing columns: reported so it is not silent', r1.droppedColumns.join(',') === 'house_ownership,solar_planning');

const withColumns = fakeDb([]);
const r2 = await updateLead(withColumns, 'lead-1', { score: 100, house_ownership: 'own', solar_planning: true });
check('columns present: written in one go, no retry', r2.error === null && withColumns.writes.length === 1 && r2.droppedColumns.length === 0);

console.log(failures === 0 ? '\nALL PASS\n' : `\n${failures} FAILURE(S)\n`);
process.exit(failures === 0 ? 0 : 1);
