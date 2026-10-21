"use client";

import { useEffect, useState } from "react";
import { getJson, type AppConfig, type CallType } from "@/lib/api";

// Data that does not change while the page is open, fetched once and shared by every component that asks.
const cache = new Map<string, Promise<unknown>>();

function useOnce<T>(url: string): T | null {
  const [data, setData] = useState<T | null>(null);
  useEffect(() => {
    let alive = true;
    if (!cache.has(url))
      cache.set(
        url,
        getJson<T>(url).catch((e) => (cache.delete(url), Promise.reject(e))),
      );
    (cache.get(url) as Promise<T>).then((d) => alive && setData(d)).catch(() => {});
    return () => {
      alive = false;
    };
  }, [url]);
  return data;
}

/** The tags a store order can carry, as the Store orders page names them: the "Fetch from store" filter. */
export const useStoreTags = () => useOnce<{ key: string; label: string }[]>("/api/store/order-tags");

/** The kinds of call a campaign can be made of. */
export const useCallTypes = () => useOnce<CallType[]>("/api/call-types");

/** Agents, voices, languages. Not cached for good: its build changes on every server restart. */
export function useConfig() {
  const [cfg, setCfg] = useState<AppConfig | null>(null);
  useEffect(() => {
    const ctrl = new AbortController();
    getJson<AppConfig>("/api/config", ctrl.signal)
      .then(setCfg)
      .catch(() => {});
    return () => ctrl.abort();
  }, []);
  return cfg;
}
