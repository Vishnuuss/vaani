/**
 * Meter every Dograh call into the billing database, then report what WOULD be
 * charged — without posting a single debit.
 *
 * Run:  npx tsx --env-file=.env.local scripts/meter-dryrun.ts
 *
 * Safe to re-run: metering is idempotent on dograh_run_id, and nothing here
 * writes to credit_ledger or moves a balance.
 */
import { createBillingClient } from '../lib/supabase-billing';
import { meterAllCampaigns } from '../lib/billing-meter';
import {
  getBillingConfig, isBillableCall, billableSecondsFor, creditsForSeconds,
  estimateProviderCost, toCredits, type MeteredCall,
} from '../lib/billing';

const rupees = (milli: number) => `Rs ${(milli / 1000).toFixed(2)}`;

async function main() {
  const billing = createBillingClient();
  const config = await getBillingConfig(billing);

  console.log('Rate:', config.rateMilliPerMinute / 1000, 'credits/min, billed in',
    config.billingIncrementSeconds, 'second blocks\n');

  console.log('Pulling runs from Dograh…');
  const meter = await meterAllCampaigns(billing);
  console.log(`  campaigns ${meter.campaignsScanned}  runs seen ${meter.runsSeen}  ` +
    `new ${meter.inserted}  already known ${meter.alreadyKnown}  errors ${meter.errors}\n`);

  const { data, error } = await billing
    .from('call_usage')
    .select('*')
    .order('called_at', { ascending: false });
  if (error) throw new Error(error.message);

  const calls = (data ?? []) as MeteredCall[];
  const byMode: Record<string, { n: number; secs: number }> = {};
  let billableCount = 0, billedSecs = 0, revenueMilli = 0, costMilli = 0;
  let costKnown = 0;
  const losers: { run: number; secs: number; rev: number; cost: number }[] = [];

  for (const call of calls) {
    const mode = call.call_mode ?? 'unknown';
    byMode[mode] ??= { n: 0, secs: 0 };
    byMode[mode].n += 1;
    byMode[mode].secs += Number(call.duration_seconds) || 0;

    if (!isBillableCall(call).billable) continue;
    billableCount += 1;
    const secs = billableSecondsFor(call, config);
    const rev = creditsForSeconds(secs, config);
    billedSecs += secs;
    revenueMilli += rev;

    if (call.usage_info) {
      const c = estimateProviderCost(call, config.rateCard);
      costMilli += c.totalMilli;
      costKnown += 1;
      if (c.totalMilli >= rev) {
        losers.push({ run: call.dograh_run_id, secs: Number(call.duration_seconds) || 0, rev, cost: c.totalMilli });
      }
    }
  }

  console.log('Calls metered by mode:');
  for (const [mode, v] of Object.entries(byMode).sort((a, b) => b[1].n - a[1].n)) {
    const billableMode = mode === 'vobiz' ? 'BILLABLE' : 'not billable (test)';
    console.log(`  ${mode.padEnd(14)} ${String(v.n).padStart(3)} calls  ${String(v.secs).padStart(5)}s   ${billableMode}`);
  }

  console.log('\nWhat would be charged:');
  console.log(`  billable calls     ${billableCount}`);
  console.log(`  billed minutes     ${billedSecs / 60}`);
  console.log(`  revenue            ${toCredits(revenueMilli)} credits  (${rupees(revenueMilli)})`);

  if (costKnown > 0) {
    console.log(`\nProvider cost (only ${costKnown} of ${billableCount} calls have usage data):`);
    console.log(`  our cost           ${rupees(costMilli)}`);
    const revForKnown = revenueMilli;
    console.log(`  margin             ${rupees(revForKnown - costMilli)}`);
    if (losers.length) {
      console.log(`\n  ${losers.length} call(s) cost more than they earn:`);
      for (const l of losers) {
        console.log(`    run ${l.run}: ${l.secs}s -> charged ${rupees(l.rev)}, cost ${rupees(l.cost)}`);
      }
    }
  } else {
    console.log('\nNo usage_info yet on any call — run the enrichment step to compute margin.');
  }

  console.log('\nNothing was billed. credit_ledger untouched.');
}

main().catch((err) => { console.error('FAILED:', err.message); process.exit(1); });
