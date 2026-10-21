"use client";

import { useCallback, useEffect, useState } from "react";
import { getJson } from "@/lib/api";

/** Fetches `url` now and every `everyMs`, keeping the last good data while a refresh is in flight.
 *  Pauses while the tab is hidden. `url` null = don't fetch. reload(): fetch now, e.g. right after an action. */
export function usePoll<T>(url: string | null, everyMs: number) {
  const [data, setData] = useState<T | null>(null);
  const [updatedAt, setUpdatedAt] = useState(0); // Date.now() when `data` arrived
  const [nonce, setNonce] = useState(0);
  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!url) return;
    let ctrl: AbortController | null = null;
    let stopped = false;

    const load = async () => {
      if (document.hidden) return;
      ctrl?.abort();
      ctrl = new AbortController();
      setLoading(true);
      try {
        const d = await getJson<T>(url, ctrl.signal);
        if (!stopped) {
          setData(d);
          setUpdatedAt(Date.now());
          setError(null);
        }
      } catch (e) {
        if (!stopped && (e as Error).name !== "AbortError") setError((e as Error).message);
      } finally {
        if (!stopped) setLoading(false);
      }
    };

    load();
    const timer = setInterval(load, everyMs);
    const onVisible = () => !document.hidden && load();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      ctrl?.abort();
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [url, everyMs, nonce]);

  return { data, error, loading, updatedAt, reload };
}
