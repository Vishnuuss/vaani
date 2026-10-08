/**
 * After a test call: did EVERYTHING actually land in Supabase?
 *
 *   node scripts/verify-solar-call.mjs            # newest solar call
 *   node scripts/verify-solar-call.mjs loan       # newest call of another line
 *   node scripts/verify-solar-call.mjs all        # newest call, any line
 *
 * Read-only. It writes nothing and places no calls - it reads the newest
 * call_logs row and the lead it belongs to, and says in plain words which parts
 * of the pipeline worked: the webhook arrived, the answers were extracted, the
 * score was computed, the lead was updated, the recording is playable.
 *
 * Reads NEXT_PUBLIC_SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY from .env.local.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const env = {};
for (const file of ['.env.local', '.env', '../.env']) {
  const p = path.resolve(root, file);
  if (!fs.existsSync(p)) continue;
  for (const line of fs.readFileSync(p, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Za-z0-9_]+)\s*=\s*(.*)$/);
    if (m && env[m[1]] === undefined) env[m[1]] = m[2].trim().replace(/^["']|["']$/g, '');
  }
}

const url = env.NEXT_PUBLIC_SUPABASE_URL;
const key = env.SUPABASE_SERVICE_ROLE_KEY;
if (!url || !key) {
  console.error('missing NEXT_PUBLIC_SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY');
  process.exit(1);
}
const headers = { apikey: key, Authorization: `Bearer ${key}` };

const wanted = (process.argv[2] || 'solar').toLowerCase();

const get = async (q) => {
  const res = await fetch(`${url}/rest/v1/${q}`, { headers });
  if (!res.ok) throw new Error(`${q} -> ${res.status} ${(await res.text()).slice(0, 200)}`);
  return res.json();
};

const yes = (ok, good, bad) => `${ok ? 'YES ' : 'NO  '} ${ok ? good : bad}`;

// ── The newest call, and the lead it belongs to ─────────────────────────────
const logs = await get('call_logs?select=*&order=called_at.desc&limit=40');
if (logs.length === 0) {
  console.log('\nNo call_logs rows at all. Nothing has ever been recorded.\n');
  process.exit(1);
}

const matches =
  wanted === 'all'
    ? logs
    : logs.filter((l) => (l.gathered_context?.vertical ?? 'loan') === wanted);

if (matches.length === 0) {
  console.log(`\nNo ${wanted} call in the last ${logs.length} calls.`);
  console.log(`Newest call was ${logs[0].gathered_context?.vertical ?? 'loan'} at ${logs[0].called_at}.\n`);
  process.exit(1);
}

const log = matches[0];
const ctx = log.gathered_context ?? {};
const [lead] = await get(`leads?select=*&id=eq.${log.lead_id}`);

console.log(`\n=== newest ${wanted} call =====================================`);
console.log(`  called_at      ${log.called_at}`);
console.log(`  dograh run     ${log.dograh_run_id}`);
console.log(`  lead           ${lead?.name || '(no name)'}  ${lead?.phone || ''}`);
console.log(`  outcome        ${log.outcome}   duration ${log.duration}s   attempt ${log.attempt_no}`);

console.log('\n--- 1. Did the webhook arrive and get recorded? ---');
console.log('  ' + yes(true, 'call_logs row written', ''));
console.log('  ' + yes(Boolean(log.lead_id && lead), 'matched to a lead', 'NOT matched to any lead — check phone/lead_id in the payload'));
console.log('  ' + yes(Boolean(log.dograh_run_id), 'run id stored (duplicate deliveries cannot double-count)', 'no run id — duplicates could be counted twice'));

console.log('\n--- 2. Were the answers extracted? ---');
if (wanted === 'solar') {
  console.log('  ' + yes(ctx.house_ownership !== undefined, `house_ownership = ${ctx.house_ownership}`, 'house_ownership MISSING — the agent did not extract question 1'));
  console.log('  ' + yes(ctx.solar_planning !== undefined, `solar_planning = ${ctx.solar_planning}`, 'solar_planning missing — normal if the call ended on "rent"'));
} else {
  console.log('  ' + yes(ctx.loan_type !== undefined, `loan_type = ${ctx.loan_type}`, 'loan_type missing'));
  console.log('  ' + yes(ctx.interested !== undefined, `interested = ${ctx.interested}`, 'interested missing'));
}
console.log('  ' + yes(Boolean(ctx.summary || ctx.call_notes), `summary: ${String(ctx.summary || ctx.call_notes).slice(0, 80)}`, 'no summary'));
console.log('  ' + yes(ctx.vertical === wanted || wanted === 'all', `filed under ${ctx.vertical}`, `filed under ${ctx.vertical} — the payload's vertical is wrong`));

console.log('\n--- 3. Was it scored, and can the score be explained? ---');
console.log('  ' + yes(ctx.scoring?.score !== undefined, `score ${ctx.scoring?.score} decided by ${ctx.scoring?.scored_by}`, 'no scoring block — this call was scored by older code'));
console.log(`       reason: ${ctx.scoring?.reason ?? '(none)'}`);

console.log('\n--- 4. Was the LEAD row updated? ---');
if (!lead) {
  console.log('  NO   there is no lead to update');
} else {
  console.log('  ' + yes(lead.score !== null, `leads.score = ${lead.score}`, 'leads.score is still empty'));
  console.log('  ' + yes(Boolean(lead.qualification), `leads.qualification = ${lead.qualification}`, 'leads.qualification empty'));
  console.log('  ' + yes(Boolean(lead.status), `leads.status = ${lead.status}`, 'no status'));
  console.log('  ' + yes(Boolean(lead.last_attempt_at), `last_attempt_at = ${lead.last_attempt_at}`, 'last_attempt_at not set — the retry sweep cannot see this call'));
  console.log('  ' + yes(Boolean(lead.qual_data && Object.keys(lead.qual_data).length), 'qual_data filled', 'qual_data still empty'));
  if (wanted === 'solar') {
    const hasCols = 'house_ownership' in lead;
    console.log('  ' + yes(hasCols, `leads.house_ownership = ${lead.house_ownership}, leads.solar_planning = ${lead.solar_planning}`,
      'the house_ownership / solar_planning COLUMNS do not exist yet — run scripts/007_solar_fields.sql (values are still in qual_data)'));
    console.log('  ' + yes(!lead.property_type, 'property_type left alone (it is the loan column)', `property_type was written: ${lead.property_type} — a solar call should not touch it`));
  }
}

console.log('\n--- 5. Can the client play the call back? ---');
console.log('  ' + yes(/^https?:\/\//.test(log.recording_url || ''), 'recording is a playable link', `recording not usable: ${log.recording_url ?? 'none'}`));
console.log('  ' + yes(/^https?:\/\//.test(log.transcript_url || ''), 'transcript is a fetchable link', `transcript not usable: ${log.transcript_url ?? 'none'}`));
console.log('  ' + yes(Boolean(log.cost_info && Object.keys(log.cost_info).length), 'cost_info stored (billing can meter it)', 'no cost_info — billing falls back to the reconcile sweep'));
console.log('  ' + yes(Boolean(ctx.qa), `QA verdict stored: ${(ctx.qa?.tags || []).map((t) => t.tag).join(', ') || 'no tags'}`, 'no QA verdict on this call'));

console.log('\nAnything marked NO above is the thing to fix. Everything else landed.\n');
