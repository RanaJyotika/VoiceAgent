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
  drive_link?: string; // the recording, both sides in one MP3, on Google Drive (drive_sync.py)
  /** What the call was about: the order or cart and its products, or a one-off call's reason. */
  subject?: CallSubject | null;
  /** Store orders placed within a week of this call by the same phone number (sales.py). */
  orders?: CallOrder[];
};

export type CallOrder = {
  order_name: string;
  placed_at: string;
  amount: number | null;
  payment: string;
  product: string;
  talked: number; // 1: the customer talked with the agent before ordering
  hours_after: number;
  bucket: string | null; // the store's outcome: New, In Transit, Delivered, Cancellation, NDR & RTO...
};

export type CallSubject = {
  kind: string; // order_confirmation, abandoned_cart, custom...
  kind_name: string;
  order_id: string;
  product: string;
  quantity: string;
  amount: string;
  payment_mode: string;
  address: string;
  delivery_date: string;
  reason: string;
  cart_link: string;
  products: { title: string; image: string; link: string }[];
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

/** /api/campaigns/board: the Campaigns page. Called = contacts rung at least once. */
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
  new_left: number; // never rung yet
  callbacks_left: number; // rung before, waiting for another try
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
  new_left: number; // never rung yet
  reached: number; // rung at least once
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
  drive_link?: string | null; // the same recording on Google Drive, once copied
};

/** What happens next to a call-back: rung at its time (scheduled), next in line (due), its time came but nothing is
 *  ringing it (not_rung), out of tries (no_tries), on the line now (calling), or rung again with a result (done). */
export type CallbackState = "scheduled" | "due" | "not_rung" | "no_tries" | "calling" | "done";

export type CallbackRow = ContactRow & {
  campaign_name: string | null;
  state: CallbackState;
  state_note: string;
  max_attempts?: number; // waiting rows: the tries its campaign allows
  was?: string | null; // done / calling rows: why the earlier call put them on the list
  last_outcome?: string | null;
};

/** /api/callbacks: everyone waiting for another call, and the call-backs of the last 30 days that were rung again. */
export type CallbackList = {
  rows: CallbackRow[];
  moved_on: CallbackRow[];
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

// ---------------- Store orders (backend: orders_view.py, store_orders.py, autodial.py) ----------------

/** A tag on an order; tone as the server names it: warning, accent, good, critical, serious, or "". */
export type OrderTag = { label: string; tone: string };
export type OrderItem = { title: string; variant: string; qty: number; image: string; price: string; sku: string };

/** The day's limit on new orders for automatic calling (store_orders.quota). */
export type OrderQuota = {
  limit: number;
  pick: "newest" | "oldest" | "random" | string;
  picked_today: number;
  room: number | null;
  days: string;
  days_label: string;
  since: string;
  before: string;
};

/** /api/orders: one order of the list, with where its call stands. */
export type OrderRow = {
  name: string;
  created_at: string;
  customer: string;
  phone: string; // the last 10 digits
  items: OrderItem[];
  amount: string;
  payment: string;
  tags: OrderTag[];
  state: string;
  state_label: string;
  state_tone: string;
  contact_id: number | null;
  attempts: number;
  result: string;
  next_try_at: string | null;
  last_call: { id: number; at: string; seconds: number } | null;
  delivery: string;
};

export type OrdersPage = {
  counts: Record<string, number>;
  tags: string[];
  total: number;
  page: number;
  pages: number;
  rows: OrderRow[];
  quota: OrderQuota;
};

/** /api/order?name=: one order for the side panel. */
export type OrderDetail = OrderRow & {
  email: string;
  address: string;
  store_status: { delivery: string; courier: string; tracking: string; fulfillment: string };
  earlier_orders: number;
  calls: { id: number; at: string; seconds: number; outcome: string; error: string; channel: string }[];
  notes: { text: string; created_at: string }[];
};

export type StoreSettings = {
  orders: "cod" | "all" | "off";
  order_wait_minutes: number;
  order_max_hours: number;
  order_daily_limit: number;
  order_pick: string;
  order_days: string;
};

/** /api/store: where orders come from and which ones get a call. */
export type StoreStatus = {
  enabled: boolean;
  source: string;
  settings: StoreSettings;
  order_days: Record<string, string>;
  quota: OrderQuota;
  last_check: string;
  since: string;
  error: string;
  campaign_id: number | null; // the Store orders campaign the confirmation calls go into
};

/** /api/store/skip: the "don't call orders that are…" switches. */
export type SkipView = { rules: { key: string; label: string; on: boolean }[]; not_calling: number; changed?: { skipped: number; back: number } };

/** /api/auto: automatic calling. */
export type AutoStatus = {
  enabled: boolean;
  running: boolean;
  reason: string;
  calls_made: number;
  in_progress: number;
  settings: RunSettings;
  campaigns: { id: number; name: string; call_type_name: string; to_call: number }[];
  within_hours: boolean;
  now_ist: string;
  dial_only_my_number: boolean;
  my_number: string;
  phone_missing: string[];
};

/** /api/store/batches: each batch of automatic calling, with its results and cost. */
export type OrderBatch = {
  id: number;
  day: string;
  started_at: string;
  size: number;
  orders: number;
  calls: number;
  answered: number;
  confirmed: number;
  cancelled: number;
  not_reached: number;
  hung_up: number;
  call_later: number;
  waiting: number;
  other: number;
  minutes: number;
  cost_rs: number;
  per_order_rs: number | null;
  per_confirmed_rs: number | null;
};
export type BatchReport = {
  batches: OrderBatch[];
  current: { id: number | null; started_at: string; picked: number; limit: number; room: number | null };
};
