import { VERTICAL_LABELS, type Vertical } from '@/lib/verticals';

/**
 * Which Dograh agent to dial each business line with.
 *
 * Loan falls back to the original single DOGRAH_WORKFLOW_ID so the live loan
 * campaign keeps working unchanged. The other three have NO fallback on purpose:
 * an unset id must refuse the launch, never quietly dial a real-estate list with
 * the loan script. That failure is invisible from the dashboard and audible only
 * to the customer.
 *
 * This lives here rather than in a route because BOTH the campaign launcher and
 * the manual single-call endpoint need it. Two copies would drift, and a drifted
 * copy calls a business line with the wrong agent.
 */
export const WORKFLOW_ENV_BY_VERTICAL: Record<Vertical, string | undefined> = {
  loan: process.env.DOGRAH_WORKFLOW_ID_LOAN ?? process.env.DOGRAH_WORKFLOW_ID,
  realestate: process.env.DOGRAH_WORKFLOW_ID_REALESTATE,
  solar: process.env.DOGRAH_WORKFLOW_ID_SOLAR,
  investing: process.env.DOGRAH_WORKFLOW_ID_INVESTING,
};

/** The configured agent id for a business line, or null when none is set. */
export function workflowIdFor(vertical: Vertical): number | null {
  const parsed = Number.parseInt(WORKFLOW_ENV_BY_VERTICAL[vertical] ?? '', 10);
  return Number.isFinite(parsed) ? parsed : null;
}

/** The refusal shown when a business line has no agent configured. */
export function noAgentMessage(vertical: Vertical): string {
  return (
    `No calling agent is configured for the ${VERTICAL_LABELS[vertical]} business line yet, ` +
    `so no call was placed. Set DOGRAH_WORKFLOW_ID_${vertical.toUpperCase()} once its agent is built.`
  );
}
