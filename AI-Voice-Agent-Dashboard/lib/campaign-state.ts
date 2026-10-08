/**
 * Campaign lifecycle state machine.
 *
 * Previously the pause/resume routes wrote whatever status they wanted with no
 * validation, so it was possible to pause a completed campaign, resume a campaign
 * that was already running, or resume one that had never been started (which
 * silently restarted it from lead #1).
 */

export type CampaignStatus =
  | 'pending'
  | 'queued'
  | 'running'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'cancelled';

export const TERMINAL_STATUSES: readonly CampaignStatus[] = ['completed', 'failed', 'cancelled'];

export function isTerminal(status: string): boolean {
  return TERMINAL_STATUSES.includes(status as CampaignStatus);
}

/** Which statuses each action is allowed to act on. */
const ALLOWED_FROM: Record<'start' | 'pause' | 'resume' | 'cancel', CampaignStatus[]> = {
  start: ['pending', 'queued'],
  pause: ['running', 'queued'],
  resume: ['paused'],
  cancel: ['pending', 'queued', 'running', 'paused'],
};

/** Statuses where the action is already satisfied - treat as a no-op success, not an error. */
const ALREADY_SATISFIED: Record<'start' | 'pause' | 'resume' | 'cancel', CampaignStatus[]> = {
  start: ['running'],
  pause: ['paused'],
  resume: ['running'],
  cancel: ['cancelled'],
};

export type TransitionResult =
  | { ok: true; noop: true; status: CampaignStatus }
  | { ok: true; noop: false; nextStatus: CampaignStatus }
  | { ok: false; reason: string; code: 'terminal' | 'invalid' };

export function evaluateTransition(
  action: 'start' | 'pause' | 'resume' | 'cancel',
  current: string,
): TransitionResult {
  const status = current as CampaignStatus;

  if (ALREADY_SATISFIED[action].includes(status)) {
    return { ok: true, noop: true, status };
  }

  if (isTerminal(status)) {
    return {
      ok: false,
      code: 'terminal',
      reason: `Campaign is already ${status} and cannot be ${action}d.`,
    };
  }

  if (!ALLOWED_FROM[action].includes(status)) {
    return {
      ok: false,
      code: 'invalid',
      reason: `Cannot ${action} a campaign in "${status}" state. Allowed: ${ALLOWED_FROM[action].join(', ')}.`,
    };
  }

  const nextStatus: CampaignStatus =
    action === 'pause' ? 'paused' : action === 'cancel' ? 'cancelled' : 'running';

  return { ok: true, noop: false, nextStatus };
}

/** Maps Dograh's campaign state strings onto our internal status vocabulary. */
export function mapDograhStatus(raw: string | undefined | null): CampaignStatus | null {
  if (!raw) return null;
  switch (raw.toLowerCase()) {
    case 'not_started':
    case 'notstarted':
    case 'created':
      return 'pending';
    case 'queued':
    case 'scheduled':
      return 'queued';
    case 'running':
    case 'in_progress':
    case 'active':
      return 'running';
    case 'paused':
      return 'paused';
    case 'completed':
    case 'finished':
    case 'done':
      return 'completed';
    case 'failed':
    case 'error':
      return 'failed';
    case 'cancelled':
    case 'canceled':
    case 'stopped':
      return 'cancelled';
    default:
      return null;
  }
}
