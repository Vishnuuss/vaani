import { useState, useEffect, useCallback, useRef } from 'react';

/** How often live tables refresh themselves, in ms. */
export const DEFAULT_POLL_MS = 15_000;

export function usePolling<T>(url: string | null, intervalMs: number, enabled: boolean) {
  const [data, setData] = useState<T | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  const fetchData = useCallback(async () => {
    if (!url) return;

    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }

    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    setIsLoading(true);
    try {
      const response = await fetch(url, { signal: abortController.signal });
      if (!response.ok) {
        throw new Error(`Error: ${response.status} ${response.statusText}`);
      }
      const json = await response.json();
      setData(json);
      setError(null);
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        setError(err.message || 'An error occurred during fetching');
      }
    } finally {
      setIsLoading(false);
    }
  }, [url]);

  useEffect(() => {
    if (enabled && url) {
      fetchData(); // Initial fetch
      const intervalId = setInterval(fetchData, intervalMs);
      return () => {
        clearInterval(intervalId);
        if (abortControllerRef.current) {
          abortControllerRef.current.abort();
        }
      };
    }
    
    return () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
  }, [enabled, url, intervalMs, fetchData]);

  return { data, isLoading, error, refresh: fetchData };
}

/**
 * Re-runs `refresh` on a timer, pausing while the tab is hidden.
 *
 * Every data hook in this app fetched exactly once on mount, which is why call
 * logs, stats and campaign status never changed until the page was reloaded by
 * hand - the webhook wrote to the database and the open dashboard never asked
 * again.
 */
export function useAutoRefresh(refresh: () => void, intervalMs: number = DEFAULT_POLL_MS, enabled = true) {
  // Kept in a ref so a new function identity each render does not restart the timer.
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  useEffect(() => {
    if (!enabled || intervalMs <= 0) return;

    const tick = () => {
      // Polling a background tab burns quota and database reads for nothing.
      if (typeof document !== 'undefined' && document.hidden) return;
      refreshRef.current();
    };

    const id = setInterval(tick, intervalMs);

    // Catch up immediately when the user comes back to the tab.
    const onVisible = () => {
      if (typeof document !== 'undefined' && !document.hidden) refreshRef.current();
    };
    document.addEventListener('visibilitychange', onVisible);

    return () => {
      clearInterval(id);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [intervalMs, enabled]);
}
