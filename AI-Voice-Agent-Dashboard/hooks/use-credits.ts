'use client';

import { useCallback, useState } from 'react';
import { useJson } from './use-reports';
import { DEFAULT_POLL_MS } from './use-polling';

/** Balance, burn and runway. Shapes mirror /api/credits. */
export interface CreditsPayload {
  balance_credits: number;
  balance_milli: number;
  state: 'healthy' | 'low' | 'critical' | 'empty';
  currency: string;
  thresholds: { low_credits: number; critical_credits: number };
  rate: { credits_per_minute: number; billing_increment_seconds: number };
  burn: {
    last_7d_credits: number;
    avg_daily_credits: number;
    calls_7d: number;
    avg_credits_per_call: number;
  };
  runway: {
    days: number | null;
    minutes: number;
    calls_est: number | null;
    basis: string;
  };
  lifetime: { purchased_credits: number; spent_credits: number };
  auto_pause: { enabled: boolean; at_credits: number };
  updated_at: string;
}

/**
 * The balance behind the top-bar pill.
 *
 * Polled at 15s rather than the 60s the report panels use — this is the number
 * the client watches while a campaign runs, and a stale balance is the one
 * thing that would make the whole feature feel untrustworthy. useAutoRefresh
 * already skips ticks while the tab is in the background.
 */
export function useCredits() {
  return useJson<CreditsPayload | null>('/api/credits', (p) => p, null, DEFAULT_POLL_MS);
}

export interface LedgerEntry {
  id: string;
  created_at: string;
  entry_type: string;
  label: string;
  amount_credits: number;
  balance_after_credits: number | null;
  description: string | null;
  billed_seconds: number | null;
  actual_seconds: number | null;
  dograh_run_id: number | null;
  campaign_id: number | null;
  external_ref: string | null;
  created_by: string | null;
}

export interface LedgerPayload {
  entries: LedgerEntry[];
  pagination: { page: number; limit: number; total: number; totalPages: number };
}

export function useCreditLedger(page = 1, limit = 25) {
  return useJson<LedgerPayload>(
    `/api/credits/ledger?page=${page}&limit=${limit}`,
    (p) => p,
    { entries: [], pagination: { page: 1, limit, total: 0, totalPages: 1 } },
    60_000,
  );
}

export interface UsagePayload {
  range_days: number;
  timezone: string;
  /** When the client's billing period opened — everything before is our testing. */
  period_start: string;
  /** Whole-period totals, independent of the 7/30/90 day chart selector. */
  period: { credits_added: number; credits_spent: number; calls: number };
  daily: { date: string; credits: number; calls: number; billed_seconds: number }[];
  by_campaign: {
    campaign_id: number | null;
    calls: number;
    billed_seconds: number;
    credits: number;
    credits_per_call: number;
  }[];
  totals: {
    credits: number;
    calls: number;
    billed_minutes: number;
    avg_credits_per_call: number;
  };
}

export function useCreditUsage(days = 30) {
  return useJson<UsagePayload | null>(`/api/credits/usage?days=${days}`, (p) => p, null, 60_000);
}

export interface TopupPayload {
  payment: {
    upi_id?: string;
    payee_name?: string;
    qr_image_url?: string;
    bank?: { account_name?: string; account_number?: string; ifsc?: string; bank_name?: string };
    note?: string;
  };
  credits_per_minute: number;
  currency: string;
  presets: number[];
  /** GST charged on top of the credits, as a percentage. Server-owned. */
  gst_rate_percent: number;
  requests: {
    id: string;
    credits_requested: number;
    base_amount_inr: number;
    gst_rate_percent: number;
    gst_amount_inr: number;
    /** Gross — what was actually payable, GST included. */
    amount_inr: number;
    status: string;
    reference_note: string | null;
    requested_at: string;
    decided_at: string | null;
    decision_note: string | null;
  }[];
}

export function useTopupInfo() {
  return useJson<TopupPayload | null>('/api/credits/topup', (p) => p, null, 30_000);
}

/**
 * File a top-up request.
 *
 * Note what this does NOT do: it cannot add credits. It creates a pending row
 * that an operator has to approve. That is the entire reason the client can be
 * given this button safely.
 */
export function useRequestTopup() {
  const [isSubmitting, setIsSubmitting] = useState(false);

  const requestTopup = useCallback(async (credits: number, referenceNote?: string) => {
    setIsSubmitting(true);
    try {
      const response = await fetch('/api/credits/topup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ credits, reference_note: referenceNote }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload?.error || 'Could not submit the request.');
      return payload;
    } finally {
      setIsSubmitting(false);
    }
  }, []);

  return { requestTopup, isSubmitting };
}
