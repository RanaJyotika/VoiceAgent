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

/** /api/live/{id}: one call for the side panel. live: still on (all lines so far); false: as saved when it ended. */
export type CallDetail = Partial<Omit<LiveCall, "status" | "lines">> & {
  call_id: number;
  live: boolean;
  status: LiveCall["status"] | "ended";
  lines: LiveCall["lines"];
  outcome_details: Record<string, unknown> | null;
  error?: string | null;
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

/** /api/config: the agents (models), voices and languages a call can use, and this server's build. */
export type AppConfig = {
  model: string; // the default agent
  voice: string; // the default voice
  voices: Record<string, "female" | "male" | string>;
  sarvam_voices: Record<string, string>; // the voices only the Sarvam agent speaks in
  languages: string[];
  models: string[];
  model_labels: Record<string, string>;
  build: string; // sent with every call; a restarted server refuses an older page
};

/** /api/plivo/status: whether real phone calls can be placed. */
export type PhoneLine = {
  ready: boolean;
  missing: string[];
  dial_only_my_number: boolean;
  from_number: string;
};

/** /api/contacts/search: one row of the Make a call picker. */
export type ContactHit = {
  id: number;
  customer_name: string | null;
  phone: string | null;
  order_id: string | null;
  product: string | null;
  quantity: string | null;
  amount: string | null;
  payment_mode: string | null;
  address: string | null;
  status: string | null;
  campaign_id: number | null;
  campaign_name: string | null;
  call_type: string | null;
};

// ---- campaigns ----

export type Column = { key: string; label: string; required: boolean; example: string };
export type Fact = { key: string; label: string; required: boolean; long: boolean; date: boolean; hint: string };

/** /api/call-types: what a campaign's calls are for. */
export type CallType = {
  key: string;
  name: string;
  kind: "service" | "promotional" | string;
  description: string;
  outcomes: string[];
  positive: string[];
  details: string[];
  summary_keys: string[];
  columns: Column[];
  facts: Fact[];
};

/** Settings a campaign runs with (and last ran with). */
export type RunSettings = {
  campaign_id: number;
  voice: string;
  model: string;
  language: string;
  max_concurrent: number;
  max_attempts: number;
  retry_minutes: number;
  start_hour: number;
  end_hour: number;
  limit: number;
  budget_rs: number;
  /** Weekdays calls may be placed on, Monday = "0": "012345" is Mon to Sat. */
  days: string;
};

/** /api/campaigns: one row of the list. */
export type CampaignRow = {
  id: number;
  name: string;
  call_type: string;
  call_type_name: string;
  source: string; // "store": the Store orders campaign, which has its own page
  auto: number;
  running: boolean;
  total: number;
  pending: number;
  by_status: Record<string, number>;
  facts: Record<string, string>;
  last_call_date: string | null;
  last_call_at: string | null;
  created_at: string;
  /** When it starts calling by itself (Indian time, ISO); null = not scheduled. */
  start_at?: string | null;
  /** What happened to its last scheduled start. */
  start_note?: string | null;
};

export type CampaignStatus = "running" | "scheduled" | "paused" | "completed" | "draft";

/** /api/campaigns/board: the Campaigns page. Called = connected + not connected + hung up (+ any call in progress). */
export type BoardRow = {
  id: number;
  name: string;
  call_type: string;
  auto: boolean;
  created_at: string;
  last_call_at: string | null;
  status: CampaignStatus;
  model: string;
  agent: string; // "" when the campaign has never run
  voice: string;
  contacts: number;
  called: number;
  to_call: number;
  connected: number;
  not_connected: number;
  hung_up: number;
  agent_rs: number;
  line_rs: number;
  cost_rs: number;
  start_at: string | null;
  start_note: string | null;
};

export type Board = { campaigns: BoardRow[]; counts: Record<CampaignStatus, number> };

/** /api/campaign?campaign_id=: how one campaign's calling stands. */
export type CampaignState = {
  running: boolean;
  running_campaign_id: number | null;
  campaign_id: number;
  last_call_date: string;
  past_last_date: boolean;
  reason: string;
  calls_made: number;
  in_progress: number;
  within_hours: boolean;
  now_ist: string;
  /** Outside the calling days or window: when the next call goes ("" when calling is allowed now). */
  waiting: string;
  dial_only_my_number: boolean;
  my_number: string;
  max_concurrent_allowed: number;
  settings: RunSettings;
  total: number;
  pending: number;
  to_call: number;
  retry_due: number;
  retry_left: number;
  by_status: Record<string, number>;
};

/** /api/contacts?campaign_id=: one contact with its latest call. */
export type ContactRow = {
  id: number;
  customer_name: string;
  phone: string;
  order_id: string;
  product: string;
  quantity: string;
  amount: string;
  payment_mode: string;
  notes: string;
  status: string;
  attempts: number;
  last_call_at: string | null;
  next_try_at: string | null;
  last_call_id: number | null;
  last_duration: number | null;
  last_error: string | null;
  last_recording: string | null;
  last_result: string | null;
  last_details: Record<string, string>;
  extra: Record<string, { label: string; value: string } | string>;
};

/** /api/calls: one call, newest first. */
export type CallRow = {
  id: number;
  contact_id: number | null;
  channel: "plivo" | "browser" | string;
  started_at: string;
  duration_sec: number | null;
  outcome: string | null;
  outcome_details: Record<string, unknown> | null;
  transcript: { role: string; text: string }[] | null;
  error: string | null;
  customer_name: string | null;
  order_id: string | null;
  phone: string | null;
  campaign_id: number | null;
  campaign_name: string | null;
  cost_rs: number;
  cost_estimated: boolean; // no token report: a per-minute guess
  recording: string | null;
};

/** /api/callbacks: everyone waiting for another call. */
export type CallbackList = {
  rows: (ContactRow & { campaign_name: string | null })[];
  max_attempts: number;
  running: boolean;
};

export type PreviewIssue = "ok" | "no_phone" | "duplicate" | "on_list" | "do_not_call";

/** /api/campaigns/{id}/preview: what an uploaded sheet holds, before anything is added. */
export type SheetPreview = {
  rows: (Record<string, unknown> & {
    row: number;
    issue: PreviewIssue;
    customer_name?: string;
    phone?: string;
    extra?: Record<string, { label: string; value: string }>;
  })[];
  total: number;
  columns: string[];
  extras: { key: string; label: string }[];
  missing: string[];
  counts: Record<PreviewIssue, number>;
};

/** POST/PUT/DELETE with a JSON body; throws the server's own message on failure. */
export async function send<T = Record<string, unknown>>(url: string, method: "POST" | "PUT" | "DELETE", body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok)
    throw new Error(j.detail || (r.status === 404 ? "the server is running older code. Restart run_phone.bat (or run.bat)" : `${r.status} ${r.statusText}`));
  return j as T;
}

export async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const r = await fetch(url, { signal, cache: "no-store" });
  if (!r.ok) {
    let detail = `${r.status} ${r.statusText}`;
    try {
      detail = (await r.json()).detail ?? detail;
    } catch {
      // Not JSON: an HTML 404 from a server started before this address existed.
      if (r.status === 404) detail = "the server is running older code. Restart run_phone.bat (or run.bat) and try again";
    }
    throw new Error(detail);
  }
  return r.json() as Promise<T>;
}
