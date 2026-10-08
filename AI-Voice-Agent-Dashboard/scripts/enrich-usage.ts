/**
 * Backfill usage_info (LLM tokens, TTS characters) onto metered calls.
 *
 * Run:  npx tsx --env-file=.env.local scripts/enrich-usage.ts
 *
 * The campaign-runs LIST endpoint returns usage_info: null, so these numbers
 * only exist on a per-run fetch. Read-only against Dograh; writes only the
 * usage_info column. Never touches the ledger.
 */
import { createBillingClient } from '../lib/supabase-billing';
import { enrichUsageInfo } from '../lib/billing-meter';

async function main() {
  const billing = createBillingClient();
  const limit = Number(process.argv[2] ?? 60);
  console.log(`Fetching usage data for up to ${limit} calls…`);
  const out = await enrichUsageInfo(billing, { limit });
  console.log(`  attempted ${out.attempted}  enriched ${out.enriched}  failed ${out.failed}`);
}

main().catch((err) => { console.error('FAILED:', err.message); process.exit(1); });
