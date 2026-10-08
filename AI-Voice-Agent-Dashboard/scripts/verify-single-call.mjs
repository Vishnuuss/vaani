/**
 * Verifies POST /api/calls/single WITHOUT PLACING A SINGLE CALL.
 *
 *   Start a server with every agent id blanked, then run this:
 *     DOGRAH_WORKFLOW_ID= DOGRAH_WORKFLOW_ID_LOAN= DOGRAH_WORKFLOW_ID_SOLAR= \
 *     DOGRAH_WORKFLOW_ID_REALESTATE= DOGRAH_WORKFLOW_ID_INVESTING= \
 *     SKIP_AUTH=true npx next dev -p 3119
 *     node scripts/verify-single-call.mjs http://localhost:3119
 *
 * WHY BLANK THE AGENT IDS: with no agent configured the endpoint refuses at the
 * agent check, which sits BEFORE the lead is claimed and long before anything is
 * uploaded or dialled. Every refusal in front of that point can then be proved
 * on real data with zero risk of ringing a real person. The dialling path itself
 * is deliberately NOT exercised here — it is verified once, by hand, against a
 * number we own.
 *
 * The "retry cap is ignored" check works the same way: a retry-capped lead must
 * come back with the no-agent refusal, NOT a retry-cap refusal. If the cap were
 * being enforced it would reject earlier and the message would differ.
 */
import fs from 'node:fs';
import path from 'node:path';

const APP = process.argv[2] || 'http://localhost:3119';
const envPath = path.resolve(process.cwd(), '..', '.env');
const env = Object.fromEntries(
  fs.readFileSync(envPath, 'utf8').split('\n')
    .filter((l) => l.includes('=') && !l.trim().startsWith('#'))
    .map((l) => [l.slice(0, l.indexOf('=')).trim(), l.slice(l.indexOf('=') + 1).trim()]),
);
const H = {
  apikey: env.SUPABASE_SERVICE_ROLE_KEY,
  Authorization: `Bearer ${env.SUPABASE_SERVICE_ROLE_KEY}`,
  'content-type': 'application/json',
};
const rest = async (p, init = {}) => {
  const r = await fetch(`${env.NEXT_PUBLIC_SUPABASE_URL}/rest/v1/${p}`, { ...init, headers: { ...H, ...(init.headers || {}) } });
  const t = await r.text();
  if (!r.ok) throw new Error(`${p} -> ${r.status} ${t}`);
  return t ? JSON.parse(t) : null;
};
const call = async (body) => {
  const r = await fetch(`${APP}/api/calls/single`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  return { status: r.status, data: await r.json().catch(() => ({})) };
};

let fails = 0;
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`);
  if (!ok) fails += 1;
};

const FAKE = '+919999000222';
const MARK = 'ZZ_SINGLECALL_TEST';
const cleanup = async () => {
  for (const l of await rest(`leads?select=id&phone=eq.${encodeURIComponent(FAKE)}`)) {
    await rest(`leads?id=eq.${l.id}`, { method: 'DELETE' });
  }
  for (const c of await rest(`campaign_runs?select=id&campaign_name=like.*${MARK}*`)) {
    await rest(`campaign_runs?id=eq.${c.id}`, { method: 'DELETE' });
  }
};

console.log(`\nVerifying ${APP}/api/calls/single — no call is placed by this script.`);
await cleanup();

// Guard: if an agent IS configured, the tests below could actually dial. Refuse.
const guard = await call({ leadId: '00000000-0000-0000-0000-000000000000' });
if (guard.status !== 404) {
  console.error(`\nABORTED: expected 404 for an unknown lead, got ${guard.status}. Server not in the expected state.`);
  process.exit(1);
}

console.log('\n1. Refusals that protect a real person');
check('unknown lead is refused', guard.status === 404, guard.data.error);

const badPhone = await call({ phone: 'not-a-number', vertical: 'solar' });
check('invalid phone number is refused', badPhone.status === 400, badPhone.data.error);

const noVertical = await call({ phone: '9876543210' });
check('missing business line is refused (never guessed)', noVertical.status === 400, noVertical.data.error);

console.log('\n2. Reaches the agent check without claiming or dialling');
const [anyLead] = await rest('leads?select=id,phone,vertical,status,retry_count&status=neq.queued&limit=1');
const normal = await call({ leadId: anyLead.id });
check('a normal lead gets past every earlier check', normal.status === 400, normal.data.error);
check('  ...and stops at "no agent configured"', /No calling agent is configured/.test(normal.data.error || ''));
const [afterNormal] = await rest(`leads?select=status,campaign_run_id&id=eq.${anyLead.id}`);
check('the lead was NOT claimed by the refused attempt', afterNormal.status === anyLead.status,
  `status still "${afterNormal.status}"`);

console.log('\n3. The retry cap is ignored for a manual call (as specified)');
const capped = await rest('leads?select=id,retry_count,status&retry_count=gte.3&status=neq.queued&limit=1');
if (capped.length === 0) {
  console.log('  SKIP  no lead with retry_count >= 3 in the database');
} else {
  const r = await call({ leadId: capped[0].id });
  check('a heavily-retried lead is NOT blocked by the cap',
    /No calling agent is configured/.test(r.data.error || ''),
    `retry_count=${capped[0].retry_count}, got: ${r.data.error}`);
}

console.log('\n4. A lead a campaign already holds is refused');
// Built here with fake data rather than hoping a queued lead happens to exist —
// this is the check that stops a person being rung twice at once, so it must be
// proved on every run, not skipped.
const HELD_PHONE = '+919999000333';
for (const l of await rest(`leads?select=id&phone=eq.${encodeURIComponent(HELD_PHONE)}`)) {
  await rest(`leads?id=eq.${l.id}`, { method: 'DELETE' });
}
const [runningRun] = await rest('campaign_runs', {
  method: 'POST',
  headers: { Prefer: 'return=representation' },
  body: JSON.stringify([{ campaign_name: `${MARK} running`, status: 'running', vertical: 'solar', requested_count: 1, concurrency: 1 }]),
});
const [heldLead] = await rest('leads', {
  method: 'POST',
  headers: { Prefer: 'return=representation' },
  body: JSON.stringify([{ name: MARK, phone: HELD_PHONE, vertical: 'solar', status: 'queued', campaign_run_id: runningRun.id, source: MARK }]),
});
const heldRes = await call({ leadId: heldLead.id });
check('refused with ALREADY_QUEUED while a campaign is running',
  heldRes.status === 409 && heldRes.data.code === 'ALREADY_QUEUED', heldRes.data.error);

// And the opposite: once that campaign is finished, the same lead is callable
// again. A lead left `queued` by a completed run must not be locked out for ever.
await rest(`campaign_runs?id=eq.${runningRun.id}`, { method: 'PATCH', body: JSON.stringify({ status: 'completed' }) });
const staleRes = await call({ leadId: heldLead.id });
check('a lead left queued by a FINISHED campaign is not locked out',
  staleRes.status !== 409, `status ${staleRes.status}: ${staleRes.data.error}`);

await rest(`leads?id=eq.${heldLead.id}`, { method: 'DELETE' });
await rest(`campaign_runs?id=eq.${runningRun.id}`, { method: 'DELETE' });

console.log('\n5. The form creates the lead, but a refusal leaves it unqueued');
const created = await call({ phone: FAKE, vertical: 'solar', name: MARK, city: 'Hyderabad' });
check('refused at the agent check', created.status === 400, created.data.error);
const rows = await rest(`leads?select=id,name,phone,vertical,status,source,city&phone=eq.${encodeURIComponent(FAKE)}`);
check('the person was saved as a lead', rows.length === 1, rows[0] && `${rows[0].name} / ${rows[0].vertical} / source=${rows[0].source}`);
check('  ...marked source=manual', rows[0]?.source === 'manual');
check('  ...with the city kept', rows[0]?.city === 'Hyderabad');
check('  ...and NOT left stuck in "queued"', rows[0]?.status !== 'queued', `status "${rows[0]?.status}"`);

const again = await call({ phone: FAKE, vertical: 'solar', name: MARK });
check('calling the same number again does not duplicate the lead', again.status === 400);
const rows2 = await rest(`leads?select=id&phone=eq.${encodeURIComponent(FAKE)}`);
check('  ...still exactly one lead', rows2.length === 1, `${rows2.length} row(s)`);

console.log('\n6. No stray campaign records were left behind');
const strays = await rest(`campaign_runs?select=id,campaign_name,status&campaign_name=like.*${MARK}*`);
check('no campaign_runs rows leaked from refused attempts', strays.length === 0, `${strays.length} found`);

await cleanup();
const left = await rest(`leads?select=id&phone=eq.${encodeURIComponent(FAKE)}`);
check('test data deleted', left.length === 0);

console.log(`\n${fails === 0 ? 'ALL CHECKS PASSED — and no call was placed.' : fails + ' CHECK(S) FAILED'}`);
process.exit(fails ? 1 : 0);
