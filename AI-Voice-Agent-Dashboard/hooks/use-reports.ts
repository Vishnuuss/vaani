import { useState, useEffect, useCallback, useRef } from 'react';
import { DEFAULT_POLL_MS, useAutoRefresh } from './use-polling';
import type { VerticalFilter } from './use-leads';

export interface DailyPoint {
  date: string;
  total: number;
  qualified: number;
}

/** Chart-ready shape: short weekday label plus the two series the chart draws. */
export interface OverviewPoint {
  day: string;
  calls: number;
  qualified: number;
}

/**
 * Daily call/qualification counts for the overview chart.
 *
 * Replaces the hard-coded Mon-Sun sample series the dashboard shipped with,
 * which showed invented traffic on a deployment that had never placed a call.
 */
export function useOverview(range = 7, vertical: VerticalFilter = 'all') {
  const [data, setData] = useState<OverviewPoint[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  const fetchOverview = useCallback(async () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }

    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    setIsLoading(true);
    setError(null);

    try {
      const response = await fetch(`/api/reports/overview?range=${range}&vertical=${vertical}`, {
        signal: abortController.signal,
      });

      if (!response.ok) {
        throw new Error(`Failed to fetch overview: ${response.statusText}`);
      }

      const payload = await response.json();
      const points: DailyPoint[] = payload.data || [];

      setData(
        points.map((point) => ({
          day: new Date(point.date).toLocaleDateString(undefined, { weekday: 'short' }),
          calls: point.total,
          qualified: point.qualified,
        })),
      );
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        setError(err.message || 'An error occurred fetching the overview');
      }
    } finally {
      setIsLoading(false);
    }
  }, [range, vertical]);

  useEffect(() => {
    fetchOverview();
    return () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
  }, [fetchOverview]);

  useAutoRefresh(fetchOverview, DEFAULT_POLL_MS);

  return { data, isLoading, error, refresh: fetchOverview };
}

/**
 * Minimal JSON GET with abort + auto-refresh, used by the report panels.
 *
 * Exported so hooks/use-credits.ts can reuse it rather than copying the same
 * abort/poll/visibility handling a second time.
 */
export function useJson<T>(url: string, pick: (payload: any) => T, initial: T, pollMs = 60_000) {
  const [data, setData] = useState<T>(initial);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const pickRef = useRef(pick);
  pickRef.current = pick;

  const fetchJson = useCallback(async () => {
    if (abortControllerRef.current) abortControllerRef.current.abort();
    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    setIsLoading(true);
    setError(null);
    try {
      const response = await fetch(url, { signal: abortController.signal });
      if (!response.ok) throw new Error(`Request failed: ${response.status}`);
      setData(pickRef.current(await response.json()));
    } catch (err: any) {
      if (err.name !== 'AbortError') setError(err.message || 'Request failed');
    } finally {
      setIsLoading(false);
    }
  }, [url]);

  useEffect(() => {
    fetchJson();
    return () => abortControllerRef.current?.abort();
  }, [fetchJson]);

  useAutoRefresh(fetchJson, pollMs);

  return { data, isLoading, error, refresh: fetchJson };
}

export interface SourcePoint {
  source: string;
  count: number;
}

/** Lead source split. The Reports page previously drew a hard-coded empty pie. */
export function useSources(vertical: VerticalFilter = 'all') {
  return useJson<SourcePoint[]>(`/api/reports/sources?vertical=${vertical}`, (p) => p.data ?? [], []);
}

export interface WeeklyPoint {
  week: string;
  leads: number;
  calls: number;
  qualified: number;
}

export function useWeekly(vertical: VerticalFilter = 'all') {
  return useJson<WeeklyPoint[]>(`/api/reports/weekly?vertical=${vertical}`, (p) => p.data ?? [], []);
}

export interface ServiceHealth {
  state: 'connected' | 'error' | 'not_configured';
  detail: string;
}

export interface HealthPayload {
  healthy: boolean;
  services: Record<string, ServiceHealth>;
}

/** Real integration status behind the badges that used to always read "Connected". */
export function useHealth() {
  return useJson<HealthPayload | null>('/api/health', (p) => p, null, 60_000);
}

export interface QualityIssue {
  tag: string;
  label: string;
  fix: string | null;
  count: number;
  share: number;
  example: string | null;
  call_id: string | null;
}

export interface QualityPayload {
  calls_total: number;
  calls_reviewed: number;
  avg_quality_score: number | null;
  sentiments: Record<string, number>;
  issues: QualityIssue[];
}

/**
 * What the AI agent is getting wrong, aggregated from Dograh's per-call QA
 * verdicts. This is the feedback loop: fix the top row, watch it shrink.
 */
export function useQuality(days = 30, vertical: VerticalFilter = 'all') {
  return useJson<QualityPayload | null>(`/api/reports/quality?days=${days}&vertical=${vertical}`, (p) => p, null, 60_000);
}
