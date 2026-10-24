"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronDown, ExternalLink, Headphones, PhoneOff, ShoppingBag, X } from "lucide-react";
import { Badge, type Tone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { usePoll } from "@/hooks/use-poll";
import { useSidePanel } from "@/hooks/use-side-panel";
import type { CallDetail, CallSubject, LiveCall } from "@/lib/api";
import { fmtClock, fmtOrder, fmtPhone } from "@/lib/format";
import { outcomeOf } from "@/lib/outcomes";
import { cn } from "@/lib/utils";

const STAGE: Record<string, { label: string; tone: Tone }> = {
  ringing: { label: "Ringing", tone: "warn" },
  agent_speaking: { label: "Agent speaking", tone: "info" },
  listening: { label: "Listening", tone: "info" },
  ending: { label: "Ending", tone: "neutral" },
};

/** Words the details of a result are stored under, as the team reads them. */
const DETAIL_LABELS: Record<string, string> = {
  notes: "Notes",
  reply: "Reply",
  question: "Question",
  callback_time: "Call back",
  cancel_reason: "Cancel reason",
  not_interested_reason: "Reason",
  mismatch: "Mismatch",
  new_address: "New address",
  order_changes: "Order changes",
  payment_change: "Payment",
  whatsapp_coupon: "WhatsApp coupon",
  complaint_type: "Complaint about",
  message_id: "Message no.",
};

type Props = { callId: number; summary?: LiveCall; campaignName?: string; onClose: () => void };

/** The right-hand panel: one call's conversation, live while it is on (refreshed every second), then as saved. */
export function CallPanel({ callId, summary, campaignName, onClose }: Props) {
  const [ended, setEnded] = useState(false);
  const { data, error, updatedAt } = usePoll<CallDetail>(`/api/live/${callId}`, ended ? 60_000 : 1_000);
  const [now, setNow] = useState(() => Date.now());
  const [hangingUp, setHangingUp] = useState(false);
  const [hangupError, setHangupError] = useState("");
  const panel = useRef<HTMLElement>(null);
  const log = useRef<HTMLDivElement>(null);
  const stick = useRef(true); // follow new lines unless the reader scrolled up

  useSidePanel(panel, 440, onClose);

  const call = data ?? (summary ? { ...summary, live: true, outcome_details: null } : null);
  const live = call?.live ?? true;

  // Stop asking every second once the call has ended: the saved transcript does not change.
  if (data && !data.live && !ended) setEnded(true);

  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [live]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    const el = log.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [call?.lines]);

  const drift = live && updatedAt ? Math.max(0, (now - updatedAt) / 1000) : 0;
  const secs = (call?.seconds ?? 0) + drift;
  const ringing = call?.status === "ringing";
  const stage = live ? (STAGE[call?.status ?? ""] ?? null) : null;
  const result = outcomeOf(call?.outcome);
  // "outcome" repeats the badge at the top.
  const details = Object.entries(call?.outcome_details ?? {}).filter(([k, v]) => k !== "outcome" && v != null && String(v).trim() !== "");

  const hangup = async () => {
    if (!call) return;
    const verb = ringing ? "Stop ringing" : "Hang up on";
    if (!confirm(`${verb} ${call.customer_name || "this customer"} (${fmtPhone(call.to ?? "")})?`)) return;
    setHangingUp(true);
    setHangupError("");
    try {
      const r = await fetch(`/api/calls/${callId}/hangup`, { method: "POST" });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || `${r.status} ${r.statusText}`);
    } catch (e) {
      setHangupError((e as Error).message);
    } finally {
      setHangingUp(false);
    }
  };

  return (
    <aside
      ref={panel}
      aria-label="Call conversation"
      className="side-panel fixed inset-y-0 right-0 z-40 flex w-full max-w-[440px] flex-col border-l bg-card shadow-2xl sm:top-[52px] sm:shadow-xl"
    >
      <header className="border-b px-4 pt-3.5 pb-3">
        <div className="flex items-start gap-3">
          <div className="min-w-0 flex-1">
            <div className="truncate text-[16px] font-semibold">{call?.customer_name || "Unknown customer"}</div>
            <div className="mt-0.5 truncate font-mono text-[12.5px] text-muted-foreground">
              {fmtPhone(call?.to ?? "")}
              {call?.order_id ? ` · ${fmtOrder(call.order_id)}` : ""}
            </div>
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground">
            <X className="size-4" />
          </button>
        </div>
        <div className="mt-2.5 flex flex-wrap items-center gap-2 text-[12.5px] text-muted-foreground">
          {live ? (
            <Badge tone={ringing ? "warn" : "good"} dot>
              {ringing ? "Ringing" : "Live"}
            </Badge>
          ) : (
            <Badge tone="neutral">Ended</Badge>
          )}
          {stage && !ringing && <Badge tone={stage.tone}>{stage.label}</Badge>}
          {result && <Badge tone={result.tone}>{result.label}</Badge>}
          <span className="font-mono tabular">
            {ringing && call?.ring_left != null ? `${Math.max(0, Math.round(call.ring_left - drift))}s left to answer` : fmtClock(secs)}
          </span>
        </div>
        {(campaignName || call?.voice) && (
          <div className="mt-1.5 truncate text-[12.5px] text-muted-foreground">
            {[campaignName, call?.voice && `${call.voice}${call.agent ? ` — ${call.agent}` : ""}`].filter(Boolean).join(" · ")}
          </div>
        )}
        {/* Owner, 24 Oct 2026: the whole conversation, both sides in one file, here and on Google Drive. */}
        {!live && call?.channel === "plivo" && (
          <div className="mt-2 flex flex-wrap gap-3 text-[12.5px] font-medium">
            <a href={`/api/calls/${callId}/recording`} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-brand hover:underline">
              <Headphones className="size-3.5" />
              Listen
            </a>
            {data?.drive_link && (
              <a href={data.drive_link} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-brand hover:underline">
                <ExternalLink className="size-3.5" />
                Recording on Google Drive
              </a>
            )}
          </div>
        )}
      </header>

      {data?.subject && <Subject s={data.subject} />}

      <div
        ref={log}
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
        className="flex-1 space-y-3 overflow-y-auto bg-background px-4 py-4"
      >
        {error && !call && <p className="text-bad">Could not load the call: {error}</p>}
        {call && call.lines.length === 0 && (
          <p className="py-10 text-center text-muted-foreground">{ringing ? "Ringing… the conversation shows here once they pick up." : "Nothing said yet."}</p>
        )}
        {call?.lines.map((l, i) => {
          const agent = l.role === "agent";
          return (
            <div key={i} className={cn("flex flex-col", agent ? "items-start" : "items-end")}>
              <span className="mb-1 px-1 text-[11.5px] text-muted-foreground">{agent ? `Agent${call.voice ? ` · ${call.voice}` : ""}` : "Customer"}</span>
              <div
                className={cn(
                  "max-w-[85%] rounded-2xl px-3.5 py-2 text-[14px] leading-snug break-words whitespace-pre-wrap",
                  agent ? "rounded-tl-sm border bg-card" : "rounded-tr-sm bg-primary text-primary-foreground",
                )}
              >
                {l.text}
              </div>
            </div>
          );
        })}
      </div>

      {!live && (details.length > 0 || call?.error) && (
        <div className="max-h-[30%] overflow-y-auto border-t px-4 py-3 text-[13px]">
          <div className="mb-1.5 font-medium">Result</div>
          {call?.error && <p className="mb-1 text-bad">{call.error}</p>}
          <dl className="space-y-1">
            {details.map(([k, v]) => (
              <div key={k} className="flex gap-2">
                <dt className="w-[96px] shrink-0 text-muted-foreground">{DETAIL_LABELS[k] ?? k.replace(/_/g, " ")}</dt>
                <dd className="min-w-0 break-words">{typeof v === "string" ? v : JSON.stringify(v)}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      {live && (
        <footer className="flex items-center gap-3 border-t px-4 py-3">
          {call?.channel === "plivo" ? (
            <Button variant="outline" className="border-bad/40 text-bad hover:bg-bad-soft" onClick={hangup} disabled={hangingUp}>
              <PhoneOff />
              {hangingUp ? "Ending…" : ringing ? "Stop ringing" : "Hang up"}
            </Button>
          ) : (
            <span className="text-[12.5px] text-muted-foreground">Browser test: end it from the test itself.</span>
          )}
          {hangupError && <span className="text-[12.5px] text-bad">{hangupError}</span>}
        </footer>
      )}
    </aside>
  );
}

const money = (a: string) => {
  const n = Number(String(a).replace(/[^\d.]/g, ""));
  return a && Number.isFinite(n) && n > 0 ? `₹${n.toLocaleString("en-IN")}` : "";
};

/** What the call was about (owner, 22 Oct 2026): the order or the cart's products, or a one-off call's reason.
 *  Open by default; folds away to give the conversation the room. */
function Subject({ s }: { s: CallSubject }) {
  const qty = s.quantity && s.quantity !== "1" ? `Qty ${s.quantity}` : s.quantity ? "Qty 1" : "";
  const meta = [qty, money(s.amount), s.payment_mode].filter(Boolean).join(" · ");
  const custom = s.kind === "custom";
  return (
    <details open className="group border-b bg-muted/30 px-4 py-2.5 text-[13px]">
      <summary className="flex cursor-pointer list-none items-center gap-2 [&::-webkit-details-marker]:hidden">
        <ShoppingBag className="size-4 shrink-0 text-muted-foreground" />
        <span className="font-medium">{custom ? "Reason for the call" : s.kind === "abandoned_cart" ? "Cart" : "Order"}</span>
        <Badge>{s.kind_name}</Badge>
        {s.order_id && <span className="truncate font-mono text-[12px] text-muted-foreground">{fmtOrder(s.order_id)}</span>}
        <ChevronDown className="ml-auto size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-180" />
      </summary>
      <div className="mt-2.5 space-y-2">
        {custom && <p className="leading-snug whitespace-pre-wrap">{s.reason || "No reason was written for this call."}</p>}
        {!custom &&
          (s.products.length ? (
            s.products.map((p, i) => (
              <div key={i} className="flex items-center gap-2.5">
                {p.image ? (
                  // eslint-disable-next-line @next/next/no-img-element -- store photos by their own address, in a static export
                  <img
                    src={`${p.image}${p.image.includes("?") ? "&" : "?"}width=96`}
                    alt=""
                    className="size-10 shrink-0 rounded-md border bg-muted object-cover"
                  />
                ) : (
                  <span className="size-10 shrink-0 rounded-md border bg-muted" />
                )}
                {p.link ? (
                  <a href={p.link} target="_blank" rel="noreferrer" className="line-clamp-2 min-w-0 leading-snug font-medium hover:underline">
                    {p.title}
                    <ExternalLink className="ml-1 inline size-3 text-muted-foreground" />
                  </a>
                ) : (
                  <span className="line-clamp-2 min-w-0 leading-snug font-medium">{p.title}</span>
                )}
              </div>
            ))
          ) : (
            <p className="line-clamp-3 leading-snug font-medium">{s.product || "No product on record"}</p>
          ))}
        {!custom && meta && <div className="text-muted-foreground tabular">{meta}</div>}
        {!custom && (s.address || s.delivery_date) && (
          <dl className="space-y-0.5 text-[12.5px]">
            {s.address && (
              <div className="flex gap-2">
                <dt className="w-[60px] shrink-0 text-muted-foreground">Address</dt>
                <dd className="line-clamp-2 min-w-0">{s.address}</dd>
              </div>
            )}
            {s.delivery_date && (
              <div className="flex gap-2">
                <dt className="w-[60px] shrink-0 text-muted-foreground">Delivery</dt>
                <dd className="min-w-0">{s.delivery_date}</dd>
              </div>
            )}
          </dl>
        )}
        {s.cart_link && (
          <a
            href={s.cart_link}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-[12.5px] font-medium text-brand hover:underline"
          >
            Open the cart
            <ExternalLink className="size-3" />
          </a>
        )}
      </div>
    </details>
  );
}
