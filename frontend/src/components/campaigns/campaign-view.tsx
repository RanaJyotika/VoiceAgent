"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ArrowDownAZ, ArrowUpAZ, ChevronLeft, Copy, Download, Pause, Pencil, Play, Search, Trash2, UserPlus } from "lucide-react";
import { CallbackBoard } from "@/components/calls/callback-list";
import { CallPanel } from "@/components/overview/call-panel";
import { StatCard } from "@/components/overview/stat-card";
import { Badge, type Tone } from "@/components/ui/badge";
import { Button, buttonClass } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useFeedback } from "@/components/ui/feedback";
import { PAGE_SIZE, Pager } from "@/components/ui/pager";
import { Select } from "@/components/ui/select";
import { usePoll } from "@/hooks/use-poll";
import { useCallTypes, useConfig } from "@/hooks/use-static";
import { send, type CampaignRow, type CampaignState, type CampaignStatus, type ContactRow, type RunSettings } from "@/lib/api";
import { fmtInt, fmtOrder, fmtPct, fmtPhone, fmtRs, pct } from "@/lib/format";
import { outcomeOf } from "@/lib/outcomes";
import { cn } from "@/lib/utils";
import { CALLBACK_STATUSES, fmtDay, plural } from "./shared";

/** /api/campaigns/{id}/stats: the Overview tab. Per contact, counted as on the Campaigns page. */
type Stats = {
  status: CampaignStatus;
  running: boolean;
  agent: string;
  voice: string;
  budget_rs: number;
  audience: number;
  called: number;
  connected: number;
  engaged: number;
  converted: number;
  not_connected: number;
  hung_up: number;
  to_call: number;
  agent_rs: number;
  line_rs: number;
  cost_rs: number;
  calls: number;
  series: { day: string; label: string; calls: number; connected: number }[];
  outcomes: { key: string; n: number }[];
};

type Campaign = CampaignRow & { settings: Partial<RunSettings> | null };

const STATUS: Record<CampaignStatus, { label: string; tone: Tone }> = {
  running: { label: "Running", tone: "info" },
  scheduled: { label: "Scheduled", tone: "warn" },
  paused: { label: "Paused", tone: "warn" },
  completed: { label: "Completed", tone: "good" },
  draft: { label: "Draft", tone: "neutral" },
};

const PAGE = PAGE_SIZE;
const rs = (n: number) => fmtRs(n, n < 100 ? 2 : 0);

/** One campaign (?id=): Overview (numbers, funnel, calls per day, outcomes) and Contacts (who is on the list, what
 *  happened, the conversation on a click). Settings and the audience are changed in the wizard (Edit). */
export function CampaignView() {
  const id = Number(useSearchParams().get("id") || 0);
  const router = useRouter();
  const { toast, confirm } = useFeedback();
  const types = useCallTypes();
  const cfg = useConfig();
  const [tab, setTab] = useState<"overview" | "contacts" | "callback">("overview");
  const [busy, setBusy] = useState(false);

  const campPoll = usePoll<Campaign>(id ? `/api/campaigns/${id}` : null, 10_000);
  const statsPoll = usePoll<Stats>(id ? `/api/campaigns/${id}/stats` : null, 5_000);
  const statePoll = usePoll<CampaignState>(id ? `/api/campaign?campaign_id=${id}` : null, 5_000);
  const contactsPoll = usePoll<ContactRow[]>(id ? `/api/contacts?campaign_id=${id}` : null, tab === "contacts" ? 5_000 : 30_000);

  const camp = campPoll.data;
  const s = statsPoll.data;
  const state = statePoll.data?.campaign_id === id ? statePoll.data : null;
  const type = types?.find((t) => t.key === camp?.call_type);
  // On the Call back tab: no answer, busy, dropped and call later, still waiting for another call.
  const waitingBack = (contactsPoll.data ?? []).filter((c) => CALLBACK_STATUSES.includes(c.status)).length;
  const refresh = () => {
    campPoll.reload();
    statsPoll.reload();
    statePoll.reload();
    contactsPoll.reload();
  };

  if (!id || campPoll.error) {
    return (
      <div className="px-4 pt-10 text-center sm:px-6">
        <p className="text-muted-foreground">{id ? `Could not load this campaign: ${campPoll.error}` : "No campaign chosen."}</p>
        <Link href="/campaigns/" className={buttonClass("outline", "md", "mt-4")}>
          Back to campaigns
        </Link>
      </div>
    );
  }

  const status = s ? STATUS[s.status] : null;
  const otherRunning = !!state?.running && state.running_campaign_id !== id;

  const start = async () => {
    if (!state || !cfg || !camp) return;
    const set = state.settings;
    const ok = await confirm({
      title: `Start calling ${plural(state.to_call, "contact")}?`,
      body: (
        <>
          {state.dial_only_my_number ? `Test mode: all of them ring your own phone (${state.my_number}).` : "They ring the customers' numbers."}
          <br />
          Agent: {cfg.model_labels[set.model] ?? (set.model || "the default")} · {set.voice}. Calling hours {set.start_hour}:00–{set.end_hour}:00.
          <br />
          Every call is a real, paid phone call. Pause at any time; the call in progress finishes first.
          {state.waiting && (
            <span className="mt-2 block rounded-md bg-warn-soft px-3 py-2 text-[13px] text-warn">
              No call goes right now. {state.waiting.replace(/^Waiting: this/, "This")} To call now, Cancel and change Days or the Calling window (Edit →
              Schedule).
            </span>
          )}
        </>
      ),
      confirmLabel: state.waiting ? "Start and wait" : "Start calling",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await send("/api/campaign/start", "POST", { ...set, campaign_id: id, build: cfg.build });
      toast("Calling started. Live calls show on the Overview.");
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy(false);
      refresh();
    }
  };

  const pause = async () => {
    setBusy(true);
    try {
      await send("/api/campaign/stop", "POST");
      toast("Paused after the call in progress.");
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy(false);
      refresh();
    }
  };

  const duplicate = async () => {
    if (!camp) return;
    setBusy(true);
    try {
      const copy = await send<Campaign>("/api/campaigns", "POST", {
        name: `${camp.name} (copy)`.slice(0, 120),
        call_type: camp.call_type,
        facts: camp.facts,
        last_call_date: camp.last_call_date || "",
        settings: camp.settings && Object.keys(camp.settings).length ? camp.settings : undefined,
        draft: true,
      });
      toast("Copied, without its contacts. Add the audience next.");
      router.push(`/campaigns/new/?edit=${copy.id}&step=3`);
    } catch (e) {
      toast((e as Error).message, true);
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!camp) return;
    const n = s?.audience ?? 0;
    const ok = await confirm({
      title: `Delete "${camp.name}"?`,
      body: `${n ? `Its ${plural(n, "contact")} go too. ` : ""}Call history and spending keep their records.`,
      confirmLabel: "Delete campaign",
      danger: true,
    });
    if (!ok) return;
    try {
      await send(`/api/campaigns/${camp.id}`, "DELETE");
      toast("Campaign deleted.");
      router.push("/campaigns/");
    } catch (e) {
      toast((e as Error).message, true);
    }
  };

  const cancelSchedule = async () => {
    try {
      await send(`/api/campaigns/${id}/schedule`, "POST", { at: null });
      toast("Scheduled start cancelled.");
    } catch (e) {
      toast((e as Error).message, true);
    }
    refresh();
  };

  const startsAt = camp?.start_at
    ? new Date(camp.start_at).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false })
    : "";

  return (
    <div className="pb-10">
      <div className="flex flex-wrap items-start gap-3 border-b px-4 pt-5 pb-4 sm:px-6">
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-[20px] font-semibold tracking-tight">{camp?.name ?? "…"}</h1>
          <p className="mt-0.5 truncate text-[13.5px] text-muted-foreground">
            {[type?.name, s?.agent ? `${s.voice} — ${s.agent}` : "", camp ? `created ${fmtDay(camp.created_at)}` : ""].filter(Boolean).join(" · ")}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Link href="/campaigns/" className={buttonClass("ghost", "md")}>
            <ChevronLeft />
            Campaigns
          </Link>
          {status && (
            <Badge tone={status.tone} dot>
              {status.label}
            </Badge>
          )}
          <Link href={`/campaigns/new/?edit=${id}`} className={buttonClass("outline")}>
            <Pencil />
            Edit
          </Link>
          <Button variant="outline" onClick={duplicate} disabled={busy || !camp}>
            <Copy />
            Duplicate
          </Button>
          {s?.running ? (
            <Button variant="outline" onClick={pause} disabled={busy}>
              <Pause />
              Pause
            </Button>
          ) : (
            <Button
              variant="primary"
              onClick={start}
              disabled={busy || !state?.to_call || otherRunning || !!state?.past_last_date || !cfg}
              title={otherRunning ? "Another campaign is calling right now" : !state?.to_call ? "Nobody left to call" : "Places real, paid phone calls"}
            >
              <Play />
              Start
            </Button>
          )}
          <Button variant="ghost" size="icon" onClick={remove} aria-label="Delete campaign" title="Delete campaign" className="text-bad hover:bg-bad-soft">
            <Trash2 />
          </Button>
        </div>
      </div>

      <div className="space-y-4 px-4 pt-5 sm:px-6">
        <div className="inline-flex rounded-lg bg-muted p-1" role="tablist">
          {(
            [
              ["overview", "Overview"],
              ["contacts", "Contacts"],
              ["callback", "Call back"],
            ] as const
          ).map(([k, label]) => (
            <button
              key={k}
              type="button"
              role="tab"
              aria-selected={tab === k}
              onClick={() => setTab(k)}
              className={cn(
                "h-8 rounded-md px-4 text-[13.5px] transition-colors",
                tab === k ? "bg-card font-medium text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
              )}
            >
              {label}
              {k === "callback" && waitingBack > 0 && <span className="ml-1.5 text-[12px] text-muted-foreground tabular">{fmtInt(waitingBack)}</span>}
            </button>
          ))}
        </div>

        {startsAt && !s?.running && (
          <div className="flex flex-wrap items-center gap-3 rounded-md bg-info-soft px-3 py-2 text-[13px] text-info">
            <span>
              Scheduled: starts calling by itself at <b className="font-medium">{startsAt}</b> (Indian time), with its saved settings.
            </span>
            <Button size="sm" variant="outline" className="ml-auto" onClick={cancelSchedule}>
              Cancel schedule
            </Button>
          </div>
        )}
        {!startsAt && camp?.start_note && <p className="text-[12.5px] text-muted-foreground">Last scheduled start: {camp.start_note}</p>}
        {state?.dial_only_my_number && (
          <p className="rounded-md bg-warn-soft px-3 py-2 text-[12.5px] text-warn">
            Test mode is on: every call of this campaign rings your own phone ({state.my_number}) instead of the customers.
          </p>
        )}
        {s?.running && state?.waiting ? (
          <p className="rounded-md bg-warn-soft px-3 py-2 text-[12.5px] text-warn">
            {state.waiting} To call sooner: Pause, Edit → Schedule (Days and Calling window), then Start again.
          </p>
        ) : (
          s?.running && state?.reason && <p className="text-[12.5px] text-muted-foreground">{state.reason}</p>
        )}

        {tab === "overview" && <OverviewTab s={s} />}
        {tab === "contacts" && camp && (
          <ContactsTab
            campaignId={id}
            rows={contactsPoll.data}
            valueLabel={type?.columns.find((c) => c.key === "amount")?.label}
            canRequeue={!s?.running && !!s && s.not_connected + s.hung_up > 0}
            onChanged={refresh}
          />
        )}
        {tab === "callback" && <CallbackBoard campaignId={id} />}
      </div>
    </div>
  );
}

function OverviewTab({ s }: { s: Stats | null }) {
  const steps = s
    ? [
        ["Audience", s.audience],
        ["Called", s.called],
        ["Connected", s.connected],
        ["Engaged", s.engaged],
        ["Converted", s.converted],
      ]
    : [];
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <StatCard label="Audience" value={s ? fmtInt(s.audience) : "—"} note={s?.to_call ? `${fmtInt(s.to_call)} still to call` : ""} />
        <StatCard label="Called" value={s ? fmtInt(s.called) : "—"} note={s?.audience ? `${fmtPct(pct(s.called, s.audience), 0)} complete` : ""} />
        <StatCard label="Connected" value={s ? fmtInt(s.connected) : "—"} note={s?.called ? `${fmtPct(pct(s.connected, s.called))} of called` : ""} />
        <StatCard
          label="Conversions"
          value={s ? fmtInt(s.converted) : "—"}
          note={s?.connected ? `${fmtPct(pct(s.converted, s.connected))} of connected` : ""}
        />
        <StatCard
          label="Spend"
          value={s ? rs(s.cost_rs) : "—"}
          note={s ? `Agent ${rs(s.agent_rs)} · line ${rs(s.line_rs)}${s.budget_rs ? ` · daily cap ${fmtRs(s.budget_rs)}` : ""}` : ""}
        />
        <StatCard label="Cost / conversion" value={s?.converted ? rs(s.cost_rs / s.converted) : "—"} note={s ? `${plural(s.calls, "call")} placed` : ""} />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card className="overflow-hidden">
          <div className="border-b px-4 py-3">
            <h2 className="text-[15px] font-medium">Funnel</h2>
          </div>
          <div className="space-y-4 p-4">
            {steps.map(([label, n]) => {
              const share = s?.audience ? (100 * Number(n)) / s.audience : 0;
              return (
                <div key={label}>
                  <div className="mb-1.5 flex items-baseline justify-between text-[13.5px]">
                    <span>{label}</span>
                    <span className="text-muted-foreground tabular">
                      {fmtInt(Number(n))} · {Math.round(share)}%
                    </span>
                  </div>
                  <div className="h-2 overflow-hidden rounded-full bg-muted">
                    <div className="h-full rounded-full bg-brand transition-[width]" style={{ width: `${share}%` }} />
                  </div>
                </div>
              );
            })}
            {!s && <p className="text-muted-foreground">Loading…</p>}
            {s && <p className="text-[12px] text-muted-foreground">Engaged: picked up and talked it through to an answer (not unclear, not a wrong number).</p>}
          </div>
        </Card>

        <Card className="flex flex-col overflow-hidden">
          <div className="border-b px-4 py-3">
            <h2 className="text-[15px] font-medium">Calls per day</h2>
          </div>
          <div className="relative h-[280px] px-2 pt-4 pb-2">
            {s && s.series.length === 0 && <div className="absolute inset-0 grid place-items-center text-muted-foreground">No calls yet.</div>}
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={s?.series ?? []} margin={{ top: 4, right: 16, bottom: 0, left: 0 }}>
                <defs>
                  <linearGradient id="perday" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--brand)" stopOpacity={0.18} />
                    <stop offset="100%" stopColor="var(--brand)" stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid vertical={false} stroke="var(--border)" />
                <XAxis dataKey="label" tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} minTickGap={20} />
                <YAxis tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} width={40} allowDecimals={false} />
                <Tooltip
                  cursor={{ stroke: "var(--input)" }}
                  contentStyle={{ background: "var(--card)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 13 }}
                  labelStyle={{ color: "var(--muted-foreground)" }}
                  formatter={(v, name) => [fmtInt(Number(v)), name === "calls" ? "Calls" : "Connected"]}
                />
                <Area type="monotone" dataKey="calls" stroke="var(--brand)" strokeWidth={2} fill="url(#perday)" isAnimationActive={false} />
                <Area type="monotone" dataKey="connected" stroke="var(--info)" strokeWidth={1.5} fill="none" isAnimationActive={false} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </Card>
      </div>

      <Card className="overflow-hidden">
        <div className="border-b px-4 py-3">
          <h2 className="text-[15px] font-medium">Outcomes</h2>
        </div>
        <div className="grid grid-cols-2 gap-x-6 gap-y-4 p-4 sm:grid-cols-3 lg:grid-cols-6">
          {(s?.outcomes ?? []).map((o) => (
            <div key={o.key}>
              <div className="text-[12.5px] text-muted-foreground">{o.key === "dropped" ? "Picked up, no result" : (outcomeOf(o.key)?.label ?? o.key)}</div>
              <div className="mt-0.5 text-[20px] font-semibold tabular">{fmtInt(o.n)}</div>
            </div>
          ))}
          {!s && <p className="text-muted-foreground">Loading…</p>}
        </div>
      </Card>
    </div>
  );
}

type ContactsProps = {
  campaignId: number;
  rows: ContactRow[] | null;
  valueLabel?: string; // the amount column as the call type names it: Price, Cart value...
  canRequeue: boolean;
  onChanged: () => void;
};

function ContactsTab({ campaignId, rows, valueLabel, canRequeue, onChanged }: ContactsProps) {
  const { toast, confirm } = useFeedback();
  const [q, setQ] = useState("");
  const [show, setShow] = useState("all");
  const [asc, setAsc] = useState(true);
  const [page, setPage] = useState(0);
  const [openCall, setOpenCall] = useState<number | null>(null);

  const all = rows ?? [];
  const statuses = [...new Set(all.map((c) => c.status))];
  const query = q.trim().toLowerCase();
  const shown = all
    .filter((c) => (show === "all" ? true : show === "called" ? c.status !== "pending" : c.status === show))
    .filter((c) => !query || [c.customer_name, c.phone, c.order_id, c.product].join(" ").toLowerCase().includes(query))
    .sort((a, b) => (asc ? 1 : -1) * (a.customer_name || "").localeCompare(b.customer_name || ""));
  const pages = Math.max(1, Math.ceil(shown.length / PAGE));
  const at = Math.min(page, pages - 1);

  const requeue = async () => {
    const ok = await confirm({
      title: "Call no answer / busy again?",
      body: "Every no-answer, busy and no-result contact of this campaign goes back on the list with a fresh count of tries.",
      confirmLabel: "Put back on the list",
    });
    if (!ok) return;
    try {
      const j = await send<{ requeued: number }>("/api/contacts/requeue", "POST", { statuses: ["no_answer", "busy", "dropped"], campaign_id: campaignId });
      toast(`${plural(j.requeued, "contact")} back on the list.`);
    } catch (e) {
      toast((e as Error).message, true);
    }
    onChanged();
  };

  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-center gap-2 border-b px-4 py-3">
        <div className="relative w-full max-w-[260px]">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
          <input
            className="h-8 w-full rounded-md border border-input bg-card pr-3 pl-8 text-[13px] outline-none focus-visible:border-brand focus-visible:ring-2 focus-visible:ring-brand/20"
            placeholder="Search name, phone, order…"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setPage(0);
            }}
          />
        </div>
        <Select
          className="w-[190px]"
          value={show}
          onChange={(e) => {
            setShow(e.target.value);
            setPage(0);
          }}
          aria-label="Status"
        >
          <option value="all">All statuses ({all.length})</option>
          <option value="called">Called ({all.filter((c) => c.status !== "pending").length})</option>
          {statuses.map((st) => (
            <option key={st} value={st}>
              {outcomeOf(st)?.label ?? st} ({all.filter((c) => c.status === st).length})
            </option>
          ))}
        </Select>
        <div className="ml-auto flex flex-wrap gap-2">
          {canRequeue && (
            <Button variant="outline" size="sm" onClick={requeue}>
              Call no answer / busy again
            </Button>
          )}
          <Link href={`/campaigns/new/?edit=${campaignId}&step=3`} className={buttonClass("outline", "sm")}>
            <UserPlus />
            Add contacts
          </Link>
          <a href={`/api/campaigns/${campaignId}/export`} className={buttonClass("outline", "sm")}>
            <Download />
            Download
          </a>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-left text-[13.5px]">
          <thead className="text-[12.5px] text-muted-foreground">
            <tr className="border-b">
              <th className="px-4 py-2.5 font-normal">
                <button type="button" onClick={() => setAsc(!asc)} className="inline-flex items-center gap-1 hover:text-foreground">
                  Contact {asc ? <ArrowDownAZ className="size-3.5" /> : <ArrowUpAZ className="size-3.5" />}
                </button>
              </th>
              <th className="px-4 py-2.5 font-normal">Phone</th>
              <th className="px-4 py-2.5 font-normal">Status</th>
              <th className="px-4 py-2.5 text-right font-normal">Attempts</th>
              {valueLabel && <th className="px-4 py-2.5 text-right font-normal">{valueLabel.replace(/ \(.*\)$/, "")}</th>}
              <th className="px-4 py-2.5 font-normal">Last call</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {shown.slice(at * PAGE, at * PAGE + PAGE).map((c) => {
              const st = outcomeOf(c.status);
              const due = CALLBACK_STATUSES.includes(c.status) && c.next_try_at;
              const opens = !!c.last_call_id && ((c.last_duration ?? 0) > 0 || !!c.last_error);
              return (
                <tr
                  key={c.id}
                  onClick={() => opens && setOpenCall(c.last_call_id)}
                  className={cn("hover:bg-muted/40", opens && "cursor-pointer")}
                  title={opens ? "Open the conversation" : undefined}
                >
                  <td className="px-4 py-2.5">
                    <div className="font-medium">{c.customer_name || "—"}</div>
                    {(c.order_id || c.product) && (
                      <div className="max-w-[260px] truncate text-[12px] text-muted-foreground">
                        {[fmtOrder(c.order_id), c.product].filter(Boolean).join(" · ")}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-2.5 font-mono text-[12.5px]">{c.phone ? fmtPhone(c.phone) : "no phone"}</td>
                  <td className="px-4 py-2.5">
                    {st && (
                      <Badge tone={st.tone} dot>
                        {st.label}
                      </Badge>
                    )}
                    {due && (
                      <div className="mt-0.5 text-[11.5px] text-muted-foreground">
                        next {new Date(c.next_try_at!).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-right tabular">{c.attempts || 0}</td>
                  {valueLabel && <td className="px-4 py-2.5 text-right tabular">{c.amount ? `₹${c.amount}` : "—"}</td>}
                  <td className="px-4 py-2.5 text-muted-foreground">{c.last_call_at ? fmtDay(c.last_call_at) : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {rows && shown.length === 0 && <p className="px-4 py-10 text-center text-muted-foreground">{all.length ? "No contacts match." : "No contacts yet."}</p>}
        {!rows && <p className="px-4 py-10 text-center text-muted-foreground">Loading…</p>}
      </div>

      <Pager page={at + 1} pages={pages} onPage={(n) => setPage(n - 1)} total={shown.length} size={PAGE} />

      {openCall != null && <CallPanel key={openCall} callId={openCall} onClose={() => setOpenCall((c) => (c === openCall ? null : c))} />}
    </Card>
  );
}
