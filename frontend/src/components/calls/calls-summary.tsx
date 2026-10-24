"use client";

import { useState } from "react";
import { Download, ExternalLink, Headphones, Monitor, MessageSquare, Phone, Search, Send, Trash2 } from "lucide-react";
import { CallPanel } from "@/components/overview/call-panel";
import { StatCard } from "@/components/overview/stat-card";
import { Badge } from "@/components/ui/badge";
import { Button, buttonClass } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useFeedback } from "@/components/ui/feedback";
import { PAGE_SIZE, Pager, usePaged } from "@/components/ui/pager";
import { Select } from "@/components/ui/select";
import { usePoll } from "@/hooks/use-poll";
import { send, type CallRow, type CampaignRow } from "@/lib/api";
import { fmtClock, fmtInt, fmtOrder, fmtPhone, fmtRs } from "@/lib/format";
import { complaintKind, outcomeOf } from "@/lib/outcomes";
import { cn } from "@/lib/utils";

type Preset = "today" | "yesterday" | "7" | "30" | "month" | "all" | "custom";
const PRESETS: { key: Preset; label: string }[] = [
  { key: "today", label: "Today" },
  { key: "yesterday", label: "Yesterday" },
  { key: "7", label: "7 days" },
  { key: "30", label: "30 days" },
  { key: "month", label: "This month" },
  { key: "all", label: "All" },
  { key: "custom", label: "Custom" },
];

/** An Indian date (YYYY-MM-DD), `back` days ago: the server counts days in Indian time. */
const istDay = (back = 0) => new Date(Date.now() + 330 * 60_000 - back * 86_400_000).toISOString().slice(0, 10);

function range(p: Preset, from: string, to: string): [string, string] {
  const t = istDay();
  if (p === "today") return [t, t];
  if (p === "yesterday") return [istDay(1), istDay(1)];
  if (p === "7" || p === "30") return [istDay(Number(p) - 1), t];
  if (p === "month") return [`${t.slice(0, 8)}01`, t];
  if (p === "custom") return [from, to || from];
  return ["", ""];
}

/** A call's result as one key: its outcome, else an error, else "picked up but nothing recorded" or "no answer". */
const outcomeKey = (c: CallRow) => c.outcome || (c.error ? "error" : (c.duration_sec || 0) > 0 ? "none" : "no_answer");
const detailText = (c: CallRow) =>
  Object.entries(c.outcome_details || {})
    .filter(([k, v]) => k !== "outcome" && v != null && String(v).trim())
    .map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`)
    .join(" · ");
const when = (iso: string) => new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
const rs = (n: number) => fmtRs(n, 2);

/** Every call with its result, by day and campaign; a click opens the conversation. */
export function CallsSummary() {
  const { toast, confirm } = useFeedback();
  const [preset, setPreset] = useState<Preset>("today");
  const [from, setFrom] = useState(istDay());
  const [to, setTo] = useState(istDay());
  const [campaignId, setCampaignId] = useState(0);
  const [outcome, setOutcome] = useState("all");
  const [kind, setKind] = useState("all");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<number | null>(null);

  const [dayFrom, dayTo] = range(preset, from, to);
  const qs = new URLSearchParams();
  if (campaignId) qs.set("campaign_id", String(campaignId));
  if (dayFrom) qs.set("day_from", dayFrom);
  if (dayTo) qs.set("day_to", dayTo);
  const calls = usePoll<CallRow[]>(`/api/calls?${qs}`, 10_000);
  // Download: the same calls as the table (dates, campaign, result, type, search), every one of the period.
  const exportQs = new URLSearchParams(qs);
  if (outcome !== "all") exportQs.set("outcome", outcome);
  if (kind !== "all") exportQs.set("kind", kind);
  if (q.trim()) exportQs.set("q", q.trim());
  const campaigns = usePoll<CampaignRow[]>("/api/campaigns", 60_000).data ?? [];

  const all = calls.data ?? [];
  const query = q.trim().toLowerCase();
  const rows = all.filter(
    (c) =>
      (outcome === "all" || outcomeKey(c) === outcome) &&
      (kind === "all" || (kind === "phone") === (c.channel === "plivo")) &&
      (!query || [c.customer_name, c.phone, c.order_id, c.campaign_name].join(" ").toLowerCase().includes(query)),
  );
  const outcomes = [...new Set(all.map(outcomeKey))];
  const connected = rows.filter((c) => (c.duration_sec || 0) > 0 && !["no_answer", "busy"].includes(c.outcome || "")).length;
  const talk = rows.reduce((n, c) => n + (c.duration_sec || 0), 0);
  const cost = rows.reduce((n, c) => n + (c.cost_rs || 0), 0);
  const cap = dayFrom ? 1000 : 100;
  const paged = usePaged(rows, PAGE_SIZE, [qs, outcome, kind, query].join("|"));

  /** The coupon message this call's customer gets after a yes, sent now on WhatsApp with no call (owner, 22 Oct 2026). */
  const [sending, setSending] = useState<number | null>(null);
  const sendCoupon = async (c: CallRow) => {
    const ok = await confirm({
      title: "Send the WhatsApp coupon now?",
      body: `${c.customer_name || "This customer"}${c.phone ? ` (${fmtPhone(c.phone)})` : ""} gets the coupon message on WhatsApp now, with no call. It is a real message to that number, with the offer as it is set today. The order's payment is not changed.`,
      confirmLabel: "Send on WhatsApp",
    });
    if (!ok) return;
    setSending(c.id);
    try {
      const m = await send<{ status: string; error: string | null; to_phone: string }>(`/api/calls/${c.id}/send-coupon`, "POST");
      if (m.status === "sent") toast(`WhatsApp coupon sent to ${fmtPhone(m.to_phone)}.`);
      else toast(`Not sent: ${m.error || m.status}`, true);
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setSending(null);
    }
  };

  const clearAll = async () => {
    const ok = await confirm({
      title: "Delete the whole call history?",
      body: "Every call record, its transcript and its result are deleted, for good. Contacts and campaigns are kept.",
      confirmLabel: "Delete all calls",
      danger: true,
    });
    if (!ok) return;
    try {
      await send("/api/calls", "DELETE");
      toast("Call history cleared.");
    } catch (e) {
      toast((e as Error).message, true);
    }
    calls.reload();
  };

  return (
    <div className="pb-10">
      <div className="flex flex-wrap items-start gap-3 border-b px-4 pt-5 pb-4 sm:px-6">
        <div className="min-w-0">
          <h1 className="text-[20px] font-semibold tracking-tight">Calls summary</h1>
          <p className="mt-0.5 text-[13.5px] text-muted-foreground">Every call, newest first, with its result and transcript.</p>
        </div>
        <a
          href={`/api/calls/export?${exportQs}`}
          className={buttonClass("outline", "md", "ml-auto")}
          title="An Excel file of the calls these filters show, with each result and what the customer said"
        >
          <Download />
          Download
        </a>
        {/* Owner, 24 Oct 2026: the recordings of the calls these filters show (the day, or a custom range), in one zip. */}
        <a
          href={`/api/calls/recordings?${exportQs}`}
          className={buttonClass("outline", "md")}
          title="Every recording of the calls these filters show (both voices, one MP3 per call, a folder per day) as one zip. A full day takes about a minute the first time."
        >
          <Headphones />
          Recordings
        </a>
        <Button variant="outline" className="border-bad/40 text-bad hover:bg-bad-soft" onClick={clearAll}>
          <Trash2 />
          Clear history
        </Button>
      </div>

      <div className="space-y-4 px-4 pt-5 sm:px-6">
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex flex-wrap gap-1.5" role="group" aria-label="Dates">
            {PRESETS.map((p) => (
              <button
                key={p.key}
                type="button"
                aria-pressed={preset === p.key}
                onClick={() => setPreset(p.key)}
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
                onChange={(e) => setFrom(e.target.value)}
              />
              to
              <input
                type="date"
                aria-label="To"
                className="h-8 rounded-md border border-input bg-card px-2.5 text-[13px] text-foreground"
                value={to}
                min={from}
                max={istDay()}
                onChange={(e) => setTo(e.target.value)}
              />
            </div>
          )}
          <Select className="w-[180px]" value={campaignId} onChange={(e) => setCampaignId(Number(e.target.value))} aria-label="Campaign">
            <option value={0}>All campaigns</option>
            {campaigns.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </Select>
          <Select className="w-[170px]" value={outcome} onChange={(e) => setOutcome(e.target.value)} aria-label="Outcome">
            <option value="all">All outcomes</option>
            {outcomes.map((o) => (
              <option key={o} value={o}>
                {outcomeOf(o)?.label ?? o} ({all.filter((c) => outcomeKey(c) === o).length})
              </option>
            ))}
          </Select>
          <Select className="w-[140px]" value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Type">
            <option value="all">Phone and browser</option>
            <option value="phone">Phone calls</option>
            <option value="browser">Browser tests</option>
          </Select>
          <div className="relative w-full max-w-[240px]">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
            <input
              className="h-8 w-full rounded-md border border-input bg-card pr-3 pl-8 text-[13px] outline-none focus-visible:border-brand focus-visible:ring-2 focus-visible:ring-brand/20"
              placeholder="Search name, phone, order…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <StatCard label="Calls" value={calls.data ? fmtInt(rows.length) : "—"} note={all.length >= cap ? `The latest ${cap} of the period` : ""} />
          <StatCard
            label="Connected"
            value={calls.data ? fmtInt(connected) : "—"}
            note={rows.length ? `${Math.round((100 * connected) / rows.length)}% of calls` : ""}
          />
          <StatCard label="Talk time" value={calls.data ? fmtClock(talk) : "—"} note={connected ? `${fmtClock(talk / connected)} a connected call` : ""} />
          <StatCard label="Cost" value={calls.data ? rs(cost) : "—"} note="Agent and phone line" />
        </div>

        <Card className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1000px] text-left text-[13.5px]">
              <thead className="text-[12.5px] text-muted-foreground">
                <tr className="border-b">
                  {["#", "Type", "Started", "Customer", "Campaign", "Length", "Outcome", "Cost", ""].map((h, i) => (
                    <th key={i} className={cn("px-4 py-2.5 font-normal", (h === "Cost" || h === "Length") && "text-right")}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y">
                {paged.rows.map((c) => {
                  const o = outcomeOf(outcomeKey(c));
                  const details = detailText(c);
                  const said = (c.transcript || []).length > 0 || !!c.error;
                  return (
                    <tr key={c.id} className="align-top hover:bg-muted/40">
                      <td className="px-4 py-3 text-muted-foreground tabular">{c.id}</td>
                      <td className="px-4 py-3">
                        <Badge>
                          {c.channel === "plivo" ? <Phone className="size-3" /> : <Monitor className="size-3" />}
                          {c.channel === "plivo" ? "Phone" : "Browser"}
                        </Badge>
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap">{when(c.started_at)}</td>
                      <td className="px-4 py-3">
                        <div className="font-medium">{c.customer_name || "—"}</div>
                        <div className="font-mono text-[12px] text-muted-foreground">
                          {[c.phone ? fmtPhone(c.phone) : "", fmtOrder(c.order_id)].filter(Boolean).join(" · ")}
                        </div>
                      </td>
                      <td className="px-4 py-3 text-[12.5px] text-muted-foreground">{c.campaign_name || (c.contact_id ? "Single call" : "—")}</td>
                      <td className="px-4 py-3 text-right font-mono tabular">{fmtClock(c.duration_sec || 0)}</td>
                      <td className="max-w-[360px] px-4 py-3">
                        {o && (
                          <Badge tone={o.tone}>
                            {o.label}
                            {complaintKind(c.outcome_details) && ` · ${complaintKind(c.outcome_details)}`}
                          </Badge>
                        )}
                        {details && <div className="mt-1 text-[12px] leading-snug text-muted-foreground">{details}</div>}
                        {c.error && !details && <div className="mt-1 text-[12px] leading-snug text-bad">{c.error.slice(0, 160)}</div>}
                      </td>
                      <td
                        className="px-4 py-3 text-right whitespace-nowrap tabular"
                        title={c.cost_estimated ? "Estimated from the call's length" : "From the tokens the agent reported"}
                      >
                        {c.cost_estimated ? "~" : ""}
                        {rs(c.cost_rs || 0)}
                      </td>
                      <td className="px-4 py-2">
                        <div className="flex justify-end gap-1">
                          {said && (
                            <Button size="sm" variant="ghost" onClick={() => setOpen(c.id)}>
                              <MessageSquare />
                              View
                            </Button>
                          )}
                          {c.recording && (
                            <a
                              href={`/api/calls/${c.id}/recording`}
                              target="_blank"
                              rel="noreferrer"
                              className="inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-[13px] font-medium hover:bg-muted [&_svg]:size-4"
                            >
                              <Headphones />
                              Listen
                            </a>
                          )}
                          {c.drive_link && (
                            <a
                              href={c.drive_link}
                              target="_blank"
                              rel="noreferrer"
                              title="The recording on Google Drive"
                              className="inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-[13px] font-medium hover:bg-muted [&_svg]:size-4"
                            >
                              <ExternalLink />
                              Drive
                            </a>
                          )}
                          {c.contact_id && (
                            <Button
                              size="sm"
                              variant="ghost"
                              disabled={sending === c.id}
                              onClick={() => sendCoupon(c)}
                              title="Send this customer's WhatsApp coupon now, without a call"
                            >
                              <Send />
                              {sending === c.id ? "Sending…" : "WhatsApp"}
                            </Button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {calls.data && rows.length === 0 && (
              <p className="px-4 py-10 text-center text-muted-foreground">{all.length ? "No calls match these filters." : "No calls in this period."}</p>
            )}
            {!calls.data && (
              <p className="px-4 py-10 text-center text-muted-foreground">{calls.error ? `Could not load the calls: ${calls.error}` : "Loading…"}</p>
            )}
          </div>
          <Pager page={paged.page} pages={paged.pages} onPage={paged.setPage} total={paged.total} size={paged.size} />
        </Card>
      </div>

      {open != null && <CallPanel key={open} callId={open} onClose={() => setOpen((c) => (c === open ? null : c))} />}
    </div>
  );
}
