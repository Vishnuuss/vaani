/**
 * Per-call unit economics, computed from real metered data.
 *
 * Run:  npx tsx --env-file=.env.local scripts/margin-report.ts
 *
 * Answers the question the whole pricing model hangs on: does 4 credits/minute
 * stay profitable as calls get longer? Read-only.
 */
import { createBillingClient } from '../lib/supabase-billing';
import {
  getBillingConfig, isBillableCall, billableSecondsFor, creditsForSeconds,
  estimateProviderCost, type MeteredCall,
} from '../lib/billing';

const rs = (milli: number) => (milli / 1000).toFixed(2);

async function main() {
  const billing = createBillingClient();
  const config = await getBillingConfig(billing);

  const { data, error } = await billing.from('call_usage').select('*');
  if (error) throw new Error(error.message);

  const rows = ((data ?? []) as MeteredCall[])
    .filter((c) => isBillableCall(c).billable && c.usage_info)
    .map((c) => {
      const actual = Number(c.duration_seconds) || 0;
      const billedSecs = billableSecondsFor(c, config);
      const rev = creditsForSeconds(billedSecs, config);
      const cost = estimateProviderCost(c, config.rateCard);
      return { run: c.dograh_run_id, actual, rev, cost, margin: rev - cost.totalMilli };
    })
    .sort((a, b) => a.actual - b.actual);

  console.log('run   secs  charged    cost   margin    llm     tts     stt   telco   cost/actual-min');
  console.log('─'.repeat(92));
  for (const r of rows) {
    const perMin = r.actual > 0 ? (r.cost.totalMilli / (r.actual / 60)) : 0;
    console.log(
      String(r.run).padStart(4),
      String(r.actual).padStart(5),
      rs(r.rev).padStart(8),
      rs(r.cost.totalMilli).padStart(7),
      rs(r.margin).padStart(8),
      rs(r.cost.breakdown.llm).padStart(6),
      rs(r.cost.breakdown.tts).padStart(7),
      rs(r.cost.breakdown.stt).padStart(7),
      rs(r.cost.breakdown.telephony).padStart(7),
      ('Rs ' + rs(perMin)).padStart(16),
    );
  }

  // Does cost per real minute climb with call length? That is the danger.
  const short = rows.filter((r) => r.actual <= 20);
  const long = rows.filter((r) => r.actual > 20);
  const avgPerMin = (set: typeof rows) =>
    set.length === 0 ? 0
      : set.reduce((s, r) => s + r.cost.totalMilli / (r.actual / 60), 0) / set.length;

  console.log('\nCost per ACTUAL minute of talk time:');
  console.log(`  calls <= 20s (${short.length}):  Rs ${rs(avgPerMin(short))}/min`);
  console.log(`  calls  > 20s (${long.length}):  Rs ${rs(avgPerMin(long))}/min`);
  console.log(`  you charge:            Rs 4.00/min (rounded up to whole minutes)`);

  const longest = rows[rows.length - 1];
  if (longest) {
    const perMin = longest.cost.totalMilli / (longest.actual / 60);
    console.log(`\nLongest call: run ${longest.run}, ${longest.actual}s, cost Rs ${rs(perMin)}/min.`);
    console.log(`Break-even call length at that rate: charging Rs 4/min, you stay profitable`);
    console.log(`while cost/min < Rs 4.00 — currently ${perMin < 4000 ? 'YES' : 'NO'}.`);
  }

  const unpriced = new Set(rows.flatMap((r) => r.cost.unpricedModels));
  if (unpriced.size) console.log('\nNOT in the rate card (counted as Rs 0):', [...unpriced].join(', '));
}

main().catch((err) => { console.error('FAILED:', err.message); process.exit(1); });
