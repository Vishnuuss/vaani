import { useState, useEffect, useCallback, useRef } from 'react';
import type { CampaignRun, CampaignStats, DograhCampaignProgress } from '@/types';
import type { LeadSegment } from '@/lib/lead-segments';
import { DEFAULT_POLL_MS, useAutoRefresh, usePolling } from './use-polling';
import type { VerticalFilter } from './use-leads';

export interface LeadSegmentCount {
  value: LeadSegment;
  label: string;
  description: string;
  count: number;
  /** True when this segment deliberately ignores the Max retries ceiling, so
   *  the launch dialog can warn before it dials someone the automatic rotation
   *  had already stopped calling. */
  bypassesRetryCap?: boolean;
  /** Of `count`, how many have ALREADY had their full quota of calls. Only set
   *  for cap-bypassing segments — it is the number that makes the warning a
   *  decision rather than a disclaimer. */
  overCapCount?: number;
}

/** Live counts behind the Start Campaign dialog's segment dropdown. */
export function useLeadSegmentCounts(enabled = true, vertical: VerticalFilter = 'all') {
  const [segments, setSegments] = useState<LeadSegmentCount[]>([]);
  // Callbacks scheduled for a FUTURE date. They are not launchable yet, but
  // without them the dialog shows a bare "Follow-ups due (0)" while the sidebar
  // badge says 1, which reads as a broken screen.
  const [followUpUpcoming, setFollowUpUpcoming] = useState(0);
  const [followUpNextDue, setFollowUpNextDue] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const abortControllerRef = useRef<AbortController | null>(null);

  const fetchSegments = useCallback(async () => {
    if (abortControllerRef.current) abortControllerRef.current.abort();
    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    setIsLoading(true);
    try {
      const response = await fetch(`/api/campaigns/segments?vertical=${vertical}`, { signal: abortController.signal });
      if (!response.ok) throw new Error(`Failed to fetch lead segments: ${response.statusText}`);
      const data = await response.json();
      setSegments(data.segments || []);
      setFollowUpUpcoming(data.follow_up_upcoming ?? 0);
      setFollowUpNextDue(data.follow_up_next_due ?? null);
    } catch (err: any) {
      if (err.name !== 'AbortError') console.error('[useLeadSegmentCounts]', err);
    } finally {
      setIsLoading(false);
    }
  }, [vertical]);

  useEffect(() => {
    if (!enabled) return;
    fetchSegments();
    return () => abortControllerRef.current?.abort();
  }, [enabled, fetchSegments]);

  return { segments, followUpUpcoming, followUpNextDue, isLoading, refresh: fetchSegments };
}

export function useCampaigns(vertical: VerticalFilter = 'all') {
  const [campaigns, setCampaigns] = useState<CampaignRun[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  const fetchCampaigns = useCallback(async () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }

    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    setIsLoading(true);
    setError(null);

    try {
      const response = await fetch(`/api/campaigns?vertical=${vertical}`, {
        signal: abortController.signal,
      });

      if (!response.ok) {
        throw new Error(`Failed to fetch campaigns: ${response.statusText}`);
      }

      const data = await response.json();
      setCampaigns(data.campaigns || data);
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        setError(err.message || 'An error occurred fetching campaigns');
      }
    } finally {
      setIsLoading(false);
    }
  }, [vertical]);

  useEffect(() => {
    fetchCampaigns();
    return () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
  }, [fetchCampaigns]);

  // The campaigns route reconciles live status against Dograh on each GET, so this
  // is also what moves a campaign from "running" to "completed" in the UI.
  useAutoRefresh(fetchCampaigns, DEFAULT_POLL_MS);

  return { campaigns, isLoading, error, refresh: fetchCampaigns };
}

export function useCampaignStats(vertical: VerticalFilter = 'all') {
  const [stats, setStats] = useState<CampaignStats | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  const fetchStats = useCallback(async () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }

    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    setIsLoading(true);
    setError(null);

    try {
      const response = await fetch(`/api/campaigns/stats?vertical=${vertical}`, {
        signal: abortController.signal,
      });

      if (!response.ok) {
        throw new Error(`Failed to fetch campaign stats: ${response.statusText}`);
      }

      const data = await response.json();
      setStats(data.stats || data);
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        setError(err.message || 'An error occurred fetching campaign stats');
      }
    } finally {
      setIsLoading(false);
    }
  }, [vertical]);

  useEffect(() => {
    fetchStats();
    return () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
  }, [fetchStats]);

  useAutoRefresh(fetchStats, DEFAULT_POLL_MS);

  return { stats, isLoading, error, refresh: fetchStats };
}

export function useCampaignProgress(campaignId: string | null, dograhCampaignId: number | null) {
  const [isPolling, setIsPolling] = useState(false);
  
  const shouldPoll = Boolean(campaignId && dograhCampaignId && isPolling);
  const url = shouldPoll ? `/api/campaigns/${campaignId}/progress` : null;
  
  const { data, isLoading, error } = usePolling<{ progress: DograhCampaignProgress }>(
    url,
    10000,
    shouldPoll
  );

  const progress = data?.progress || null;

  useEffect(() => {
    if (campaignId && dograhCampaignId) {
      setIsPolling(true);
    } else {
      setIsPolling(false);
    }
  }, [campaignId, dograhCampaignId]);

  useEffect(() => {
    if (progress) {
      if (progress.progress_percentage === 100 || progress.state === 'completed') {
        setIsPolling(false);
      }
    }
  }, [progress]);

  return { progress, isPolling, error };
}

export async function launchCampaign(params: any): Promise<CampaignRun> {
  const response = await fetch('/api/campaigns', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(params),
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(errorData.error || `Failed to launch campaign: ${response.statusText}`);
  }

  const data = await response.json();
  return data.campaign || data;
}

/**
 * The pause/resume routes return a useful message in `error` (wrong state, provider
 * rejected, not configured). Throwing `statusText` discarded all of it and the UI
 * showed a bare "Failed to pause campaign".
 */
async function campaignAction(id: string, action: 'pause' | 'resume'): Promise<void> {
  const response = await fetch(`/api/campaigns/${id}/${action}`, { method: 'POST' });
  const data = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw new Error(data.error || `Failed to ${action} campaign (${response.status})`);
  }
}

export function pauseCampaign(id: string): Promise<void> {
  return campaignAction(id, 'pause');
}

export function resumeCampaign(id: string): Promise<void> {
  return campaignAction(id, 'resume');
}
