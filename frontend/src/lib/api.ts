// Types and fetchers for the Python server's API (backend/app). Same origin: in development the
// Next.js dev server forwards /api to the Python server (next.config.ts); in production Python serves this app.

export type Range = "today" | "yesterday" | "7d" | "30d" | "custom";

export type Totals = {
  calls: number;
  connected: number;
  conversions: number;
  failed: number;
  spend_rs: number;
};

export type SeriesPoint = {
  label: string;
  calls: number;
  connected: number;
  conversions: number;
  failed: number;
};

export type AttentionItem = {
  level: "critical" | "warning" | "info";
  title: string;
  detail: string;
  page: string; // a page of the classic dashboard: history, store, spend...
};

export type LiveCall = {
  call_id: number;
  channel: "plivo" | "browser" | string;
  status: "ringing" | "agent_speaking" | "listening" | "ending" | string;
  seconds: number;
  ring_left: number | null;
  campaign_id: number | null;
  customer_name: string;
  order_id: string;
  product: string;
  to: string;
  voice: string;
  agent: string;
  outcome: string | null;
  lines: { role: string; text: string }[];
};

export type Campaign = { id: number; name: string };

export type Overview = {
  range: Range;
  from: string;
  to: string;
  bucket: "hour" | "day";
  now: Totals;
  before: Totals;
  live: LiveCall[];
  queued: number;
  waiting: number;
  series: SeriesPoint[];
  attention: AttentionItem[];
  campaigns: Campaign[];
  spend_today_rs: number;
  budget_rs: number;
  auto: { enabled: boolean; running: boolean; test_mode: boolean };
};

export type OverviewQuery = {
  range: Range;
  dayFrom?: string;
  dayTo?: string;
  campaignId?: number;
};

export function overviewUrl(q: OverviewQuery): string {
  const p = new URLSearchParams({ range: q.range });
  if (q.range === "custom") {
    p.set("day_from", q.dayFrom ?? "");
    p.set("day_to", q.dayTo ?? "");
  }
  if (q.campaignId) p.set("campaign_id", String(q.campaignId));
  return `/api/overview?${p}`;
}

export async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const r = await fetch(url, { signal, cache: "no-store" });
  if (!r.ok) {
    let detail = `${r.status} ${r.statusText}`;
    try {
      detail = (await r.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return r.json() as Promise<T>;
}
