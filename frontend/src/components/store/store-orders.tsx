"use client";

import { useState } from "react";
import { Download, Phone, Search, Settings2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button, LinkButton } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useFeedback } from "@/components/ui/feedback";
import { PAGE_SIZE, Pager } from "@/components/ui/pager";
import { Select } from "@/components/ui/select";
import { usePoll } from "@/hooks/use-poll";
import { useConfig } from "@/hooks/use-static";
import { getJson, send, type AutoStatus, type OrdersPage, type OrderQuota, type OrderRow, type StoreStatus } from "@/lib/api";
import { fmtClock, fmtInt } from "@/lib/format";
import { cn } from "@/lib/utils";
import { fitVoice } from "@/lib/voices";
import { OrderOutcomesCard, type OrderOutcomes } from "@/components/campaigns/order-outcomes";
import { BatchesCard } from "./batches-card";
import { OrderPanel } from "./order-panel";
import { Items, TagChip, quotaText, rupees, toneOf, topTags, when } from "./shared";
import { StoreSettingsDialog } from "./store-settings";

type Preset = "today" | "yesterday" | "7" | "30" | "custom";
const PRESETS: { key: Preset; label: string }[] = [
  { key: "today", label: "Today" },
  { key: "yesterday", label: "Yesterday" },
  { key: "7", label: "7 days" },
  { key: "30", label: "30 days" },
  { key: "custom", label: "Custom" },
];

/** An Indian date (YYYY-MM-DD), `back` days ago. */
const istDay = (back = 0) => new Date(Date.now() + 330 * 60_000 - back * 86_400_000).toISOString().slice(0, 10);

function range(p: Preset, from: string, to: string): [string, string] {
  const t = istDay();
  if (p === "today") return [t, t];
  if (p === "yesterday") return [istDay(1), istDay(1)];
  if (p === "7" || p === "30") return [istDay(Number(p) - 1), t];
  return [from || t, to || from || t];
}

// The cards double as tabs: who is left, who was reached, who did not pick up or was busy, who is booked for later.
type View = { key: string; label: string; dot: string };
const VIEWS: View[] = [
  { key: "left", label: "Left to call", dot: "bg-warn" },
  { key: "called", label: "Called", dot: "bg-good" },
  { key: "no_pick", label: "Didn't pick", dot: "bg-bad" },
  { key: "busy", label: "Busy", dot: "bg-bad" },
  { key: "scheduled", label: "Scheduled", dot: "bg-info" },
  { key: "all", label: "All orders", dot: "bg-muted-foreground" },
];

/** The small line under a card's number. */
function viewNote(key: string, c: Record<string, number>, q: OrderQuota | undefined) {
  if (key === "left")
    return c.calling
      ? `${c.calling} on a call now`
      : q?.limit
        ? q.room
          ? `${q.room} more today · limit ${q.limit} a day`
          : `batch of ${q.limit} done`
        : "not called yet";
  if (key === "called")
    return (
      [c.confirmed && `${c.confirmed} confirmed`, c.cancelled && `${c.cancelled} cancelled`, c.review && `${c.review} need a look`]
        .filter(Boolean)
        .join(" · ") || "none yet"
    );
  if (key === "no_pick" || key === "busy") return "tried again later";
  if (key === "scheduled") return "call booked for later";
  const n = (c.not_calling || 0) + (c.not_picked || 0) + (c.too_late || 0);
  return n ? `${fmtInt(n)} not being called` : "";
}

function callLine(r: OrderRow) {
  if (r.state === "scheduled" && r.next_try_at) return `at ${when(r.next_try_at)}`;
  if ((r.state === "no_pick" || r.state === "busy") && r.next_try_at) return `tries again ${when(r.next_try_at)}`;
  if (r.last_call) return `${when(r.last_call.at)} · ${fmtClock(r.last_call.seconds)}`;
  return "";
}

const PER_PAGE = PAGE_SIZE;

/** Store orders: every order of the chosen days, where its call stands, one side panel per order, and the settings
 *  for which orders get a call and automatic calling. */
export function StoreOrders() {
  const { toast, confirm } = useFeedback();
  const cfg = useConfig();
  const [preset, setPreset] = useState<Preset>("today");
  const [from, setFrom] = useState(istDay());
  const [to, setTo] = useState(istDay());
  const [tab, setTab] = useState("left");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [tag, setTag] = useState("");
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const [dayFrom, dayTo] = range(preset, from, to);
  const qs = new URLSearchParams({ day_from: dayFrom, day_to: dayTo, tab, q: query, tag, page: String(page), per_page: String(PER_PAGE) });
  const orders = usePoll<OrdersPage>(`/api/orders?${qs}`, 10_000);
  const auto = usePoll<AutoStatus>("/api/auto", 5_000);
  const store = usePoll<StoreStatus>("/api/store", 30_000);
  const cid = store.data?.campaign_id;
  const outcomes = usePoll<OrderOutcomes>(cid ? `/api/sales/confirmations?campaign_id=${cid}` : null, 300_000);
  const d = orders.data;
  const c = d?.counts ?? {};

  // Typing waits a moment before it asks the server.
  const [typing, setTyping] = useState<ReturnType<typeof setTimeout> | null>(null);
  const onSearch = (v: string) => {
    setQ(v);
    if (typing) clearTimeout(typing);
    setTyping(
      setTimeout(() => {
        setQuery(v.trim());
        setPage(1);
      }, 350),
    );
  };
  const pick = (fn: () => void) => {
    fn();
    setPage(1);
  };

  const callNow = async (name: string) => {
    try {
      const contact = await send<{ id: number; customer_name: string; phone: string }>("/api/order/contact", "POST", { name });
      const a = auto.data ?? (await getJson<AutoStatus>("/api/auto"));
      const ok = await confirm({
        title: `Call ${contact.customer_name || "the customer"} about order ${name}?`,
        body: (
          <>
            Rings: {a.dial_only_my_number ? `your own phone ${a.my_number} (test mode)` : contact.phone || "no phone number"}
            <br />
            Agent: {cfg?.model_labels[a.settings.model] ?? a.settings.model} · {a.settings.voice}. This is a real, paid phone call.
          </>
        ),
        confirmLabel: "Call now",
      });
      if (!ok) return;
      const s = a.settings;
      const model = s.model || cfg?.model || "";
      const voice = cfg ? fitVoice(cfg, model, s.voice || cfg.voice) : s.voice;
      const p = new URLSearchParams({ voice, model, language: s.language || "", build: cfg?.build ?? "" });
      await send(`/api/calls/dial/${contact.id}?${p}`, "POST");
      toast("Calling. It shows on the Overview under Live calls.");
      setTimeout(orders.reload, 1500);
    } catch (e) {
      toast((e as Error).message, true);
    }
  };

  const a = auto.data;
  const s = store.data;
  const first = d ? (d.page - 1) * PER_PAGE + 1 : 0;

  return (
    <div className="pb-10">
      <div className="flex flex-wrap items-start gap-3 border-b px-4 pt-5 pb-4 sm:px-6">
        <div className="min-w-0">
          <h1 className="text-[20px] font-semibold tracking-tight">Store orders</h1>
          <p className="mt-0.5 text-[13.5px] text-muted-foreground">Orders from your store, and where each one&apos;s confirmation call stands.</p>
        </div>
        <div className="ml-auto flex flex-wrap gap-2">
          <LinkButton href={`/api/orders/export?${qs}`} download title="The orders shown, with their products, prices and tags, as an Excel file">
            <Download />
            Export to Excel
          </LinkButton>
          <Button variant="primary" onClick={() => setSettingsOpen(true)}>
            <Settings2 />
            Settings
          </Button>
        </div>
      </div>

      <div className="space-y-4 px-4 pt-5 sm:px-6">
        {/* Automatic calling, in one line */}
        {a && (
          <div
            className={cn(
              "flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-lg border px-4 py-2.5 text-[13px]",
              a.running ? "border-good/30 bg-good-soft" : "border-warn/30 bg-warn-soft",
            )}
          >
            <span className={cn("relative flex size-2.5 shrink-0", a.running ? "text-good" : "text-warn")}>
              {a.running && <span className="absolute inset-0 animate-ping rounded-full bg-current opacity-60" />}
              <span className="relative size-2.5 rounded-full bg-current" />
            </span>
            <span className="min-w-0 flex-1">
              {a.running ? (
                <>
                  <b className="font-semibold text-good">Automatic calling is on</b>
                  <span className="text-foreground/80">
                    {" "}
                    · {a.settings.start_hour}:00 to {a.settings.end_hour}:00 IST
                    {a.settings.budget_rs ? ` · up to ₹${a.settings.budget_rs} a day` : ""}
                    {a.calls_made ? ` · ${a.calls_made} calls so far` : ""}
                    {a.dial_only_my_number ? " · test mode: rings your own phone" : ""}
                  </span>
                </>
              ) : (
                <>
                  <b className="font-semibold text-warn">Automatic calling is off.</b>
                  <span className="text-foreground/80">
                    {" "}
                    Orders under &quot;Left to call&quot; are not being rung. Call them one by one, or switch it on in Settings.
                  </span>
                </>
              )}
              {s?.since && (
                <span className="block text-[12px] text-muted-foreground">
                  Calls only orders placed after {when(s.since)}: {quotaText(s.quota)}. An order not called within {s.settings.order_max_hours} h is dropped.
                </span>
              )}
            </span>
            {!a.running && (
              <Button size="sm" variant="outline" onClick={() => setSettingsOpen(true)}>
                Switch on
              </Button>
            )}
          </div>
        )}

        {/* Dates */}
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex flex-wrap gap-1.5" role="group" aria-label="Dates">
            {PRESETS.map((p) => (
              <button
                key={p.key}
                type="button"
                aria-pressed={preset === p.key}
                onClick={() => pick(() => setPreset(p.key))}
                className={cn(
                  "h-8 rounded-md border border-input px-3 text-[13px] font-medium transition-colors",
                  preset === p.key ? "bg-muted text-foreground" : "bg-card text-foreground/85 hover:bg-muted",
                )}
              >
                {p.label}
              </button>
            ))}
          </div>
          {preset === "custom" && (
            <div className="flex items-center gap-1.5 text-[13px] text-muted-foreground">
              <input
                type="date"
                aria-label="From"
                className="h-8 rounded-md border border-input bg-card px-2.5 text-[13px] text-foreground"
                value={from}
                max={to || istDay()}
                onChange={(e) => pick(() => setFrom(e.target.value))}
              />
              to
              <input
                type="date"
                aria-label="To"
                className="h-8 rounded-md border border-input bg-card px-2.5 text-[13px] text-foreground"
                value={to}
                min={from}
                max={istDay()}
                onChange={(e) => pick(() => setTo(e.target.value))}
              />
            </div>
          )}
        </div>

        {/* The cards, which pick the list below */}
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6" role="tablist" aria-label="Orders">
          {VIEWS.map((v) => {
            const on = tab === v.key;
            return (
              <button
                key={v.key}
                type="button"
                role="tab"
                aria-selected={on}
                onClick={() => pick(() => setTab(v.key))}
                className={cn(
                  "rounded-lg border bg-card px-4 py-3.5 text-left transition-colors hover:bg-muted/40",
                  on && "border-brand bg-brand-soft/40 ring-1 ring-brand hover:bg-brand-soft/40",
                )}
              >
                <div className="flex items-center gap-2 text-[13px] text-muted-foreground">
                  <span className={cn("size-2 rounded-full", v.dot)} />
                  {v.label}
                </div>
                <div className="mt-1.5 text-[26px] leading-none font-semibold tracking-tight tabular">{d ? fmtInt(c[v.key] || 0) : "—"}</div>
                <div className="mt-2 truncate text-[12px] text-muted-foreground">{d ? viewNote(v.key, c, d.quota) : ""}</div>
              </button>
            );
          })}
        </div>

        <Card className="overflow-hidden">
          <div className="flex flex-wrap items-center gap-2 border-b px-4 py-3">
            <div className="relative w-full max-w-[320px]">
              <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
              <input
                className="h-8 w-full rounded-md border border-input bg-card pr-3 pl-8 text-[13px] outline-none focus-visible:border-brand focus-visible:ring-2 focus-visible:ring-brand/20"
                placeholder="Search order no., name, phone or product"
                value={q}
                onChange={(e) => onSearch(e.target.value)}
              />
            </div>
            <Select className="w-[200px]" value={d?.tags.includes(tag) ? tag : ""} onChange={(e) => pick(() => setTag(e.target.value))} aria-label="Tag">
              <option value="">All tags</option>
              {(d?.tags ?? []).map((t) => (
                <option key={t}>{t}</option>
              ))}
            </Select>
            {d && d.total > 0 && (
              <span className="ml-auto text-[12.5px] text-muted-foreground tabular">
                {fmtInt(first)}–{fmtInt(first + d.rows.length - 1)} of {fmtInt(d.total)}
              </span>
            )}
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[980px] text-left text-[13.5px]">
              <thead className="text-[12.5px] text-muted-foreground">
                <tr className="border-b">
                  {["Order", "Customer", "Products", "Amount", "Call", ""].map((h, i) => (
                    <th key={i} className={cn("px-4 py-2.5 font-normal", h === "Amount" && "text-right")}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y">
                {d?.rows.map((r) => {
                  const line = callLine(r);
                  const tags = topTags(r.tags);
                  return (
                    <tr key={r.name} className="cursor-pointer align-top hover:bg-muted/40" onClick={() => setOpen(r.name)}>
                      <td className="px-4 py-3 whitespace-nowrap">
                        <div className="font-mono font-medium">{r.name}</div>
                        <div className="text-[12px] text-muted-foreground">{when(r.created_at)}</div>
                      </td>
                      <td className="px-4 py-3">
                        <div className="font-medium">{r.customer || "—"}</div>
                        <div className="font-mono text-[12px] text-muted-foreground">{r.phone ? `+91 ${r.phone}` : "no phone"}</div>
                        {tags.length > 0 && (
                          <div className="mt-1.5 flex flex-wrap gap-1">
                            {tags.map((t) => (
                              <TagChip key={t.label} tag={t} />
                            ))}
                          </div>
                        )}
                      </td>
                      <td className="max-w-[320px] px-4 py-3">
                        <Items items={r.items} />
                      </td>
                      <td className="px-4 py-3 text-right whitespace-nowrap">
                        <div className="font-medium tabular">{rupees(r.amount)}</div>
                        <div className="text-[12px] text-muted-foreground">{r.payment}</div>
                      </td>
                      <td className="px-4 py-3">
                        <Badge tone={toneOf(r.state_tone)} dot className={cn(r.state === "calling" && "animate-pulse")}>
                          {r.state_label}
                        </Badge>
                        {line && <div className="mt-1 text-[12px] text-muted-foreground">{line}</div>}
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={r.state === "calling"}
                          onClick={(e) => {
                            e.stopPropagation();
                            callNow(r.name);
                          }}
                        >
                          <Phone />
                          {r.state === "calling" ? "Calling…" : r.last_call ? "Call again" : "Call"}
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {d && d.rows.length === 0 && (
              <p className="px-4 py-10 text-center text-muted-foreground">{tab === "left" ? "Nobody left to call." : "No orders here."}</p>
            )}
            {!d && <p className="px-4 py-10 text-center text-muted-foreground">{orders.error ? `Could not load the orders: ${orders.error}` : "Loading…"}</p>}
          </div>
          {d && <Pager page={d.page} pages={d.pages} onPage={setPage} total={d.total} size={PER_PAGE} />}
        </Card>

        <BatchesCard />
        {cid && <OrderOutcomesCard data={outcomes.data} />}
      </div>

      {open && (
        <OrderPanel key={open} name={open} onClose={() => setOpen((o) => (o === open ? null : o))} onCall={() => callNow(open)} onChanged={orders.reload} />
      )}
      <StoreSettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        auto={a}
        store={s}
        cfg={cfg}
        onChanged={() => {
          auto.reload();
          store.reload();
          orders.reload();
        }}
      />
    </div>
  );
}
