import { cookies } from 'next/headers';
import { isValidOperatorToken, OPERATOR_COOKIE } from '@/lib/operator-auth';
import { OperatorConsole } from '@/components/operator/operator-console';
import { OperatorUnlock } from '@/components/operator/operator-unlock';

export const dynamic = 'force-dynamic';

/**
 * The operator console — ours, not the client's.
 *
 * The gate is checked here on the server rather than in middleware, because
 * SKIP_AUTH short-circuits middleware entirely and would leave this page open
 * for as long as that flag is set. Every /api/operator route re-checks
 * independently, so a stale page cannot be used to act.
 */
export default async function OperatorPage() {
  const store = await cookies();
  const token = store.get(OPERATOR_COOKIE)?.value;
  const unlocked = isValidOperatorToken(token);

  if (!unlocked) return <OperatorUnlock />;
  return <OperatorConsole />;
}
