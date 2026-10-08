/**
 * When the client's real billing starts.
 *
 * Everything before this instant is our own build-and-test traffic: agents being
 * tuned, dummy top-ups (one is literally referenced `UPI-TEST-12345`), calls to
 * our own numbers. Showing it to the client is worse than untidy — it inflates
 * their usage history and invites a conversation about charges that were never
 * theirs.
 *
 * The boundary is not a guess. The ledger records an operator zeroing the test
 * balance and opening the real one, 22 seconds apart:
 *
 *   seq 51  05 Aug 2026 21:13:07 IST  adjustment_debit   -47 cr  -> balance 0
 *   seq 52  05 Aug 2026 21:13:29 IST  adjustment_credit +500 cr  -> balance 500
 *   seq 53  05 Aug 2026 21:20:07 IST  call_debit  (first real call)
 *
 * So the period opens between seq 51 and 52. The default below sits in that gap,
 * which makes the opening +500 the first line of the client's statement and the
 * balance reconcile exactly from there.
 *
 * The ledger itself is NEVER filtered or deleted — it is a hash-chained,
 * tamper-evident record and breaking the chain would destroy the integrity check
 * (`/api/operator/integrity`). This is a display boundary only: the operator
 * console still sees everything.
 */

/** 05 Aug 2026, 21:13:20 IST — between the reset and the opening credit. */
const DEFAULT_PERIOD_START = '2026-08-05T15:43:20.000Z';

/**
 * ISO instant the client's billing period opens. Overridable with
 * BILLING_PERIOD_START so a new period can be opened without a code change.
 * An unparseable value falls back to the default rather than showing everything.
 */
export function billingPeriodStart(): string {
  const raw = process.env.BILLING_PERIOD_START?.trim();
  if (raw) {
    const parsed = new Date(raw);
    if (!Number.isNaN(parsed.getTime())) return parsed.toISOString();
    console.warn('[billing-period] BILLING_PERIOD_START is not a valid date, using the default:', raw);
  }
  return DEFAULT_PERIOD_START;
}

/**
 * The later of the period start and a rolling "last N days" window.
 *
 * A 90-day view must not reach back past the period start and drag the test
 * traffic back in, so the two floors are combined rather than chosen between.
 */
export function usageWindowStart(days: number): string {
  const rolling = new Date(Date.now() - days * 86_400_000).toISOString();
  const period = billingPeriodStart();
  return rolling > period ? rolling : period;
}
